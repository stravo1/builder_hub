from __future__ import annotations

from unittest.mock import patch

import frappe
from builder.export_import_standard_page import sync_standard_builder_pages
from frappe.tests import IntegrationTestCase
from frappe.website.serve import get_response_content


class SubmitPageTests(IntegrationTestCase):
	def test_standard_page_serves_the_publication_form(self):
		# in developer mode, deleting a standard page also deletes its files in builder_files
		with patch.dict(frappe.conf, {"developer_mode": 0}):
			frappe.delete_doc("Builder Page", "extensions-submit", force=True, ignore_missing=True)
		sync_standard_builder_pages("builder_hub")
		frappe.set_user("Guest")

		html = get_response_content("extensions/submit")

		self.assertIn('name="repository_url"', html)
		self.assertIn('name="categories"', html)
		self.assertIn("data-publication-form", html)

	def test_page_script_submits_the_publication_form(self):
		script = frappe.get_doc("Builder Client Script", "extensions_submit_form")
		page_scripts = frappe.get_all(
			"Builder Page Client Script", filters={"parent": "extensions-submit"}, pluck="builder_script"
		)

		self.assertIn(script.name, page_scripts)
		self.assertIn("[data-publication-form]", script.script)
		self.assertIn("builder_hub.extensions.api.request_publication", script.script)

	def tearDown(self):
		frappe.set_user("Administrator")
