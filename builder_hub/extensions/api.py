"""Public JSON API and authenticated actions for Builder Hub extensions."""

from __future__ import annotations

import json
from datetime import UTC

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import get_datetime, get_url
from frappe.utils.caching import redis_cache

from builder_hub.extensions import publishing, submission
from builder_hub.extensions.protocol import (
	EXTENSION_NAME_PATTERN,
	EXTENSIONS_API_VERSION,
	SCHEMA_VERSION,
	SEMVER_PATTERN,
	semver_key,
)


@frappe.whitelist(allow_guest=True)
def get_info() -> dict:
	return {
		"schema_version": SCHEMA_VERSION,
		"name": "Builder Hub",
		"extensions_api_version": EXTENSIONS_API_VERSION,
	}


@frappe.whitelist(allow_guest=True)
def get_catalog(protocol_version: int | str = 1) -> dict:
	return _get_catalog(_protocol_version(protocol_version), get_url())


@redis_cache(ttl=600)
def _get_catalog(protocol_version: int, base_url: str) -> dict:
	items = []
	for extension in frappe.get_all(
		"Hub Extension",
		filters={"status": ("in", ["Published", "Deprecated"])},
		fields=_extension_fields(),
		order_by="extension_name asc",
		ignore_permissions=True,
	):
		publisher = _publisher(extension.publisher)
		if not publisher or publisher.status != "Active":
			continue
		release = _latest_release(extension.name, protocol_version)
		if release:
			items.append(_catalog_item(extension, publisher, release, base_url))
	return {"schema_version": SCHEMA_VERSION, "extensions": items}


@frappe.whitelist(allow_guest=True)
def get_extension(extension_name: str, protocol_version: int | str = 1) -> dict:
	protocol_version = _protocol_version(protocol_version)
	extension = _public_extension(extension_name)
	publisher = _publisher(extension.publisher)
	if not publisher or publisher.status != "Active":
		_not_found()
	releases = frappe.get_all(
		"Hub Extension Release",
		filters={
			"extension": extension.name,
			"status": "Published",
			"protocol_version": ("<=", protocol_version),
		},
		fields=[
			"version",
			"protocol_version",
			"package_size",
			"package_sha256",
			"release_notes",
			"status",
			"published_on",
		],
		ignore_permissions=True,
	)
	releases.sort(key=lambda row: semver_key(row.version), reverse=True)
	latest = releases[0] if releases else None
	detail = _catalog_item(extension, publisher, latest, get_url(), include_latest=False)
	detail["readme"] = extension.readme or ""
	return {
		"schema_version": SCHEMA_VERSION,
		"extension": detail,
		"releases": [
			{
				"version": row.version,
				"protocol_version": row.protocol_version,
				"package_size": row.package_size,
				"package_sha256": row.package_sha256,
				"release_notes": row.release_notes or "",
				"status": row.status,
				"published_on": _iso(row.published_on),
			}
			for row in releases
		],
	}


@frappe.whitelist(allow_guest=True)
def get_extension_release(extension_name: str, version: str) -> dict:
	_validate_identity(extension_name, version)
	extension = _public_extension(extension_name)
	publisher = _publisher(extension.publisher)
	if not publisher or publisher.status != "Active":
		_not_found()
	name = f"{extension_name}@{version}"
	if not frappe.db.exists("Hub Extension Release", name):
		_not_found()
	release = frappe.get_doc("Hub Extension Release", name)
	if release.status != "Published":
		frappe.throw(_("This release is not available for installation."), frappe.PermissionError)
	return {
		"schema_version": SCHEMA_VERSION,
		"release": {
			"extension_name": release.extension,
			"version": release.version,
			"protocol_version": release.protocol_version,
			"manifest": _json(release.manifest, {}),
			"github_release_url": release.github_release_url,
			"package_url": release.package_url,
			"package_size": release.package_size,
			"package_sha256": release.package_sha256,
			"status": release.status,
			"published_on": _iso(release.published_on),
		},
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_release_status(releases: list[dict] | str) -> dict:
	releases = _json(releases, None)
	if not isinstance(releases, list):
		frappe.throw(_("releases must be a JSON list."))
	if len(releases) > 100:
		frappe.throw(_("At most 100 release identities can be checked at once."))
	results = []
	for identity in releases:
		if not isinstance(identity, dict):
			frappe.throw(_("Each release identity must be an object."))
		extension_name = identity.get("extension_name")
		version = identity.get("version")
		_validate_identity(extension_name, version)
		extension = frappe.db.get_value(
			"Hub Extension",
			extension_name,
			["name", "publisher", "status", "replacement"],
			as_dict=True,
		)
		release = frappe.db.get_value(
			"Hub Extension Release",
			f"{extension_name}@{version}",
			["status"],
			as_dict=True,
		)
		found = bool(extension and release)
		extension_status = extension.status if extension else None
		if extension:
			publisher_status = frappe.db.get_value("Hub Publisher", extension.publisher, "status")
			if publisher_status == "Blocked":
				extension_status = "Blocked"
		results.append(
			{
				"extension_name": extension_name,
				"version": version,
				"found": found,
				"extension_status": extension_status,
				"release_status": release.status if release else None,
				"replacement": extension.replacement if extension else None,
			}
		)
	return {"schema_version": SCHEMA_VERSION, "releases": results}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=60 * 60, methods="POST")
def request_publication(
	repository_url: str,
	categories: list[str] | str | None = None,
) -> dict:
	return submission.request_publication(repository_url, categories)


@frappe.whitelist()
def approve_publication_request(request_name: str, reason: str | None = None) -> dict:
	return submission.approve_publication_request(request_name, reason)


@frappe.whitelist()
def reject_publication_request(request_name: str, reason: str) -> dict:
	return submission.reject_publication_request(request_name, reason)


@frappe.whitelist()
def approve_first_release(release_name: str) -> dict:
	return publishing.approve_first_release(release_name)


@frappe.whitelist()
def reject_first_release(release_name: str, reason: str) -> dict:
	return publishing.reject_first_release(release_name, reason)


@frappe.whitelist()
def request_release_check(extension_name: str) -> dict:
	from builder_hub.extensions.tasks import request_release_check as enqueue_check

	return enqueue_check(extension_name)


@frappe.whitelist()
def yank_release(release_name: str) -> dict:
	return publishing.yank_release(release_name)


@frappe.whitelist()
def block_release(release_name: str) -> dict:
	return publishing.block_release(release_name)


@frappe.whitelist()
def block_extension(extension_name: str) -> dict:
	return publishing.block_extension(extension_name)


@frappe.whitelist()
def block_publisher(publisher_id: str) -> dict:
	return publishing.block_publisher(publisher_id)


def _public_extension(extension_name: str):
	if not isinstance(extension_name, str) or not EXTENSION_NAME_PATTERN.fullmatch(extension_name):
		_not_found()
	row = frappe.db.get_value(
		"Hub Extension",
		extension_name,
		_extension_fields(),
		as_dict=True,
	)
	if not row or row.status not in {"Published", "Deprecated"}:
		_not_found()
	return row


def _latest_release(extension_name: str, protocol_version: int):
	releases = frappe.get_all(
		"Hub Extension Release",
		filters={
			"extension": extension_name,
			"status": "Published",
			"protocol_version": ("<=", protocol_version),
		},
		fields=["version", "protocol_version", "package_size", "package_sha256", "published_on"],
		ignore_permissions=True,
	)
	return max(releases, key=lambda row: semver_key(row.version), default=None)


def _catalog_item(extension, publisher, release, base_url: str, *, include_latest: bool = True) -> dict:
	item = {
		"name": extension.extension_name,
		"label": extension.label,
		"description": extension.description,
		"publisher": {
			"id": publisher.publisher_id,
			"name": publisher.display_name,
			"verified": bool(publisher.verified),
		},
		"repository_url": extension.repository_url,
		"license": extension.license,
		"categories": _json(extension.categories, []),
		"icon_url": _absolute_url(extension.icon, base_url),
		"status": extension.status,
		"replacement": extension.replacement or None,
	}
	if include_latest:
		item["latest_release"] = {
			"version": release.version,
			"protocol_version": release.protocol_version,
			"package_size": release.package_size,
			"package_sha256": release.package_sha256,
			"published_on": _iso(release.published_on),
		}
	return item


def _publisher(name: str):
	return frappe.db.get_value(
		"Hub Publisher",
		name,
		["publisher_id", "display_name", "verified", "status"],
		as_dict=True,
	)


def _extension_fields() -> list[str]:
	return [
		"name",
		"extension_name",
		"publisher",
		"label",
		"description",
		"readme",
		"repository_url",
		"license",
		"categories",
		"icon",
		"status",
		"replacement",
	]


def _protocol_version(value) -> int:
	try:
		value = int(value)
	except TypeError, ValueError:
		frappe.throw(_("protocol_version must be a positive integer."))
	if value < 1 or value > 2_147_483_647:
		frappe.throw(_("protocol_version must be a positive integer."))
	return value


def _validate_identity(extension_name, version) -> None:
	if not isinstance(extension_name, str) or not EXTENSION_NAME_PATTERN.fullmatch(extension_name):
		frappe.throw(_("Invalid extension name."))
	if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
		frappe.throw(_("Invalid release version."))


def _absolute_url(path: str | None, base_url: str) -> str | None:
	if path and path.startswith("/"):
		return base_url.rstrip("/") + path
	return path


def _iso(value) -> str | None:
	if not value:
		return None
	date = get_datetime(value)
	if date.tzinfo is None:
		date = date.replace(tzinfo=UTC)
	return date.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json(value, default):
	if value in (None, ""):
		return default
	if isinstance(value, str):
		try:
			return json.loads(value)
		except json.JSONDecodeError:
			return default
	return value


def _not_found():
	frappe.throw(_("Extension or release not found."), frappe.DoesNotExistError)
