"""HTTP response policy for content-addressed public extension icons."""

from __future__ import annotations

import re

ICON_PATH = re.compile(r"^/files/extension-icons/[0-9a-f]{64}\.svg$")


def set_extension_icon_cache_headers(response=None, request=None) -> None:
	if request is None or response is None or not ICON_PATH.fullmatch(request.path):
		return
	response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
	response.headers["Content-Type"] = "image/svg+xml"
	response.headers["X-Content-Type-Options"] = "nosniff"
