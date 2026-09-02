"""Content-addressed storage for validated catalog icons."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from frappe.utils import get_files_path


def store_catalog_icon(content: bytes) -> str:
	digest = hashlib.sha256(content).hexdigest()
	filename = f"{digest}.svg"
	directory = Path(get_files_path("extension-icons"))
	directory.mkdir(mode=0o755, parents=True, exist_ok=True)
	path = directory / filename
	if not path.exists():
		_write_icon(path, content)
	return f"/files/extension-icons/{filename}"


def _write_icon(path: Path, content: bytes) -> None:
	with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".extension-icon-", delete=False) as output:
		temporary = Path(output.name)
		output.write(content)
	try:
		temporary.replace(path)
	finally:
		if temporary.exists():
			temporary.unlink()
