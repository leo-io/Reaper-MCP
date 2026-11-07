"""Safe quoting of strings written into a REAPER project (.rpp) file.

REAPER reads and writes strings in "...", '...' or `...`. Text is wrapped in
the first quote character it does not contain, so nothing in a song name or
path can end the string early and inject extra project lines.
"""

from __future__ import annotations

import re

from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError

MAX_LABEL_CHARS = 120

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"\s+")


def rpp_quote(text: str, *, is_path: bool) -> str:
    """Quote `text` for an .rpp file.

    A line break or NUL is always an error (it would start a new project
    line). A path containing all three quote characters is an error because
    changing it would point at a different file; other text has its double
    quotes replaced with single quotes instead.
    """
    if any(c in text for c in "\r\n\0"):
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, f"Line break or NUL not allowed: {text!r}")
    for q in ('"', "'", "`"):
        if q not in text:
            return f"{q}{text}{q}"
    if is_path:
        raise ReaperMCPError(ErrorCode.INVALID_PATH, f"Path contains all three quote characters: {text!r}")
    return '"' + text.replace('"', "'") + '"'


def clean_label(text: str) -> str:
    """A display name safe to put in a project: control characters removed,
    whitespace collapsed, at most MAX_LABEL_CHARS characters."""
    text = _CONTROL.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return text[:MAX_LABEL_CHARS].rstrip()
