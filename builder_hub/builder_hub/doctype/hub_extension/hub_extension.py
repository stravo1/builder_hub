from __future__ import annotations

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from builder_hub.extensions.protocol import EXTENSION_NAME_PATTERN


class HubExtension(Document):
	def validate(self):
		if not EXTENSION_NAME_PATTERN.fullmatch(self.extension_name or ""):
			frappe.throw(_("Extension name must use the lowercase publisher/name format."))
		if self.publisher and self.extension_name.split("/", 1)[0] != self.publisher:
			frappe.throw(_("Extension name must start with its publisher ID."))
		url = urlparse(self.repository_url or "")
		if url.scheme != "https" or url.hostname != "github.com" or len(url.path.strip("/").split("/")) != 2:
			frappe.throw(_("Repository URL must identify one public GitHub repository."))
		if self.replacement == self.name:
			frappe.throw(_("An extension cannot replace itself."))
		if not self.is_new():
			before = self.get_doc_before_save()
			if before and before.extension_name != self.extension_name:
				frappe.throw(_("Extension names are permanent."))
			protected = (
				"publisher",
				"repository_url",
				"github_repository_id",
				"license",
				"icon",
				"replacement",
				"status",
			)
			if (
				before
				and any(before.get(fieldname) != self.get(fieldname) for fieldname in protected)
				and not _is_maintainer()
			):
				frappe.throw(_("A Builder Hub maintainer must review protected listing changes."))

	def on_trash(self):
		frappe.throw(_("Extension records are permanent. Set the listing to Removed instead."))


def _is_maintainer() -> bool:
	roles = set(frappe.get_roles())
	return frappe.session.user == "Administrator" or bool(
		roles.intersection({"System Manager", "Builder Hub Maintainer"})
	)
