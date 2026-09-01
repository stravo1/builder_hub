from __future__ import annotations

import re
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

PUBLISHER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class BuilderHubPublisher(Document):
	def validate(self):
		if not PUBLISHER_ID_PATTERN.fullmatch(self.publisher_id or ""):
			frappe.throw(_("Publisher ID must contain lowercase letters, numbers, and hyphens only."))
		if self.website:
			url = urlparse(self.website)
			if url.scheme != "https" or not url.netloc or url.username or url.password:
				frappe.throw(_("Publisher website must be a public HTTPS URL."))
		self._validate_protected_changes()

	def _validate_protected_changes(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		if before.publisher_id != self.publisher_id:
			frappe.throw(_("Publisher IDs are permanent."))
		protected = ("owner_user", "github_owner", "github_account_id", "verified", "status")
		if (
			any(before.get(fieldname) != self.get(fieldname) for fieldname in protected)
			and not _is_maintainer()
		):
			frappe.throw(_("A Builder Hub maintainer must review publisher ownership changes."))

	def on_trash(self):
		frappe.throw(_("Publisher records are permanent and cannot be deleted."))


def _is_maintainer() -> bool:
	roles = set(frappe.get_roles())
	return frappe.session.user == "Administrator" or bool(
		roles.intersection({"System Manager", "Builder Hub Maintainer"})
	)
