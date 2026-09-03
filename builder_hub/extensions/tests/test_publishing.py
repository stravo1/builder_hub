from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

from builder_hub.extensions import publishing
from builder_hub.extensions.protocol import ProtocolValidationError, expected_package_name


class PackageClient:
	def __init__(self, package_path: Path):
		self.package_path = str(package_path)
		self.downloaded_paths = []

	def download_asset(self, url: str) -> str:
		self.downloaded_paths.append(self.package_path)
		return self.package_path


class PublishingTests(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.publisher_id = f"import-{frappe.generate_hash(length=8).lower()}"
		self.extension_name = f"{self.publisher_id}/icons"
		self.repository_id = frappe.generate_hash(length=12)
		self.owner_id = frappe.generate_hash(length=12)
		frappe.get_doc(
			{
				"doctype": "Builder Hub Publisher",
				"publisher_id": self.publisher_id,
				"display_name": "Import Publisher",
				"github_owner": self.publisher_id,
				"github_account_id": self.owner_id,
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Builder Hub Extension",
				"extension_name": self.extension_name,
				"publisher": self.publisher_id,
				"label": "Old label",
				"description": "Old description",
				"repository_url": f"https://github.com/{self.publisher_id}/icons",
				"github_repository_id": self.repository_id,
				"license": "MIT",
				"categories": "[]",
				"status": "Draft",
			}
		).insert(ignore_permissions=True)
		self.paths = []

	def tearDown(self):
		for path in self.paths:
			Path(path).unlink(missing_ok=True)
		frappe.db.delete("Builder Hub Extension Release", {"extension": self.extension_name})
		frappe.db.delete("Builder Hub Extension", self.extension_name)
		frappe.db.delete("Builder Hub Publisher", self.publisher_id)

	def manifest(self, version: str, **changes) -> dict:
		value = {
			"v": 1,
			"name": self.extension_name,
			"label": "Imported icons",
			"description": "Imported icon package.",
			"version": version,
			"entry": "main.js",
			"capabilities": ["context.read"],
		}
		value.update(changes)
		return value

	def package(self, manifest: dict) -> Path:
		file_descriptor, filename = tempfile.mkstemp(suffix=".builderext")
		os.close(file_descriptor)
		path = Path(filename)
		self.paths.append(str(path))
		with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
			archive.writestr("manifest.json", json.dumps(manifest))
			archive.writestr("main.js", "export const ready = true;")
		return path

	def repository(self) -> dict:
		return {
			"id": self.repository_id,
			"private": False,
			"archived": False,
			"disabled": False,
			"default_branch": "main",
			"owner": {"id": self.owner_id, "login": self.publisher_id},
			"license": {"spdx_id": "MIT"},
			"html_url": f"https://github.com/{self.publisher_id}/icons",
		}

	def contract(self, manifest: dict, versions: dict | None = None) -> dict:
		return {
			"manifest": manifest,
			"versions": versions or {manifest["version"]: manifest["v"]},
			"readme": "README <script>bad()</script> [bad](javascript:alert(1))",
		}

	def github_release(self, version: str, *, release_id="release-1", asset_id="asset-1") -> dict:
		return {
			"id": release_id,
			"tag_name": version,
			"html_url": f"https://github.com/{self.publisher_id}/icons/releases/tag/{version}",
			"draft": False,
			"prerelease": False,
			"body": "Notes <script>bad()</script> [bad](javascript:alert(1))",
			"assets": [
				{
					"id": asset_id,
					"name": expected_package_name(self.extension_name, version),
					"state": "uploaded",
					"browser_download_url": f"https://github.com/{self.publisher_id}/icons/releases/download/{version}/{expected_package_name(self.extension_name, version)}",
				}
			],
		}

	def import_version(self, version: str, *, first_release=False, manifest=None, versions=None):
		manifest = manifest or self.manifest(version)
		package = self.package(manifest)
		client = PackageClient(package)
		release = publishing.import_release(
			self.extension_name,
			version,
			first_release=first_release,
			client=client,
			repository=self.repository(),
			contract=self.contract(manifest, versions),
			release_data=self.github_release(version),
		)
		self.assertFalse(package.exists(), "temporary package must be deleted")
		return release

	def test_first_and_later_release_use_same_import_and_publication_flow(self):
		first = self.import_version("1.0.0", first_release=True)
		self.assertEqual(first.status, "Pending Review")
		self.assertEqual(len(first.package_sha256), 64)
		self.assertEqual(
			first.release_notes,
			"Notes <script>bad()</script> [bad](javascript:alert(1))",
		)
		self.assertEqual(
			frappe.db.get_value("Builder Hub Extension", self.extension_name, "label"), "Imported icons"
		)
		self.assertEqual(
			frappe.db.get_value("Builder Hub Extension", self.extension_name, "description"),
			"Imported icon package.",
		)
		self.assertEqual(
			frappe.db.get_value("Builder Hub Extension", self.extension_name, "readme"),
			"README <script>bad()</script> [bad](javascript:alert(1))",
		)

		frappe.db.set_value("Builder Hub Extension", self.extension_name, "status", "Pending Review")
		publishing.approve_first_release(first.name)
		self.assertEqual(frappe.db.get_value(first.doctype, first.name, "status"), "Published")

		later_manifest = self.manifest("1.1.0")
		later = self.import_version(
			"1.1.0",
			manifest=later_manifest,
			versions={"1.0.0": 1, "1.1.0": 1},
		)
		self.assertEqual(later.status, "Published")
		self.assertIsNotNone(later.published_on)

	def test_validation_failure_is_author_visible_and_temp_file_is_deleted(self):
		wrong_manifest = self.manifest("1.0.0", name=f"{self.publisher_id}/other")
		release = self.import_version("1.0.0", first_release=True, manifest=wrong_manifest)
		self.assertEqual(release.status, "Rejected")
		errors = json.loads(release.validation_errors)
		self.assertEqual(errors[0]["code"], "identity_mismatch")

	def test_existing_published_release_rejects_changed_asset_identity(self):
		first = self.import_version("1.0.0", first_release=True)
		frappe.db.set_value("Builder Hub Extension", self.extension_name, "status", "Pending Review")
		publishing.approve_first_release(first.name)
		changed = self.github_release("1.0.0", asset_id="replacement-asset")
		with self.assertRaises(ProtocolValidationError) as raised:
			publishing.import_release(
				self.extension_name,
				"1.0.0",
				repository=self.repository(),
				contract=self.contract(self.manifest("1.0.0")),
				release_data=changed,
			)
		self.assertEqual(raised.exception.code, "release_asset_changed")
