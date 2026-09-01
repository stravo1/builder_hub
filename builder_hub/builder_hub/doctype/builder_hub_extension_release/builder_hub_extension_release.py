from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

from builder_hub.extensions.protocol import SEMVER_PATTERN, validate_manifest

IMMUTABLE_FIELDS = (
	"extension",
	"version",
	"protocol_version",
	"manifest",
	"github_release_id",
	"github_release_url",
	"github_asset_id",
	"package_url",
	"package_sha256",
	"package_size",
	"release_notes",
	"published_on",
)


class BuilderHubExtensionRelease(Document):
	def autoname(self):
		self.name = f"{self.extension}@{self.version}"

	def validate(self):
		if not SEMVER_PATTERN.fullmatch(self.version or ""):
			frappe.throw(_("Release version must be a valid SemVer value."))
		if self.protocol_version and self.protocol_version < 1:
			frappe.throw(_("Protocol version must be a positive integer."))
		if self.manifest:
			manifest = validate_manifest(frappe.parse_json(self.manifest))
			if manifest["name"] != self.extension or manifest["version"] != self.version:
				frappe.throw(_("Release manifest identity does not match the record."))
		if self.package_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.package_sha256):
			frappe.throw(_("Package SHA-256 must contain 64 lowercase hexadecimal characters."))
		self._validate_immutable_publication()

	def _validate_immutable_publication(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or not before.published_on:
			return
		for fieldname in IMMUTABLE_FIELDS:
			if self.get(fieldname) != before.get(fieldname):
				frappe.throw(_("Published release metadata is immutable; only status may change."))

	def on_trash(self):
		if self.published_on:
			frappe.throw(_("Published release records cannot be deleted."))
