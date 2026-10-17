from __future__ import annotations

import stat
from pathlib import Path

from frappe.tests import IntegrationTestCase
from frappe.utils import get_files_path

from builder_hub.extensions.icons import store_catalog_icon

ICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>'


class CatalogIconTests(IntegrationTestCase):
	def tearDown(self):
		digest = store_catalog_icon(ICON).rsplit("/", 1)[1]
		Path(get_files_path("extension-icons"), digest).unlink(missing_ok=True)

	def test_stored_icon_is_world_readable_for_the_web_server(self):
		url = store_catalog_icon(ICON)
		self.assertRegex(url, r"^/files/extension-icons/[0-9a-f]{64}\.svg$")

		directory = Path(get_files_path("extension-icons"))
		path = directory / url.rsplit("/", 1)[1]
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
		self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)

	def test_second_call_repairs_permissions_on_an_existing_file(self):
		first = store_catalog_icon(ICON)
		path = Path(get_files_path("extension-icons"), first.rsplit("/", 1)[1])
		path.chmod(0o600)
		self.assertEqual(store_catalog_icon(ICON), first)
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
