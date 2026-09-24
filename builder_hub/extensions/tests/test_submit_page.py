from __future__ import annotations

import frappe
from builder.export_import_standard_page import sync_standard_builder_pages
from frappe.tests import IntegrationTestCase
from frappe.website.serve import get_response_content


class SubmitPageTests(IntegrationTestCase):
	def test_standard_page_serves_the_publication_form(self):
		frappe.delete_doc("Builder Page", "extensions-submit", force=True, ignore_missing=True)
		sync_standard_builder_pages("builder_hub")
		frappe.set_user("Guest")

		html = get_response_content("extensions/submit")

		self.assertIn('name="repository_url"', html)
		self.assertIn('name="categories"', html)
		self.assertIn("builder_hub.extensions.api.request_publication", html)

	def tearDown(self):
		frappe.set_user("Administrator")
