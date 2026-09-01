from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

from frappe.tests import UnitTestCase

from builder_hub.extensions.github import GitHubClient
from builder_hub.extensions.protocol import ProtocolValidationError


class FakeResponse:
	def __init__(self, status_code=200, *, data=b"", headers=None, redirect=False):
		self.status_code = status_code
		self.data = data
		self.headers = headers or {}
		self.is_redirect = redirect
		self.closed = False

	def iter_content(self, chunk_size):
		for start in range(0, len(self.data), chunk_size):
			yield self.data[start : start + chunk_size]

	def close(self):
		self.closed = True


class FakeSession:
	def __init__(self, *responses):
		self.responses = list(responses)
		self.requests = []

	def get(self, url, **kwargs):
		self.requests.append((url, kwargs))
		return self.responses.pop(0)


class GitHubClientTests(UnitTestCase):
	def test_api_response_is_bounded_before_json_decode(self):
		response = FakeResponse(headers={"Content-Length": str(2 * 1024 * 1024 + 1)})
		client = GitHubClient(token="secret", session=FakeSession(response))
		with self.assertRaises(ProtocolValidationError) as raised:
			client.get_repository("123")
		self.assertEqual(raised.exception.code, "github_response_too_large")
		self.assertTrue(response.closed)

	def test_repository_content_accepts_github_base64_line_breaks(self):
		content = b"hello repository"
		encoded = base64.encodebytes(content).decode()
		payload = json.dumps({"type": "file", "encoding": "base64", "content": encoded}).encode()
		client = GitHubClient(token="secret", session=FakeSession(FakeResponse(data=payload)))
		repository = {"owner": {"login": "acme"}, "name": "icons"}
		self.assertEqual(client.get_repository_file(repository, "README.md"), content)

	@patch("builder_hub.extensions.github.socket.getaddrinfo")
	def test_redirect_drops_auth_and_deletes_no_successful_download(self, getaddrinfo):
		getaddrinfo.return_value = [(2, 1, 6, "", ("8.8.8.8", 443))]
		redirect = FakeResponse(
			status_code=302,
			headers={"Location": "https://release-assets.githubusercontent.com/package"},
			redirect=True,
		)
		package = FakeResponse(data=b"package bytes", headers={"Content-Length": "13"})
		session = FakeSession(redirect, package)
		path = GitHubClient(token="secret", session=session).download_asset(
			"https://github.com/acme/icons/releases/download/1.0.0/acme-icons-1.0.0.builderext"
		)
		try:
			self.assertEqual(Path(path).read_bytes(), b"package bytes")
			self.assertEqual(session.requests[0][1]["headers"]["Authorization"], "Bearer secret")
			self.assertNotIn("Authorization", session.requests[1][1]["headers"])
			self.assertTrue(redirect.closed)
			self.assertTrue(package.closed)
		finally:
			Path(path).unlink(missing_ok=True)

	@patch("builder_hub.extensions.github.socket.getaddrinfo")
	def test_package_download_rejects_private_dns_results(self, getaddrinfo):
		getaddrinfo.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
		client = GitHubClient(token="secret", session=FakeSession())
		with self.assertRaises(ProtocolValidationError) as raised:
			client.download_asset("https://github.com/acme/package.builderext")
		self.assertEqual(raised.exception.code, "unsafe_package_url")

	def test_rate_limit_has_stable_error_code(self):
		response = FakeResponse(
			status_code=403,
			headers={"X-RateLimit-Remaining": "0"},
		)
		client = GitHubClient(token="secret", session=FakeSession(response))
		with self.assertRaises(ProtocolValidationError) as raised:
			client.get_repository("123")
		self.assertEqual(raised.exception.code, "github_rate_limited")
		self.assertTrue(response.closed)
