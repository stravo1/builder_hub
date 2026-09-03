from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

from builder_hub.extensions import api
from builder_hub.extensions.protocol import expected_package_name
from builder_hub.extensions.submission import (
	approve_publication_request,
	reject_publication_request,
	request_publication,
)


class PublicationClient:
	def __init__(self, repository: dict, files: dict[str, bytes], release: dict, package: Path):
		self.repository = repository
		self.files = files
		self.release = release
		self.package = package

	def get_repository_by_name(self, owner: str, name: str) -> dict:
		return self.repository

	def get_repository_file(self, repository: dict, path: str) -> bytes:
		return self.files[path]

	def get_release(self, repository: dict, version: str) -> dict:
		return self.release

	def download_asset(self, url: str) -> str:
		file_descriptor, filename = tempfile.mkstemp(suffix=".builderext")
		os.close(file_descriptor)
		shutil.copyfile(self.package, filename)
		return filename


class PublicationRequestTests(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.temporary = tempfile.TemporaryDirectory()
		self.publisher_id = f"public-{frappe.generate_hash(length=8).lower()}"
		self.extension_name = f"{self.publisher_id}/icons"
		self.repository_id = frappe.generate_hash(length=12)
		self.owner_id = frappe.generate_hash(length=12)
		self.repository_url = f"https://github.com/{self.publisher_id}/builder-extension-icons"
		self.manifest = {
			"v": 1,
			"name": self.extension_name,
			"label": "Public icons",
			"description": "Insert public icons.",
			"version": "1.0.0",
			"entry": "main.js",
			"capabilities": ["block.update"],
		}
		self.package = Path(self.temporary.name) / "extension.builderext"
		with zipfile.ZipFile(self.package, "w", zipfile.ZIP_DEFLATED) as archive:
			archive.writestr("manifest.json", json.dumps(self.manifest))
			archive.writestr("main.js", "export const ready = true;")
		self.client = PublicationClient(
			self._repository(),
			{
				"manifest.json": json.dumps(self.manifest).encode(),
				"README.md": b"README <script>untrusted()</script>",
				"LICENSE": b"MIT License",
			},
			self._release(),
			self.package,
		)
		api._get_catalog.clear_cache()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Builder Hub Extension Release", {"extension": self.extension_name})
		frappe.db.delete("Builder Hub Extension", self.extension_name)
		frappe.db.delete("Builder Hub Publication Request", {"extension_name": self.extension_name})
		frappe.db.delete("Builder Hub Publisher", self.publisher_id)
		api._get_catalog.clear_cache()
		self.temporary.cleanup()

	def test_guest_request_and_admin_approval_publish_the_extension(self):
		frappe.set_user("Guest")
		result = request_publication(
			self.repository_url,
			"Public Publisher",
			["design"],
			client=self.client,
		)
		request = frappe.get_doc("Builder Hub Publication Request", result["request"])
		self.assertEqual(request.status, "Pending Review")
		self.assertIn("<script>", request.readme)
		self.assertFalse(frappe.db.exists("Builder Hub Publisher", self.publisher_id))
		self.assertFalse(frappe.db.exists("Builder Hub Extension", self.extension_name))

		frappe.set_user("Administrator")
		approved = approve_publication_request(request.name, client=self.client)
		self.assertEqual(approved["status"], "Published")
		self.assertEqual(frappe.db.get_value("Builder Hub Publisher", self.publisher_id, "status"), "Active")
		self.assertEqual(
			frappe.db.get_value("Builder Hub Extension", self.extension_name, "status"), "Published"
		)
		self.assertEqual(
			frappe.db.get_value("Builder Hub Publication Request", request.name, "status"), "Approved"
		)
		catalog = api.get_catalog(1)
		self.assertIn(self.extension_name, [item["name"] for item in catalog["extensions"]])

	def test_duplicate_repository_request_is_rejected(self):
		frappe.set_user("Guest")
		request_publication(self.repository_url, "Public Publisher", client=self.client)
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			request_publication(self.repository_url, "Public Publisher", client=self.client)

	def test_malformed_categories_are_rejected(self):
		frappe.set_user("Guest")
		with self.assertRaisesRegex(frappe.ValidationError, "Categories"):
			request_publication(
				self.repository_url,
				"Public Publisher",
				"not-json",
				client=self.client,
			)

	def test_existing_namespace_rejects_a_different_github_owner(self):
		frappe.get_doc(
			{
				"doctype": "Builder Hub Publisher",
				"publisher_id": self.publisher_id,
				"display_name": "Existing Publisher",
				"github_owner": "another-owner",
				"github_account_id": "another-account",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		frappe.set_user("Guest")
		with self.assertRaisesRegex(frappe.ValidationError, "different GitHub account"):
			request_publication(self.repository_url, "Public Publisher", client=self.client)

	def test_only_a_maintainer_can_reject_a_request(self):
		frappe.set_user("Guest")
		result = request_publication(self.repository_url, "Public Publisher", client=self.client)
		with self.assertRaises(frappe.PermissionError):
			reject_publication_request(result["request"], "Not suitable.")
		frappe.set_user("Administrator")
		response = reject_publication_request(result["request"], "Not suitable.")
		self.assertEqual(response["status"], "Rejected")

	def _repository(self) -> dict:
		return {
			"id": self.repository_id,
			"name": "builder-extension-icons",
			"private": False,
			"archived": False,
			"disabled": False,
			"default_branch": "main",
			"owner": {"id": self.owner_id, "login": self.publisher_id, "type": "User"},
			"license": {"spdx_id": "MIT"},
			"html_url": self.repository_url,
		}

	def _release(self) -> dict:
		package_name = expected_package_name(self.extension_name, "1.0.0")
		return {
			"id": frappe.generate_hash(length=12),
			"tag_name": "1.0.0",
			"html_url": f"{self.repository_url}/releases/tag/1.0.0",
			"draft": False,
			"prerelease": False,
			"body": "First release.",
			"assets": [
				{
					"id": frappe.generate_hash(length=12),
					"name": package_name,
					"state": "uploaded",
					"browser_download_url": f"{self.repository_url}/releases/download/1.0.0/{package_name}",
				}
			],
		}
