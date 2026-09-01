"""Versioned Builder extension protocol constants and value validation."""

from __future__ import annotations

import re
from collections.abc import Mapping

EXTENSIONS_API_VERSION = 1
SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1

MAX_PACKAGE_SIZE = 10 * 1024 * 1024
MAX_EXTRACTED_SIZE = 30 * 1024 * 1024
MAX_PACKAGE_FILES = 200

EXTENSION_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")
SEMVER_PATTERN = re.compile(
	r"^(0|[1-9]\d*)\."
	r"(0|[1-9]\d*)\."
	r"(0|[1-9]\d*)"
	r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
	r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
	r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

SUPPORTED_CAPABILITIES = frozenset(
	{
		"context.read",
		"block.read",
		"block.update",
		"block.insert",
		"page.read",
		"page.write",
		"token.write",
		"ui.dialog",
		"ui.popover",
		"data.access",
		"schema.write",
	}
)

ALLOWED_SUFFIXES = frozenset(
	{
		".js",
		".mjs",
		".css",
		".json",
		".map",
		".svg",
		".png",
		".jpg",
		".jpeg",
		".webp",
		".gif",
		".woff",
		".woff2",
		".ttf",
		".otf",
	}
)

MANIFEST_V1_REQUIRED_FIELDS = frozenset(
	{"v", "name", "label", "description", "version", "entry", "capabilities"}
)
MANIFEST_V1_OPTIONAL_FIELDS = frozenset({"icon"})
MANIFEST_V1_FIELDS = MANIFEST_V1_REQUIRED_FIELDS | MANIFEST_V1_OPTIONAL_FIELDS


class ProtocolValidationError(ValueError):
	"""A stable, author-visible protocol validation failure."""

	def __init__(self, code: str, message: str):
		super().__init__(message)
		self.code = code
		self.message = message

	def as_dict(self) -> dict[str, str]:
		return {"code": self.code, "message": self.message}


def validate_manifest(manifest: object) -> dict:
	if not isinstance(manifest, Mapping):
		raise ProtocolValidationError("manifest_not_object", "manifest.json must contain a JSON object.")

	manifest = dict(manifest)
	_validate_manifest_fields(manifest)
	_validate_manifest_identity(manifest)
	_validate_manifest_icon(manifest.get("icon"))
	_validate_capabilities(manifest["capabilities"])
	return manifest


def _validate_manifest_fields(manifest: dict) -> None:
	missing = sorted(MANIFEST_V1_REQUIRED_FIELDS - manifest.keys())
	if missing:
		raise ProtocolValidationError(
			"manifest_missing_field", f"manifest.json is missing required field: {missing[0]}."
		)

	unknown = sorted(manifest.keys() - MANIFEST_V1_FIELDS)
	if unknown:
		raise ProtocolValidationError(
			"manifest_unknown_field", f"manifest.json contains unsupported field: {unknown[0]}."
		)

	if type(manifest["v"]) is not int or manifest["v"] != PROTOCOL_VERSION:
		raise ProtocolValidationError(
			"unsupported_protocol", f"Manifest protocol v must equal {PROTOCOL_VERSION}."
		)


def _validate_manifest_identity(manifest: dict) -> None:
	name = manifest["name"]
	if not isinstance(name, str) or not EXTENSION_NAME_PATTERN.fullmatch(name):
		raise ProtocolValidationError("invalid_name", "Manifest name must use the publisher/name format.")

	_validate_plain_text(manifest["label"], "label", 80)
	_validate_plain_text(manifest["description"], "description", 240)

	version = manifest["version"]
	if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
		raise ProtocolValidationError("invalid_version", "Manifest version must be a valid SemVer value.")

	if manifest["entry"] != "main.js":
		raise ProtocolValidationError("invalid_entry", "Manifest entry must equal main.js.")


def _validate_manifest_icon(icon: object) -> None:
	if icon is None:
		return
	if not isinstance(icon, str) or not icon or "/" in icon or "\\" in icon or not icon.endswith(".svg"):
		raise ProtocolValidationError("invalid_icon", "Manifest icon must name one root SVG file.")


def _validate_capabilities(capabilities: object) -> None:
	if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
		raise ProtocolValidationError(
			"invalid_capabilities", "Manifest capabilities must be a list of supported capability names."
		)
	if len(capabilities) != len(set(capabilities)):
		raise ProtocolValidationError(
			"duplicate_capability", "Manifest capabilities must not contain duplicates."
		)
	unsupported = sorted(set(capabilities) - SUPPORTED_CAPABILITIES)
	if unsupported:
		raise ProtocolValidationError(
			"unsupported_capability", f"Unsupported extension capability: {unsupported[0]}."
		)


def validate_versions(versions: object, current_manifest: Mapping | None = None) -> dict[str, int]:
	if not isinstance(versions, Mapping) or not versions:
		raise ProtocolValidationError("invalid_versions", "versions.json must contain a non-empty object.")

	validated: dict[str, int] = {}
	for version, protocol_version in versions.items():
		if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
			raise ProtocolValidationError(
				"invalid_versions", f"Invalid SemVer key in versions.json: {version}."
			)
		if type(protocol_version) is not int or protocol_version < 1:
			raise ProtocolValidationError(
				"invalid_versions", f"Protocol version for {version} must be a positive integer."
			)
		validated[version] = protocol_version

	if current_manifest:
		version = current_manifest.get("version")
		if validated.get(version) != current_manifest.get("v"):
			raise ProtocolValidationError(
				"versions_mismatch", "The current manifest version and protocol must match versions.json."
			)
	return validated


def semver_key(version: str) -> tuple:
	"""A deterministic SemVer precedence key; build metadata does not affect precedence."""
	match = SEMVER_PATTERN.fullmatch(version)
	if not match:
		raise ValueError(f"Invalid SemVer: {version}")
	major, minor, patch, prerelease, _build = match.groups()
	if prerelease is None:
		pre_key = (1,)
	else:
		parts = []
		for part in prerelease.split("."):
			parts.append((0, int(part)) if part.isdigit() else (1, part))
		pre_key = (0, *parts)
	return int(major), int(minor), int(patch), pre_key


def expected_package_name(extension_name: str, version: str) -> str:
	return f"{extension_name.replace('/', '-')}-{version}.builderext"


def _validate_plain_text(value: object, fieldname: str, maximum: int) -> None:
	if not isinstance(value, str) or not 1 <= len(value) <= maximum or value != value.strip():
		raise ProtocolValidationError(
			f"invalid_{fieldname}", f"Manifest {fieldname} must contain 1 through {maximum} characters."
		)
	if (
		any(ord(character) < 32 and character not in "\t\n\r" for character in value)
		or "<" in value
		or ">" in value
	):
		raise ProtocolValidationError(
			f"invalid_{fieldname}", f"Manifest {fieldname} must contain plain text only."
		)
