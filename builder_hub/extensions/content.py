"""Sanitized public text and content-addressed catalog icons."""

from __future__ import annotations

import hashlib
import html
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from frappe.utils import get_files_path

MARKDOWN_LINK = re.compile(r"(!?\[[^\]]*\])\(([^)]+)\)")
RAW_HTML = re.compile(r"<[^>]*>", re.S)
REFERENCE_LINK = re.compile(r"^\s*(\[[^\]]+\]:)\s*(\S+)(.*)$", re.I | re.M)


def sanitize_markdown(markdown: str) -> str:
	"""Keep Markdown source while removing raw HTML and unsafe link schemes."""
	markdown = RAW_HTML.sub("", markdown or "")
	markdown = MARKDOWN_LINK.sub(_sanitize_inline_link, markdown)
	return REFERENCE_LINK.sub(_sanitize_reference_link, markdown).replace("\x00", "")


def store_catalog_icon(content: bytes) -> str:
	digest = hashlib.sha256(content).hexdigest()
	filename = f"{digest}.svg"
	directory = Path(get_files_path("extension-icons"))
	directory.mkdir(mode=0o755, parents=True, exist_ok=True)
	path = directory / filename
	if not path.exists():
		_write_icon(path, content)
	return f"/files/extension-icons/{filename}"


def _sanitize_inline_link(match: re.Match) -> str:
	label, destination = match.groups()
	if _unsafe_markdown_scheme(destination):
		return label[1:] if label.startswith("!") else label
	return match.group(0)


def _sanitize_reference_link(match: re.Match) -> str:
	label, destination, _suffix = match.groups()
	return label if _unsafe_markdown_scheme(destination) else match.group(0)


def _unsafe_markdown_scheme(destination: str) -> str:
	destination = html.unescape(destination.strip("<> ").split(maxsplit=1)[0])
	destination = "".join(character for character in destination if ord(character) > 32)
	scheme = urlparse(destination).scheme.lower()
	return scheme if scheme and scheme not in {"http", "https", "mailto"} else ""


def _write_icon(path: Path, content: bytes) -> None:
	with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".extension-icon-", delete=False) as output:
		temporary = Path(output.name)
		output.write(content)
	try:
		temporary.replace(path)
	finally:
		if temporary.exists():
			temporary.unlink()
