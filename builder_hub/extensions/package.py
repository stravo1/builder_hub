"""Bounded validation for untrusted .builderext ZIP packages."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from builder_hub.extensions.protocol import (
	ALLOWED_SUFFIXES,
	MAX_EXTRACTED_SIZE,
	MAX_PACKAGE_FILES,
	MAX_PACKAGE_SIZE,
	ProtocolValidationError,
	validate_manifest,
)

IMPORT_PATTERN = re.compile(
	r"(?:\bimport\s*(?:[^'\";]*?\sfrom\s*)?|\bexport\s+[^'\";]*?\sfrom\s*|\bimport\s*\()"
	r"['\"]([^'\"]+)['\"]"
)
URL_PATTERN = re.compile(r"\bnew\s+URL\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*import\.meta\.url")
EXTERNAL_SVG_REFERENCE = re.compile(
	r"(?:url\s*\(\s*['\"]?\s*(?!#)|^\s*(?:https?:|//|data:|javascript:))", re.I
)


@dataclass(frozen=True)
class ValidatedPackage:
	manifest: dict
	package_sha256: str
	package_size: int
	icon_name: str | None
	icon_content: bytes | None


def validate_package(
	package_path: str | os.PathLike,
	*,
	expected_name: str,
	expected_version: str,
) -> ValidatedPackage:
	path = Path(package_path)
	package_size = path.stat().st_size
	if package_size > MAX_PACKAGE_SIZE:
		raise ProtocolValidationError("package_too_large", "The compressed package exceeds 10 MB.")

	package_sha256 = _sha256(path)
	try:
		with zipfile.ZipFile(path) as archive:
			entries = _inspect_archive(archive)
			_validate_archive_contents(archive, entries)
			manifest = _read_manifest(archive, entries)
			_validate_package_identity(manifest, expected_name, expected_version)
			main_js = _read_entry(archive, entries["main.js"], MAX_EXTRACTED_SIZE)
			_validate_relative_imports(main_js, set(entries))
			icon_name, icon_content = _validate_package_svgs(archive, entries, manifest.get("icon"))
	except zipfile.BadZipFile:
		raise ProtocolValidationError("invalid_zip", "The extension package is not a valid ZIP file.")

	return ValidatedPackage(manifest, package_sha256, package_size, icon_name, icon_content)


def validate_svg(content: bytes, name: str = "SVG") -> None:
	if len(content) > MAX_EXTRACTED_SIZE:
		raise ProtocolValidationError("unsafe_svg", f"{name} is too large.")
	text = _decode_svg(content, name)
	root = _parse_svg(text, name)
	if _local_name(root.tag).lower() != "svg":
		raise ProtocolValidationError("unsafe_svg", f"{name} does not have an SVG root element.")
	for element in root.iter():
		_validate_svg_element(element, name)


def _decode_svg(content: bytes, name: str) -> str:
	try:
		return content.decode("utf-8", errors="strict")
	except UnicodeDecodeError:
		raise ProtocolValidationError("unsafe_svg", f"{name} is not valid UTF-8 SVG.")


def _parse_svg(text: str, name: str) -> ET.Element:
	if re.search(r"<!DOCTYPE|<!ENTITY", text, flags=re.I):
		raise ProtocolValidationError("unsafe_svg", f"{name} contains a forbidden declaration.")
	if re.search(r"<\?xml-stylesheet|@import", text, flags=re.I) or EXTERNAL_SVG_REFERENCE.search(text):
		raise ProtocolValidationError("unsafe_svg", f"{name} contains an external reference.")
	try:
		return ET.fromstring(text)
	except ET.ParseError, UnicodeDecodeError:
		raise ProtocolValidationError("unsafe_svg", f"{name} is not valid UTF-8 SVG.")


def _validate_svg_element(element: ET.Element, name: str) -> None:
	tag = _local_name(element.tag).lower()
	if tag in {"script", "foreignobject"}:
		raise ProtocolValidationError("unsafe_svg", f"{name} contains a forbidden {tag} element.")
	for attribute, value in element.attrib.items():
		attribute = _local_name(attribute).lower()
		value = value.strip()
		if attribute.startswith("on"):
			raise ProtocolValidationError("unsafe_svg", f"{name} contains an event attribute.")
		if attribute in {"href", "src"} and value and not value.startswith("#"):
			raise ProtocolValidationError("unsafe_svg", f"{name} contains an external reference.")
		if EXTERNAL_SVG_REFERENCE.search(value):
			raise ProtocolValidationError("unsafe_svg", f"{name} contains an external reference.")


def _inspect_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
	entries: dict[str, zipfile.ZipInfo] = {}
	extracted_size = 0
	file_count = 0
	for entry in archive.infolist():
		normalized = _normalized_path(entry.filename)
		if entry.is_dir():
			continue
		_validate_file_entry(entry, normalized, entries)
		entries[normalized] = entry
		file_count += 1
		extracted_size += entry.file_size
		_validate_archive_limits(file_count, extracted_size)

	for required in ("manifest.json", "main.js"):
		if required not in entries:
			raise ProtocolValidationError("missing_required_file", f"The package is missing {required}.")
	return entries


def _validate_file_entry(
	entry: zipfile.ZipInfo,
	normalized: str,
	existing_entries: dict[str, zipfile.ZipInfo],
) -> None:
	if _is_symlink(entry):
		raise ProtocolValidationError("symlink", f"ZIP entry is a symbolic link: {entry.filename}.")
	if normalized in existing_entries:
		raise ProtocolValidationError("duplicate_path", f"ZIP contains duplicate path: {normalized}.")
	if entry.flag_bits & 0x1:
		raise ProtocolValidationError(
			"encrypted_file", f"Encrypted ZIP entry is not allowed: {entry.filename}."
		)
	if Path(normalized).suffix not in ALLOWED_SUFFIXES:
		raise ProtocolValidationError("unsupported_file", f"Unsupported package file: {normalized}.")


def _validate_archive_limits(file_count: int, extracted_size: int) -> None:
	if file_count > MAX_PACKAGE_FILES:
		raise ProtocolValidationError("too_many_files", "The package contains more than 200 files.")
	if extracted_size > MAX_EXTRACTED_SIZE:
		raise ProtocolValidationError("extracted_too_large", "The extracted package exceeds 30 MB.")


def _validate_archive_contents(archive: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo]) -> None:
	"""Read every entry without extracting it, enforcing actual output and CRC checks."""
	total = 0
	try:
		for entry in entries.values():
			with archive.open(entry) as source:
				while chunk := source.read(64 * 1024):
					total += len(chunk)
					if total > MAX_EXTRACTED_SIZE:
						raise ProtocolValidationError(
							"extracted_too_large", "The extracted package exceeds 30 MB."
						)
	except RuntimeError, zipfile.BadZipFile:
		raise ProtocolValidationError("invalid_zip", "The extension package contains invalid file data.")


def _normalized_path(name: str) -> str:
	if "\x00" in name:
		raise ProtocolValidationError("unsafe_path", "A ZIP path contains a null byte.")
	name = unicodedata.normalize("NFC", name.replace("\\", "/"))
	path = PurePosixPath(name)
	if (
		path.is_absolute()
		or ".." in path.parts
		or not name
		or name.startswith("/")
		or re.match(r"^[A-Za-z]:/", name)
	):
		raise ProtocolValidationError("unsafe_path", f"Unsafe ZIP path: {name}.")
	normalized = posixpath.normpath(name)
	if normalized in {".", ".."} or normalized.startswith("../"):
		raise ProtocolValidationError("unsafe_path", f"Unsafe ZIP path: {name}.")
	return normalized


def _is_symlink(entry: zipfile.ZipInfo) -> bool:
	return stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK


def _read_manifest(archive: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo]) -> dict:
	content = _read_entry(archive, entries["manifest.json"], 128 * 1024)
	try:
		manifest = json.loads(content.decode("utf-8"))
	except UnicodeDecodeError, json.JSONDecodeError:
		raise ProtocolValidationError("invalid_manifest_json", "manifest.json must contain valid UTF-8 JSON.")
	return validate_manifest(manifest)


def _validate_package_identity(manifest: dict, expected_name: str, expected_version: str) -> None:
	if manifest["name"] != expected_name:
		raise ProtocolValidationError(
			"identity_mismatch", "The package manifest name does not match the extension listing."
		)
	if manifest["version"] != expected_version:
		raise ProtocolValidationError(
			"version_mismatch", "The package manifest version does not match the GitHub release."
		)


def _validate_package_svgs(
	archive: zipfile.ZipFile,
	entries: dict[str, zipfile.ZipInfo],
	icon_name: str | None,
) -> tuple[str | None, bytes | None]:
	if icon_name and icon_name not in entries:
		raise ProtocolValidationError("missing_icon", "The manifest icon is missing from the package.")
	icon_content = None
	for name, entry in entries.items():
		if not name.endswith(".svg"):
			continue
		content = _read_entry(archive, entry, min(entry.file_size + 1, MAX_EXTRACTED_SIZE))
		validate_svg(content, name)
		if name == icon_name:
			icon_content = content
	return icon_name, icon_content


def _read_entry(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int) -> bytes:
	with archive.open(entry) as source:
		content = source.read(limit + 1)
	if len(content) > limit:
		raise ProtocolValidationError("entry_too_large", f"Package file is too large: {entry.filename}.")
	return content


def _validate_relative_imports(content: bytes, paths: set[str]) -> None:
	try:
		source = content.decode("utf-8")
	except UnicodeDecodeError:
		raise ProtocolValidationError("invalid_javascript", "main.js must contain UTF-8 JavaScript.")
	imports = [*IMPORT_PATTERN.findall(source), *URL_PATTERN.findall(source)]
	for specifier in imports:
		if not specifier.startswith("."):
			continue
		resolved = posixpath.normpath(specifier)
		if resolved == ".." or resolved.startswith("../"):
			raise ProtocolValidationError(
				"unsafe_import", f"Relative import leaves the package: {specifier}."
			)
		candidates = {
			resolved,
			f"{resolved}.js",
			f"{resolved}.mjs",
			f"{resolved}/index.js",
			f"{resolved}/index.mjs",
		}
		if not candidates.intersection(paths):
			raise ProtocolValidationError(
				"missing_import", f"Relative import is missing from the package: {specifier}."
			)


def _sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as package:
		for chunk in iter(lambda: package.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _local_name(name: str) -> str:
	return name.rsplit("}", 1)[-1]
