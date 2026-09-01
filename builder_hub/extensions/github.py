"""A bounded GitHub client for extension repository and release reads."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import frappe
import requests

from builder_hub.extensions.content import sanitize_markdown
from builder_hub.extensions.protocol import (
	MAX_PACKAGE_SIZE,
	ProtocolValidationError,
	validate_manifest,
	validate_versions,
)

API_ROOT = "https://api.github.com"
API_RESPONSE_LIMIT = 2 * 1024 * 1024
CONTENT_RESPONSE_LIMIT = 1024 * 1024
CONNECT_TIMEOUT = 5
RESPONSE_TIMEOUT = 20
MAX_REDIRECTS = 3
ASSET_HOSTS = frozenset(
	{
		"github.com",
		"objects.githubusercontent.com",
		"release-assets.githubusercontent.com",
		"github-releases.githubusercontent.com",
	}
)


@dataclass(frozen=True)
class GitHubResponse:
	data: Any
	etag: str | None
	status_code: int


class GitHubClient:
	def __init__(self, token: str | None = None, session: requests.Session | None = None):
		self.token = token if token is not None else _configured_token()
		self.session = session or requests.Session()

	def get_repository(self, repository_id: str) -> dict:
		return self._api_json(f"/repositories/{quote(str(repository_id), safe='')}").data

	def get_repository_by_name(self, owner: str, name: str) -> dict:
		owner = quote(owner, safe="")
		name = quote(name, safe="")
		return self._api_json(f"/repos/{owner}/{name}").data

	def get_repository_file(self, repository: dict, path: str) -> bytes:
		owner = quote(repository["owner"]["login"], safe="")
		name = quote(repository["name"], safe="")
		path = quote(path, safe="/")
		response = self._api_json(f"/repos/{owner}/{name}/contents/{path}", limit=CONTENT_RESPONSE_LIMIT)
		data = response.data
		if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
			raise ProtocolValidationError(
				"missing_repository_file", f"Required repository file is missing: {path}."
			)
		try:
			encoded = "".join((data.get("content") or "").split())
			content = base64.b64decode(encoded, validate=True)
		except ValueError, TypeError:
			raise ProtocolValidationError(
				"invalid_repository_file", f"GitHub returned invalid content for {path}."
			)
		if len(content) > CONTENT_RESPONSE_LIMIT:
			raise ProtocolValidationError(
				"repository_file_too_large", f"Repository file is too large: {path}."
			)
		return content

	def get_release(self, repository: dict, version: str) -> dict:
		owner = quote(repository["owner"]["login"], safe="")
		name = quote(repository["name"], safe="")
		version = quote(version, safe="")
		return self._api_json(f"/repos/{owner}/{name}/releases/tags/{version}").data

	def get_repository_permission(self, repository: dict, username: str) -> dict:
		owner = quote(repository["owner"]["login"], safe="")
		name = quote(repository["name"], safe="")
		username = quote(username, safe="")
		return self._api_json(f"/repos/{owner}/{name}/collaborators/{username}/permission").data

	def get_releases(self, repository: dict, etag: str | None = None) -> GitHubResponse:
		owner = quote(repository["owner"]["login"], safe="")
		name = quote(repository["name"], safe="")
		return self._api_json(
			f"/repos/{owner}/{name}/releases?per_page=100", etag=etag, allow_not_modified=True
		)

	def download_asset(self, url: str) -> str:
		current_url = url
		temporary = tempfile.NamedTemporaryFile(prefix="builderext-", suffix=".builderext", delete=False)
		path = temporary.name
		try:
			with temporary:
				for redirect in range(MAX_REDIRECTS + 1):
					response = self._get_asset_response(current_url)
					try:
						next_url = _get_redirect_url(response, redirect)
						if next_url:
							current_url = next_url
							continue
						_validate_asset_response(response)
						_write_limited_response(
							response,
							temporary,
							MAX_PACKAGE_SIZE,
							"package_too_large",
							"package_download_failed",
						)
						return path
					finally:
						response.close()
		except requests.RequestException:
			if os.path.exists(path):
				os.unlink(path)
			raise ProtocolValidationError(
				"package_download_failed", "GitHub package download could not be completed."
			)
		except Exception:
			_delete_file(path)
			raise

	def _get_asset_response(self, url: str):
		_validate_asset_url(url)
		headers = self._headers(auth=urlparse(url).hostname == "github.com")
		return self.session.get(
			url,
			headers=headers,
			stream=True,
			allow_redirects=False,
			timeout=(CONNECT_TIMEOUT, RESPONSE_TIMEOUT),
		)

	def _api_json(
		self,
		path: str,
		*,
		etag: str | None = None,
		allow_not_modified: bool = False,
		limit: int = API_RESPONSE_LIMIT,
	) -> GitHubResponse:
		url = f"{API_ROOT}{path}"
		headers = self._headers(auth=True)
		if etag:
			headers["If-None-Match"] = etag
		response = self._get_api_response(url, headers)
		try:
			if response.status_code == 304 and allow_not_modified:
				return GitHubResponse(None, etag, 304)
			_validate_api_response(response)
			content = _read_limited_response(
				response, limit, "github_response_too_large", "github_request_failed"
			)
			response_etag = response.headers.get("ETag")
			status_code = response.status_code
		finally:
			response.close()
		return GitHubResponse(_decode_api_json(content), response_etag, status_code)

	def _get_api_response(self, url: str, headers: dict[str, str]):
		try:
			return self.session.get(
				url,
				headers=headers,
				allow_redirects=False,
				stream=True,
				timeout=(CONNECT_TIMEOUT, RESPONSE_TIMEOUT),
			)
		except requests.RequestException:
			raise ProtocolValidationError(
				"github_request_failed", "GitHub API request could not be completed."
			)

	def _headers(self, *, auth: bool) -> dict[str, str]:
		headers = {
			"Accept": "application/vnd.github+json",
			"User-Agent": "Builder-Hub-Extensions/1",
			"X-GitHub-Api-Version": "2022-11-28",
		}
		if auth and self.token:
			headers["Authorization"] = f"Bearer {self.token}"
		return headers


def _get_redirect_url(response, redirect: int) -> str | None:
	if not response.is_redirect:
		return None
	if redirect == MAX_REDIRECTS:
		raise ProtocolValidationError("too_many_redirects", "Package download used too many redirects.")
	return response.headers.get("Location", "")


def _validate_asset_response(response) -> None:
	if response.status_code != 200:
		raise ProtocolValidationError(
			"package_download_failed",
			f"GitHub package download failed with HTTP {response.status_code}.",
		)


def _validate_api_response(response) -> None:
	if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
		raise ProtocolValidationError("github_rate_limited", "GitHub API rate limit was reached.")
	if response.status_code != 200:
		raise ProtocolValidationError(
			"github_request_failed", f"GitHub API request failed with HTTP {response.status_code}."
		)


def _write_limited_response(
	response,
	output,
	limit: int,
	size_error_code: str,
	request_error_code: str,
) -> None:
	for chunk in _iter_limited_chunks(response, limit, size_error_code, request_error_code):
		output.write(chunk)


def _read_limited_response(
	response,
	limit: int,
	size_error_code: str,
	request_error_code: str,
) -> bytes:
	return b"".join(_iter_limited_chunks(response, limit, size_error_code, request_error_code))


def _iter_limited_chunks(response, limit: int, size_error_code: str, request_error_code: str):
	_validate_content_length(response, limit, size_error_code)
	total = 0
	try:
		for chunk in response.iter_content(chunk_size=64 * 1024):
			if not chunk:
				continue
			total += len(chunk)
			if total > limit:
				raise ProtocolValidationError(size_error_code, "GitHub response exceeded its size limit.")
			yield chunk
	except requests.RequestException:
		raise ProtocolValidationError(request_error_code, "GitHub response could not be read.")


def _validate_content_length(response, limit: int, error_code: str) -> None:
	length = response.headers.get("Content-Length")
	if not length:
		return
	try:
		too_large = int(length) > limit
	except ValueError:
		raise ProtocolValidationError("invalid_github_response", "GitHub returned an invalid response size.")
	if too_large:
		raise ProtocolValidationError(error_code, "GitHub response exceeded its size limit.")


def _decode_api_json(content: bytes):
	try:
		return json.loads(content)
	except json.JSONDecodeError:
		raise ProtocolValidationError("invalid_github_response", "GitHub API returned invalid JSON.")


def _delete_file(path: str) -> None:
	if os.path.exists(path):
		os.unlink(path)


def validate_repository(repository: dict, publisher_account_id: str) -> None:
	if repository.get("private"):
		raise ProtocolValidationError("private_repository", "The GitHub repository must be public.")
	if repository.get("archived") or repository.get("disabled"):
		raise ProtocolValidationError("inactive_repository", "The GitHub repository must be active.")
	owner = repository.get("owner") or {}
	if str(owner.get("id")) != str(publisher_account_id):
		raise ProtocolValidationError(
			"repository_owner_mismatch",
			"The repository owner does not match the registered publisher account.",
		)
	if not repository.get("default_branch"):
		raise ProtocolValidationError("invalid_repository", "The GitHub repository has no default branch.")


def repository_url(repository: dict) -> str:
	return repository.get("html_url") or ""


def get_validated_repository_url(repository: dict) -> str:
	url = repository_url(repository)
	if not url.startswith("https://github.com/"):
		raise ProtocolValidationError("invalid_repository", "GitHub returned an invalid repository URL.")
	return url


def get_repository_contract(client: GitHubClient, repository: dict, license_id: str) -> dict:
	files = _get_required_repository_files(client, repository)
	try:
		manifest = validate_manifest(json.loads(files["manifest.json"].decode("utf-8")))
		versions = validate_versions(json.loads(files["versions.json"].decode("utf-8")), manifest)
	except UnicodeDecodeError, json.JSONDecodeError:
		raise ProtocolValidationError(
			"invalid_repository_json", "Repository manifest.json and versions.json must contain UTF-8 JSON."
		)
	_validate_repository_license(repository, license_id, files["LICENSE"])
	return {
		"manifest": manifest,
		"versions": versions,
		"readme": sanitize_markdown(files["README.md"].decode("utf-8", errors="replace")),
	}


def parse_repository_url(url: str) -> tuple[str, str]:
	parsed = urlparse(url)
	parts = parsed.path.strip("/").split("/")
	if (
		parsed.scheme != "https"
		or parsed.hostname != "github.com"
		or len(parts) != 2
		or parsed.query
		or parsed.fragment
	):
		raise ProtocolValidationError(
			"invalid_repository_url", "Enter an HTTPS URL for one GitHub repository."
		)
	name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
	return parts[0], name


def _get_required_repository_files(client: GitHubClient, repository: dict) -> dict[str, bytes]:
	files = {}
	for name in ("manifest.json", "README.md", "LICENSE", "versions.json"):
		try:
			files[name] = client.get_repository_file(repository, name)
		except ProtocolValidationError as error:
			if error.code == "github_request_failed":
				raise ProtocolValidationError(
					"missing_repository_file", f"Required repository file is missing: {name}."
				)
			raise
	return files


def _validate_repository_license(repository: dict, license_id: str, license_content: bytes) -> None:
	repository_license = (repository.get("license") or {}).get("spdx_id")
	if not repository_license or repository_license == "NOASSERTION" or repository_license != license_id:
		raise ProtocolValidationError(
			"license_mismatch", "The repository license does not match the submitted SPDX license ID."
		)
	if not license_content.strip():
		raise ProtocolValidationError("license_mismatch", "The repository LICENSE file is empty.")


def _configured_token() -> str | None:
	return frappe.conf.get("builder_hub_github_token") or frappe.conf.get("github_token")


def _validate_asset_url(url: str) -> None:
	parsed = urlparse(url)
	host = (parsed.hostname or "").lower().rstrip(".")
	if parsed.scheme != "https" or host not in ASSET_HOSTS or parsed.username or parsed.password:
		raise ProtocolValidationError(
			"unsafe_package_url", "The package URL is not an allowed GitHub asset URL."
		)
	try:
		addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
	except socket.gaierror:
		raise ProtocolValidationError("unsafe_package_url", "The package host could not be resolved.")
	if not addresses:
		raise ProtocolValidationError("unsafe_package_url", "The package host could not be resolved.")
	for address in addresses:
		ip = ipaddress.ip_address(address)
		if not ip.is_global:
			raise ProtocolValidationError(
				"unsafe_package_url", "The package host resolved to a private address."
			)
