from __future__ import annotations

import json
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from builder_hub.extensions.package import validate_package, validate_svg
from builder_hub.extensions.protocol import (
	ProtocolValidationError,
	expected_package_name,
	semver_key,
	validate_manifest,
)


def manifest(**changes):
	value = {
		"v": 1,
		"name": "acme/icons",
		"label": "Icon Library",
		"description": "Add an icon library to Builder.",
		"version": "1.2.0",
		"entry": "main.js",
		"icon": "icon.svg",
		"capabilities": ["context.read", "block.update"],
	}
	value.update(changes)
	return value


class ProtocolTests(unittest.TestCase):
	def test_accepts_exact_v1_manifest(self):
		self.assertEqual(validate_manifest(manifest()), manifest())

	def test_rejects_every_missing_required_field(self):
		for fieldname in ("v", "name", "label", "description", "version", "entry", "capabilities"):
			value = manifest()
			del value[fieldname]
			with (
				self.subTest(fieldname=fieldname),
				self.assertRaisesRegex(ProtocolValidationError, fieldname),
			):
				validate_manifest(value)

	def test_rejects_unknown_network_field_and_capability(self):
		with self.assertRaisesRegex(ProtocolValidationError, "network"):
			validate_manifest(manifest(network={"access": True}))
		with self.assertRaisesRegex(ProtocolValidationError, "Unsupported"):
			validate_manifest(manifest(capabilities=["network.access"]))

	def test_rejects_wrong_identity_entry_icon_and_duplicate_capability(self):
		invalid = (
			manifest(name="Acme/icons"),
			manifest(version="v1.2.0"),
			manifest(entry="dist/main.js"),
			manifest(icon="assets/icon.svg"),
			manifest(capabilities=["context.read", "context.read"]),
		)
		for value in invalid:
			with self.subTest(value=value), self.assertRaises(ProtocolValidationError):
				validate_manifest(value)

	def test_semver_precedence_and_package_name(self):
		values = ["1.0.0", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-alpha"]
		self.assertEqual(
			sorted(values, key=semver_key), ["1.0.0-alpha", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0"]
		)
		self.assertEqual(expected_package_name("acme/icons", "1.2.0"), "acme-icons-1.2.0.builderext")


class PackageTests(unittest.TestCase):
	def setUp(self):
		self.temporary = tempfile.TemporaryDirectory()

	def tearDown(self):
		self.temporary.cleanup()

	def package(self, files=None, infos=None) -> Path:
		files = files or {
			"manifest.json": json.dumps(manifest()).encode(),
			"main.js": b"export const feature = true;",
			"icon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>',
		}
		path = Path(self.temporary.name) / "extension.builderext"
		with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
			for name, content in files.items():
				archive.writestr(name, content)
			for info, content in infos or []:
				archive.writestr(info, content)
		return path

	def assert_code(self, code, path):
		with self.assertRaises(ProtocolValidationError) as raised:
			validate_package(path, expected_name="acme/icons", expected_version="1.2.0")
		self.assertEqual(raised.exception.code, code)

	def test_accepts_valid_package_and_hashes_package_bytes(self):
		path = self.package()
		result = validate_package(path, expected_name="acme/icons", expected_version="1.2.0")
		self.assertEqual(result.manifest, manifest())
		self.assertEqual(len(result.package_sha256), 64)
		self.assertEqual(result.package_size, path.stat().st_size)
		self.assertTrue(result.icon_content.startswith(b"<svg"))

	def test_rejects_missing_required_file_and_unsupported_suffix(self):
		self.assert_code("missing_required_file", self.package({"manifest.json": json.dumps(manifest())}))
		self.assert_code(
			"unsupported_file",
			self.package({"manifest.json": json.dumps(manifest()), "main.js": "", "run.exe": "x"}),
		)

	def test_rejects_every_file_outside_the_three_file_contract(self):
		base = {
			"manifest.json": json.dumps(manifest(icon=None)),
			"main.js": "export const ready = true;",
		}
		self.assert_code("unexpected_file", self.package({**base, "chunk.js": "export {};"}))
		self.assert_code("unexpected_file", self.package({**base, "assets/": ""}))

	def test_rejects_traversal_absolute_windows_and_null_paths(self):
		base = {"manifest.json": json.dumps(manifest(icon=None)), "main.js": ""}
		for unsafe in ("../x.js", "/x.js", "C:\\x.js"):
			with self.subTest(path=unsafe):
				self.assert_code("unsafe_path", self.package({**base, unsafe: "x"}))

	def test_rejects_symlink(self):
		info = zipfile.ZipInfo("linked.js")
		info.create_system = 3
		info.external_attr = (stat.S_IFLNK | 0o777) << 16
		self.assert_code(
			"symlink",
			self.package(
				{"manifest.json": json.dumps(manifest(icon=None)), "main.js": ""}, [(info, "main.js")]
			),
		)

	def test_rejects_duplicate_normalized_path(self):
		path = Path(self.temporary.name) / "duplicate.builderext"
		with warnings.catch_warnings(), zipfile.ZipFile(path, "w") as archive:
			warnings.simplefilter("ignore", UserWarning)
			archive.writestr("manifest.json", json.dumps(manifest(icon=None)))
			archive.writestr("main.js", "")
			archive.writestr("./main.js", "")
		self.assert_code("duplicate_path", path)

	def test_rejects_wrong_manifest_identity(self):
		self.assert_code(
			"identity_mismatch",
			self.package(
				{"manifest.json": json.dumps(manifest(name="acme/other", icon=None)), "main.js": ""}
			),
		)
		self.assert_code(
			"version_mismatch",
			self.package({"manifest.json": json.dumps(manifest(version="2.0.0", icon=None)), "main.js": ""}),
		)

	def test_rejects_relative_imports(self):
		base_manifest = json.dumps(manifest(icon=None))
		self.assert_code(
			"relative_import",
			self.package({"manifest.json": base_manifest, "main.js": 'import "../escape.js";'}),
		)
		self.assert_code(
			"relative_import",
			self.package({"manifest.json": base_manifest, "main.js": 'import "./missing.js";'}),
		)

	def test_enforces_compressed_source_and_icon_size_limits(self):
		path = self.package()
		with patch("builder_hub.extensions.package.MAX_PACKAGE_SIZE", 1):
			self.assert_code("package_too_large", path)
		with patch("builder_hub.extensions.package.MAX_MAIN_JS_SIZE", 4):
			self.assert_code("source_too_large", path)
		with patch("builder_hub.extensions.package.MAX_ICON_SIZE", 4):
			self.assert_code("unsafe_svg", path)

	def test_rejects_unsafe_svg_forms(self):
		unsafe = (
			b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
			b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
			b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x.png"/></svg>',
			b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
			b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.com/x.css";</style></svg>',
		)
		for content in unsafe:
			with self.subTest(content=content), self.assertRaises(ProtocolValidationError):
				validate_svg(content)

	def test_rejects_an_unnamed_svg(self):
		files = {
			"manifest.json": json.dumps(manifest(icon=None)),
			"main.js": "",
			"unused.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
		}
		self.assert_code("unexpected_file", self.package(files))


if __name__ == "__main__":
	unittest.main()
