"""Scheduled and maintainer-requested GitHub release checks."""

from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from builder_hub.extensions.github import GitHubClient, get_repository_contract, validate_repository
from builder_hub.extensions.protocol import ProtocolValidationError, semver_key
from builder_hub.extensions.publishing import import_release, is_maintainer

MANUAL_CHECK_INTERVAL = 15 * 60


def check_releases() -> None:
	"""Hourly scheduler entry: queue one deduplicated job for each active listing."""
	for extension in frappe.get_all(
		"Builder Hub Extension",
		filters={"status": ("in", ["Published", "Deprecated"])},
		fields=["name", "github_repository_id"],
		ignore_permissions=True,
	):
		enqueue_repository_check(extension.name, extension.github_repository_id)


def enqueue_repository_check(extension_name: str, repository_id: str | None = None):
	repository_id = repository_id or frappe.db.get_value(
		"Builder Hub Extension", extension_name, "github_repository_id"
	)
	if not repository_id:
		return None
	job_suffix = hashlib.sha256(str(repository_id).encode()).hexdigest()[:24]
	return frappe.enqueue(
		"builder_hub.extensions.tasks.check_repository",
		queue="short",
		timeout=180,
		job_id=f"builder-hub-extension-{job_suffix}",
		deduplicate=True,
		extension_name=extension_name,
	)


def check_repository(extension_name: str, *, client: GitHubClient | None = None) -> dict:
	"""Check one stable repository ID and import every new declared release."""
	_metric("checks")
	extension = frappe.get_doc("Builder Hub Extension", extension_name)
	if extension.status not in {"Published", "Deprecated"}:
		return {"extension": extension.name, "checked": False, "reason": "inactive_listing"}
	publisher = frappe.get_doc("Builder Hub Publisher", extension.publisher)
	if publisher.status != "Active":
		return {"extension": extension.name, "checked": False, "reason": "inactive_publisher"}

	client = client or GitHubClient()
	try:
		repository = client.get_repository(extension.github_repository_id)
		validate_repository(repository, publisher.github_account_id)
		response = client.get_releases(repository, extension.github_etag)
		if response.status_code == 304:
			extension.db_set("last_checked_on", now_datetime(), update_modified=False)
			return {"extension": extension.name, "checked": True, "unchanged": True, "imported": []}

		contract = get_repository_contract(client, repository, extension.license)
		available = {
			item.get("tag_name"): item
			for item in response.data or []
			if not item.get("draft") and not item.get("prerelease")
		}
		imported = []
		for version in sorted(contract["versions"], key=semver_key):
			if version not in available:
				continue
			existing_status = frappe.db.get_value(
				"Builder Hub Extension Release", f"{extension.name}@{version}", "status"
			)
			if existing_status in {"Published", "Pending Review", "Yanked", "Blocked"}:
				continue
			release = import_release(
				extension.name,
				version,
				client=client,
				repository=repository,
				contract=contract,
				release_data=available[version],
			)
			imported.append({"version": version, "status": release.status})

		extension.db_set(
			{"last_checked_on": now_datetime(), "github_etag": response.etag or extension.github_etag},
			update_modified=False,
		)
		return {"extension": extension.name, "checked": True, "unchanged": False, "imported": imported}
	except ProtocolValidationError as error:
		_metric("github_rate_limits" if error.code == "github_rate_limited" else "check_failures")
		extension.db_set("last_checked_on", now_datetime(), update_modified=False)
		frappe.logger("builder_hub.extensions").warning(
			"Extension release check failed for %s: %s (%s)", extension.name, error.message, error.code
		)
		return {"extension": extension.name, "checked": False, "error": error.as_dict()}


def request_release_check(extension_name: str) -> dict:
	if not is_maintainer():
		frappe.throw(_("A Builder Hub maintainer must request a release check."), frappe.PermissionError)
	extension = frappe.get_doc("Builder Hub Extension", extension_name)
	key = f"builder_hub:extensions:manual_check:{extension.name}"
	if frappe.cache.get_value(key):
		frappe.throw(_("A release check was requested recently. Try again later."))
	frappe.cache.set_value(key, 1, expires_in_sec=MANUAL_CHECK_INTERVAL)
	enqueue_repository_check(extension.name, extension.github_repository_id)
	return {"extension": extension.name, "queued": True}


def _metric(name: str) -> None:
	try:
		frappe.cache.incrby(f"builder_hub:extensions:metrics:{name}", 1)
	except Exception:
		frappe.logger("builder_hub.extensions").debug("Could not increment extension metric", exc_info=True)
