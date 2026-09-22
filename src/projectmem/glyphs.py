"""Terminal glyphs and output guards for legacy Windows consoles.

The project writes a few box-drawing/status glyphs to the terminal. A legacy
Windows code page can raise while encoding them, and punctuation such as an em
dash can be encodable as cp1252 bytes that a UTF-8 terminal then renders as a
replacement diamond. Keep the real typography on Unicode streams and fold all
terminal output to safe ASCII otherwise.
"""
from __future__ import annotations

import sys

_PROBE = "═"


def _stream_handles_unicode() -> bool:
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return False
    try:
        _PROBE.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


ASCII = not _stream_handles_unicode()


def _pick(unicode_form: str, ascii_form: str) -> str:
    return ascii_form if ASCII else unicode_form


OK = _pick("✓", "+")
FAIL = _pick("✗", "x")
WARN = _pick("⚠", "!")
RUNNING = _pick("●", "*")
STOPPED = _pick("○", "o")
RULE = _pick("─", "-")
RULE_HEAVY = _pick("━", "-")
RULE_DOUBLE = _pick("═", "=")
ARROW = _pick("→", "->")
BAR_FULL = _pick("█", "#")
BAR_EMPTY = _pick("░", ".")


_FOLD = str.maketrans({
    "—": "--",
    "–": "-",
    "…": "...",
    "·": "*",
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "−": "-",
    " ": " ",
})


class _AsciiFoldingStream:
    """Forward a text stream while replacing Unicode punctuation."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, text):
        if isinstance(text, str):
            text = text.translate(_FOLD)
        return self._stream.write(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def configure_stdio() -> None:
    """Make CLI output non-fatal and ASCII-safe on non-Unicode streams."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    if ASCII:
        sys.stdout = _AsciiFoldingStream(sys.stdout)
        sys.stderr = _AsciiFoldingStream(sys.stderr)
