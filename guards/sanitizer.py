"""The Sanitizer: the first, free check on every message (S-1, S-2).

No model call, no network: a length limit and a character allow-list. Attack *shapes*
are the Judge's job (guards/patterns.py). The allow-list is by Unicode category, so every
letter, digit, symbol and punctuation mark passes, including ' # $ : ; @ (S-2); what is
rejected is what no customer types: control characters, invisible format characters
(zero-width spaces, bidi overrides), and unassigned or private-use code points.
"""
from __future__ import annotations

import unicodedata

# Not in THRESHOLDS.md, so it's a design choice: the longest legitimate gold message is
# under 100 chars; 1000 leaves room for a long complaint. See DESIGN.md.
MAX_CHARS = 1000

_ALLOWED_CONTROLS = {"\n", "\t"}
_REJECTED_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn"}


def sanitize(message: str) -> tuple[bool, str]:
    """(passed, detail). Never alters the message; it only decides."""
    if not message.strip():
        return False, "empty message"
    if len(message) > MAX_CHARS:
        return False, f"too long: {len(message)} chars (limit {MAX_CHARS})"
    bad = sorted({f"U+{ord(c):04X}" for c in message
                  if c not in _ALLOWED_CONTROLS and unicodedata.category(c) in _REJECTED_CATEGORIES})
    if bad:
        return False, f"disallowed characters: {', '.join(bad[:5])}"
    return True, f"{len(message)} chars, all allowed"
