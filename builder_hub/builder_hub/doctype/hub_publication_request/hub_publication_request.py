from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

from builder_hub.extensions.protocol import EXTENSION_NAME_PATTERN, SEMVER_PATTERN


class HubPublicationRequest(Document):
	def validate(self):
		if not EXTENSION_NAME_PATTERN.fullmatch(self.extension_name or ""):
			frappe.throw(_("Extension name must use the lowercase publisher/name format."))
		if not SEMVER_PATTERN.fullmatch(self.version or ""):
			frappe.throw(_("Release version must be a valid SemVer value."))
		if self.publisher_id != self.extension_name.split("/", 1)[0]:
			frappe.throw(_("The publisher ID must match the extension namespace."))
		if not re.fullmatch(r"[0-9a-f]{64}", self.package_sha256 or ""):
			frappe.throw(_("Package SHA-256 must contain 64 lowercase hexadecimal characters."))

	def on_trash(self):
		if self.status == "Approved":
			frappe.throw(_("Approved publication requests cannot be deleted."))
