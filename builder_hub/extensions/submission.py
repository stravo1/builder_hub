"""Authenticated first-publication repository submission."""

from __future__ import annotations

import re

import frappe
from frappe import _

from builder_hub.extensions.github import (
	GitHubClient,
	get_repository_contract,
	get_validated_repository_url,
	parse_repository_url,
	validate_repository,
)
from builder_hub.extensions.protocol import ProtocolValidationError
from builder_hub.extensions.publishing import import_release, is_maintainer

CATEGORY = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")


def submit_repository(
	repository_url: str,
	publisher_id: str,
	license_id: str,
	categories: list[str] | str | None = None,
	*,
	client: GitHubClient | None = None,
) -> dict:
	_require_signed_in()
	publisher = _get_submission_publisher(publisher_id)
	client = client or GitHubClient()
	repository = _get_submission_repository(client, repository_url, publisher)
	contract = get_repository_contract(client, repository, license_id)
	manifest = contract["manifest"]
	_validate_new_listing(manifest, publisher_id, repository)
	extension = _create_listing(
		publisher_id, license_id, _validate_categories(categories), repository, contract
	)
	release = import_release(
		extension.name,
		manifest["version"],
		first_release=True,
		client=client,
		repository=repository,
		contract=contract,
	)
	if release.status == "Pending Review":
		extension.db_set("status", "Pending Review")
	return {
		"extension": extension.name,
		"release": release.name,
		"status": release.status,
		"validation_errors": _parse_json(release.validation_errors, []),
	}


def _create_listing(
	publisher_id: str,
	license_id: str,
	categories: list[str],
	repository: dict,
	contract: dict,
):
	manifest = contract["manifest"]
	return frappe.get_doc(
		{
			"doctype": "Builder Hub Extension",
			"extension_name": manifest["name"],
			"publisher": publisher_id,
			"label": manifest["label"],
			"description": manifest["description"],
			"readme": contract["readme"],
			"repository_url": get_validated_repository_url(repository),
			"github_repository_id": str(repository["id"]),
			"license": license_id,
			"categories": frappe.as_json(categories),
			"status": "Draft",
		}
	).insert(ignore_permissions=True)


def _get_submission_publisher(publisher_id: str):
	publisher = frappe.get_doc("Builder Hub Publisher", publisher_id)
	if publisher.owner_user != frappe.session.user and not is_maintainer():
		frappe.throw(_("You do not own this publisher."), frappe.PermissionError)
	if publisher.status != "Active":
		frappe.throw(_("The publisher must be active before submitting an extension."))
	return publisher


def _get_submission_repository(client: GitHubClient, url: str, publisher):
	try:
		owner, repository_name = parse_repository_url(url)
	except ProtocolValidationError as error:
		frappe.throw(_(error.message))
	repository = client.get_repository_by_name(owner, repository_name)
	validate_repository(repository, publisher.github_account_id)
	if repository.get("owner", {}).get("login", "").lower() != (publisher.github_owner or "").lower():
		frappe.throw(_("The repository owner does not match the publisher GitHub login."))
	_verify_connected_github_account(client, repository)
	return repository


def _validate_new_listing(manifest: dict, publisher_id: str, repository: dict) -> None:
	if manifest["name"].split("/", 1)[0] != publisher_id:
		frappe.throw(_("The root manifest does not use the selected publisher namespace."))
	if frappe.db.exists("Builder Hub Extension", manifest["name"]):
		frappe.throw(_("This extension name has already been used."))
	if frappe.db.exists("Builder Hub Extension", {"github_repository_id": str(repository["id"])}):
		frappe.throw(_("This GitHub repository has already been submitted."))


def _verify_connected_github_account(client: GitHubClient, repository: dict) -> None:
	account = frappe.db.get_value(
		"User Social Login",
		{"parent": frappe.session.user, "parenttype": "User", "provider": "github"},
		["userid", "username"],
		as_dict=True,
	)
	if not account:
		frappe.throw(_("Connect your GitHub account before submitting an extension."))
	owner = repository.get("owner") or {}
	if owner.get("type") != "Organization":
		if str(owner.get("id")) != str(account.userid):
			frappe.throw(_("The connected GitHub account does not own this repository."))
		return
	_verify_organization_permission(client, repository, account)


def _verify_organization_permission(client: GitHubClient, repository: dict, account) -> None:
	if not account.username:
		frappe.throw(_("The connected GitHub account has no username."))
	permission = client.get_repository_permission(repository, account.username)
	returned_user_id = str((permission.get("user") or {}).get("id"))
	role = permission.get("role_name") or permission.get("permission")
	if returned_user_id != str(account.userid) or role not in {"admin", "maintain", "write"}:
		frappe.throw(_("Your GitHub account needs write access to the organization repository."))


def _validate_categories(categories: list[str] | str | None) -> list[str]:
	categories = _parse_json(categories, [])
	if not isinstance(categories, list) or len(categories) > 20 or len(categories) != len(set(categories)):
		frappe.throw(_("Categories must be a unique list with at most 20 values."))
	if any(not isinstance(item, str) or not CATEGORY.fullmatch(item) for item in categories):
		frappe.throw(_("Each category must be a lowercase ID."))
	return categories


def _parse_json(value, default):
	if value in (None, ""):
		return default
	if isinstance(value, str):
		try:
			return frappe.parse_json(value)
		except ValueError:
			return default
	return value


def _require_signed_in() -> None:
	if frappe.session.user == "Guest":
		frappe.throw(_("Sign in to submit an extension."), frappe.PermissionError)
