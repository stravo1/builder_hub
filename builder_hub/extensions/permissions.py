from __future__ import annotations

import frappe


def publisher_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if _maintainer(user):
		return ""
	return f"`tabBuilder Hub Publisher`.`owner_user` = {frappe.db.escape(user)}"


def publisher_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	user = user or frappe.session.user
	return _maintainer(user) or doc.owner_user == user


def extension_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if _maintainer(user):
		return ""
	return (
		"exists (select 1 from `tabBuilder Hub Publisher` p "
		f"where p.name = `tabBuilder Hub Extension`.publisher and p.owner_user = {frappe.db.escape(user)})"
	)


def extension_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	user = user or frappe.session.user
	return (
		_maintainer(user) or frappe.db.get_value("Builder Hub Publisher", doc.publisher, "owner_user") == user
	)


def release_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if _maintainer(user):
		return ""
	return (
		"exists (select 1 from `tabBuilder Hub Extension` e "
		"join `tabBuilder Hub Publisher` p on p.name = e.publisher "
		"where e.name = `tabBuilder Hub Extension Release`.extension "
		f"and p.owner_user = {frappe.db.escape(user)})"
	)


def release_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
	user = user or frappe.session.user
	if _maintainer(user):
		return True
	publisher = frappe.db.get_value("Builder Hub Extension", doc.extension, "publisher")
	return frappe.db.get_value("Builder Hub Publisher", publisher, "owner_user") == user


def _maintainer(user: str) -> bool:
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	return bool(roles.intersection({"System Manager", "Builder Hub Maintainer"}))
