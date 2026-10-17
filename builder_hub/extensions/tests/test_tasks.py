from frappe.tests import UnitTestCase

from builder_hub.extensions.tasks import get_release_index


class ReleaseDiscoveryTests(UnitTestCase):
	def test_indexes_only_published_semver_releases(self):
		releases = [
			{"tag_name": "1.1.0"},
			{"tag_name": "documentation"},
			{"tag_name": "2.0.0", "draft": True},
			{"tag_name": "3.0.0", "prerelease": True},
			{"tag_name": "1.0.0"},
		]

		self.assertEqual(list(get_release_index(releases)), ["1.0.0", "1.1.0"])
