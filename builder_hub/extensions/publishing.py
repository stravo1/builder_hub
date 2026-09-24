"""Extension import, publication, moderation, and immutable state changes."""

from __future__ import annotations

import os
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import now_datetime

from builder_hub.extensions.github import (
	GitHubClient,
	get_repository_contract,
	validate_repository,
)
from builder_hub.extensions.icons import store_catalog_icon
from builder_hub.extensions.package import ValidatedPackage, validate_package
from builder_hub.extensions.protocol import (
	ProtocolValidationError,
	expected_package_name,
)

TRANSIENT_ERROR_CODES = frozenset(
	{
		"github_not_found",
		"github_rate_limited",
		"github_request_failed",
		"github_response_too_large",
		"invalid_github_response",
		"package_download_failed",
	}
)


@dataclass(frozen=True)
class ValidatedReleaseImport:
	release_data: dict
	asset: dict
	package: ValidatedPackage
	contract: dict


def import_release(
	extension_name: str,
	version: str,
	*,
	first_release: bool = False,
	client: GitHubClient | None = None,
	repository: dict | None = None,
	contract: dict | None = None,
	release_data: dict | None = None,
):
	"""Idempotently validate and record one exact GitHub release."""
	extension = frappe.get_doc("Hub Extension", extension_name)
	publisher = frappe.get_doc("Hub Publisher", extension.publisher)
	release = _get_or_create_release(extension.name, version)
	if release.status in {"Published", "Yanked", "Blocked"}:
		return _reuse_immutable_release(release, release_data)

	client = client or GitHubClient()
	try:
		validated_import = _validate_release_import(
			extension,
			publisher,
			release,
			version,
			client,
			repository,
			contract,
			release_data,
		)
		return _store_validated_release(extension, release, validated_import, first_release)
	except ProtocolValidationError as error:
		return _record_import_failure(release, error)


def validate_release_source(
	client: GitHubClient,
	repository: dict,
	contract: dict,
	extension_name: str,
	version: str,
	*,
	release_data: dict | None = None,
) -> ValidatedReleaseImport:
	"""Validate one repository release without creating registry records."""
	release_data = release_data or client.get_release(repository, version)
	asset = _validate_release(release_data, extension_name, version)
	package = _download_and_validate_package(client, asset, extension_name, version)
	_validate_current_manifest(package, contract)
	return ValidatedReleaseImport(release_data, asset, package, contract)


def import_validated_release(
	extension_name: str,
	validated_import: ValidatedReleaseImport,
	*,
	first_release: bool = False,
):
	"""Store a release that was validated in the current request."""
	extension = frappe.get_doc("Hub Extension", extension_name)
	release = _get_or_create_release(extension.name, validated_import.package.manifest["version"])
	if release.status in {"Published", "Yanked", "Blocked"}:
		return release
	return _store_validated_release(extension, release, validated_import, first_release)


def _reuse_immutable_release(release, release_data: dict | None):
	if release_data:
		asset = _validate_release(release_data, release.extension, release.version)
		_assert_unchanged_asset(release, release_data, asset)
	return release


def _validate_release_import(
	extension,
	publisher,
	release,
	version: str,
	client: GitHubClient,
	repository: dict | None,
	contract: dict | None,
	release_data: dict | None,
) -> ValidatedReleaseImport:
	repository = repository or client.get_repository(extension.github_repository_id)
	validate_repository(repository, publisher.github_account_id)
	if str(repository.get("id")) != str(extension.github_repository_id):
		raise ProtocolValidationError(
			"repository_mismatch", "GitHub returned a different repository identity."
		)
	contract = contract or get_repository_contract(client, repository, extension.license)
	validated_import = validate_release_source(
		client,
		repository,
		contract,
		extension.name,
		version,
		release_data=release_data,
	)
	_assert_unchanged_asset(release, validated_import.release_data, validated_import.asset)
	return validated_import


def _store_validated_release(extension, release, validated_import, first_release: bool):
	_apply_validated_release(
		release,
		validated_import.release_data,
		validated_import.asset,
		validated_import.package,
		validated_import.contract,
		first_release,
	)
	if validated_import.package.icon_content:
		icon_url = store_catalog_icon(validated_import.package.icon_content)
		if extension.icon != icon_url:
			extension.db_set("icon", icon_url, update_modified=False)
	_update_listing_from_contract(extension, validated_import.contract)
	return release.reload()


def _download_and_validate_package(
	client: GitHubClient, asset: dict, extension_name: str, version: str
) -> ValidatedPackage:
	temporary_path = client.download_asset(asset["browser_download_url"])
	try:
		return validate_package(
			temporary_path,
			expected_name=extension_name,
			expected_version=version,
		)
	finally:
		if os.path.exists(temporary_path):
			os.unlink(temporary_path)


def _validate_current_manifest(package: ValidatedPackage, contract: dict) -> None:
	if (
		package.manifest["version"] == contract["manifest"]["version"]
		and package.manifest != contract["manifest"]
	):
		raise ProtocolValidationError(
			"root_manifest_mismatch", "The package manifest does not match the root manifest.json."
		)


def _record_import_failure(release, error: ProtocolValidationError):
	transient = error.code in TRANSIENT_ERROR_CODES
	_metric(_failure_metric(error.code, transient))
	if not release.published_on:
		release.status = "Validating" if transient else "Rejected"
		release.validation_errors = frappe.as_json([error.as_dict()])
		release.save(ignore_permissions=True)
	return release


def _failure_metric(error_code: str, transient: bool) -> str:
	if error_code == "github_rate_limited":
		return "github_rate_limits"
	return "check_failures" if transient else "validation_failures"


def approve_first_release(release_name: str) -> dict:
	_require_maintainer()
	release = frappe.get_doc("Hub Extension Release", release_name)
	extension = frappe.get_doc("Hub Extension", release.extension)
	if release.status != "Pending Review" or extension.status != "Pending Review":
		frappe.throw(_("Only a pending first release can be published."))
	_set_status(extension, "Published")
	release.published_on = now_datetime()
	_set_status(release, "Published", save=True)
	clear_public_caches()
	return {"extension": extension.name, "release": release.name, "status": "Published"}


def reject_first_release(release_name: str, reason: str) -> dict:
	_require_maintainer()
	if not reason or not reason.strip():
		frappe.throw(_("A rejection reason is required."))
	release = frappe.get_doc("Hub Extension Release", release_name)
	if release.status != "Pending Review":
		frappe.throw(_("Only a pending release can be rejected."))
	release.validation_errors = frappe.as_json([{"code": "maintainer_rejected", "message": reason.strip()}])
	_set_status(release, "Rejected", save=True)
	return {"release": release.name, "status": release.status}


def yank_release(release_name: str) -> dict:
	_require_maintainer()
	release = frappe.get_doc("Hub Extension Release", release_name)
	if release.status != "Published":
		frappe.throw(_("Only a published release can be yanked."))
	_set_status(release, "Yanked", save=True)
	clear_public_caches()
	return {"release": release.name, "status": release.status}


def block_release(release_name: str) -> dict:
	_require_maintainer()
	release = frappe.get_doc("Hub Extension Release", release_name)
	_set_status(release, "Blocked", save=True)
	clear_public_caches()
	return {"release": release.name, "status": release.status}


def block_extension(extension_name: str) -> dict:
	_require_maintainer()
	extension = frappe.get_doc("Hub Extension", extension_name)
	_set_status(extension, "Blocked")
	for release_name in frappe.get_all(
		"Hub Extension Release", filters={"extension": extension.name}, pluck="name"
	):
		release = frappe.get_doc("Hub Extension Release", release_name)
		_set_status(release, "Blocked", save=True)
	clear_public_caches()
	return {"extension": extension.name, "status": extension.status}


def block_publisher(publisher_id: str) -> dict:
	_require_maintainer()
	publisher = frappe.get_doc("Hub Publisher", publisher_id)
	_set_status(publisher, "Blocked")
	for extension_name in frappe.get_all(
		"Hub Extension", filters={"publisher": publisher.name}, pluck="name"
	):
		block_extension(extension_name)
	clear_public_caches()
	return {"publisher": publisher.name, "status": publisher.status}


def clear_public_caches() -> None:
	frappe.cache.delete_keys("builder_hub.extensions.api._get_catalog")


def is_maintainer() -> bool:
	roles = set(frappe.get_roles())
	return frappe.session.user == "Administrator" or bool(
		roles.intersection({"System Manager", "Builder Hub Maintainer"})
	)


def _apply_validated_release(
	release,
	release_data: dict,
	asset: dict,
	validated: ValidatedPackage,
	contract: dict,
	first_release: bool,
) -> None:
	release.protocol_version = validated.manifest["v"]
	release.manifest = frappe.as_json(validated.manifest)
	release.github_release_id = str(release_data["id"])
	release.github_release_url = release_data["html_url"]
	release.github_asset_id = str(asset["id"])
	release.package_url = asset["browser_download_url"]
	release.package_sha256 = validated.package_sha256
	release.package_size = validated.package_size
	release.release_notes = release_data.get("body") or ""
	release.validation_errors = frappe.as_json([])
	if first_release:
		release.status = "Pending Review"
	else:
		extension_status = frappe.db.get_value("Hub Extension", release.extension, "status")
		if extension_status not in {"Published", "Deprecated"}:
			raise ProtocolValidationError(
				"listing_not_published", "Later releases require a published listing."
			)
		release.status = "Published"
		release.published_on = now_datetime()
	release.save(ignore_permissions=True)
	if release.status == "Published":
		clear_public_caches()


def _update_listing_from_contract(extension, contract: dict) -> None:
	manifest = contract["manifest"]
	extension.label = manifest["label"]
	extension.description = manifest["description"]
	extension.readme = contract["readme"]
	extension.save(ignore_permissions=True)


def _validate_release(release: dict, extension_name: str, version: str) -> dict:
	if release.get("tag_name") != version:
		raise ProtocolValidationError(
			"release_tag_mismatch", "GitHub release tag must exactly equal the SemVer version."
		)
	if release.get("draft") or release.get("prerelease"):
		raise ProtocolValidationError(
			"unpublished_release", "Draft and prerelease GitHub releases are not accepted."
		)
	expected = expected_package_name(extension_name, version)
	assets = [asset for asset in release.get("assets") or [] if asset.get("name") == expected]
	if len(assets) != 1:
		raise ProtocolValidationError(
			"invalid_release_asset", f"The release must contain exactly one {expected} asset."
		)
	asset = assets[0]
	if asset.get("state") not in {None, "uploaded"} or not asset.get("browser_download_url"):
		raise ProtocolValidationError(
			"invalid_release_asset", "The GitHub release asset is not ready for download."
		)
	return asset


def _assert_unchanged_asset(release, release_data: dict, asset: dict) -> None:
	if not release.github_asset_id:
		return
	values = {
		"github_release_id": str(release_data["id"]),
		"github_asset_id": str(asset["id"]),
		"package_url": asset["browser_download_url"],
	}
	for fieldname, value in values.items():
		if release.get(fieldname) and release.get(fieldname) != value:
			raise ProtocolValidationError(
				"release_asset_changed", "An existing release asset cannot be replaced."
			)


def _get_or_create_release(extension_name: str, version: str):
	name = f"{extension_name}@{version}"
	if frappe.db.exists("Hub Extension Release", name):
		release = frappe.get_doc("Hub Extension Release", name)
		if not release.published_on:
			release.status = "Validating"
			release.validation_errors = frappe.as_json([])
			release.save(ignore_permissions=True)
		return release
	return frappe.get_doc(
		{
			"doctype": "Hub Extension Release",
			"extension": extension_name,
			"version": version,
			"protocol_version": 1,
			"status": "Validating",
		}
	).insert(ignore_permissions=True)


def _set_status(doc, status: str, *, save: bool = False) -> None:
	doc.status = status
	if save:
		doc.save(ignore_permissions=True)
	else:
		doc.db_set("status", status)


def _require_maintainer() -> None:
	if not is_maintainer():
		frappe.throw(_("A Builder Hub maintainer must perform this action."), frappe.PermissionError)


def _metric(name: str) -> None:
	try:
		frappe.cache.incrby(f"builder_hub:extensions:metrics:{name}", 1)
	except Exception:
		frappe.logger("builder_hub.extensions").debug("Could not increment extension metric", exc_info=True)
