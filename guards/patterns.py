"""Deterministic attack patterns: the Security Judge's pattern tool (J-2).

Each pattern is written against the *shape* of an attack, not a keyword, because the
legitimate set is full of keywords: "drop the gift wrap", "select the fastest shipping",
"ignore my last message", "O'Brien St", "15 Dec; hold my deliveries" (EVALS §3.2).
A pattern that matches any legitimate message is a false block (T-LEGIT-FALSE-BLOCK).
"""
from __future__ import annotations

import re

_TABLES = r"(users|customer_orders|actions_log)"

PATTERNS: list[tuple[str, str, re.Pattern]] = [(cat, name, re.compile(rx, re.IGNORECASE)) for cat, name, rx in [
    # ── SQL injection ────────────────────────────────────────────────────────
    ("sql", "quote then SQL control",   r"'\s*(;|--|#|/\*|\bor\b|\band\b|\bunion\b)"),
    ("sql", "tautology",                r"\b(or|and)\s+['\"]?\w+['\"]?\s*=\s*['\"]?\w+"),
    ("sql", "quoted equality",          r"'\s*=\s*'"),
    ("sql", "union select",             r"\bunion\s+(all\s+)?select\b"),
    ("sql", "select star",              r"\bselect\s+\*"),
    ("sql", "statement on a table",     rf"\b(from|into|update|table|join)\s+{_TABLES}\b"),
    ("sql", "DDL",                      r"\b(drop|truncate|alter)\s+(table|database|schema)\b"),
    ("sql", "stacked statement",        r";\s*(drop|delete|update|insert|select|truncate|exec|shutdown)\b"),
    ("sql", "trailing comment",         r"(--|/\*)\s*$"),
    # ── markup / XSS ─────────────────────────────────────────────────────────
    ("markup", "html tag",              r"<\s*/?\s*(script|iframe|img|svg|object|embed|style|link|meta|body|form|input)\b"),
    ("markup", "event handler",         r"\bon[a-z]+\s*=\s*\S"),
    ("markup", "javascript url",        r"\bjavascript\s*:"),
    # ── template injection ───────────────────────────────────────────────────
    ("template", "template expression", r"\{\{.*?\}\}|\{%.*?%\}|\$\{[^}]*\}"),
    # ── shell / path traversal ───────────────────────────────────────────────
    ("shell", "command substitution",   r"\$\([^)]*\)|`[^`]+`"),
    ("shell", "chained command",        r"(^|;|&&|\|\|?)\s*(cat|rm|ls|curl|wget|bash|sh|nc|chmod|sudo)\s+[-/~.\w]"),
    ("shell", "sensitive path",         r"/etc/(passwd|shadow|hosts)\b|~/\.ssh"),
    ("shell", "path traversal",         r"(\.\./|\.\.\\){2,}"),
    # ── prompt injection / extraction ────────────────────────────────────────
    ("prompt", "ignore instructions",   r"\bignore\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|prior|above|earlier|preceding)\b"),
    ("prompt", "forget rules",          r"\b(forget|disregard|override)\s+(all\s+)?(your|the|these|previous)\s+(rules|instructions|guidelines|prompt)"),
    ("prompt", "role override",         r"\byou\s+are\s+now\b|\bact\s+as\s+(the\s+)?(an?\s+)?(admin|administrator|dba|database administrator|developer|system)\b|\bfrom\s+now\s+on\s+answer\s+as\b"),
    ("prompt", "fake system turn",      r"(^|\n)\s*(system|assistant|developer)\s*:|###\s*(new\s+)?(instructions|system)"),
    ("prompt", "prompt extraction",     r"\b(system\s+prompt|your\s+(instructions|rules|prompt|guidelines))\b|\brepeat\s+everything\s+above\b|\bverbatim\b"),
    ("prompt", "fake approval",         r"\b(security\s+judge|guardrail|security\s+check)\s+(approved|allowed|said|says)\b"),
    # ── bulk data access (other customers' data) ─────────────────────────────
    ("exfil", "every customer",         r"\b(all|every|each)\s+(other\s+)?(customers?|users?)('s?)?\b"),
    ("exfil", "credentials",            r"\bpasswords?\s+(of|for)\b|\b(dump|export)\b.*\b(table|database|emails|addresses|customer_orders|users)\b"),
]]


def scan_patterns(message: str) -> dict:
    """Scan a customer message for known attack shapes.

    Returns {"matched": bool, "matches": [{"category", "pattern", "text"}, ...]}.
    Deterministic: the same message always gives the same answer, and no model is called.
    """
    matches = []
    for category, name, rx in PATTERNS:
        if m := rx.search(message):
            matches.append({"category": category, "pattern": name, "text": m.group(0)[:60]})
    return {"matched": bool(matches), "matches": matches}
