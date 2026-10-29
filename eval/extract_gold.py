"""Transcribe the EVALS.md §3 gold sets into eval/gold/<set>.jsonl, verbatim (EVALS §3).

    .venv/bin/python -m eval.extract_gold

Parsed straight from the Markdown tables so no item is retyped (and mistyped) by hand.
Items are never edited; extra items go in separate files (EVALS §3).
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = {  # heading prefix → (set name, column names)
    "### 3.1": ("attack", ["id", "message"]),
    "### 3.2": ("legit", ["id", "user", "message"]),
    "### 3.3": ("offtopic", ["id", "message"]),
    "### 3.4": ("probe", ["id", "user", "message", "leak_markers"]),
    "### 3.5": ("order", ["id", "user", "message", "must_contain", "expected_tool"]),
    "### 3.6": ("action", ["id", "user", "message", "expected_action_type"]),
    "### 3.7": ("memory", ["id", "user", "plant", "ask", "keyword"]),
}
DEFAULT_USER = {"attack": "alice", "offtopic": "alice"}   # "All items run as alice"


def cell(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":   # code-formatted message
        text = text[1:-1]
    return text


def main() -> None:
    lines = (ROOT / "EVALS.md").read_text().splitlines()
    current = None
    rows: dict[str, list[dict]] = {}
    for line in lines:
        head = next((k for k in SECTIONS if line.startswith(k)), None)
        if head:
            current = SECTIONS[head]
            rows[current[0]] = []
            continue
        if line.startswith("## ") or line.startswith("### "):
            current = None
        if not current or not line.startswith("|") or re.match(r"^\|\s*(Id|---)", line):
            continue
        name, cols = current
        cells = [cell(c) for c in line.strip().strip("|").split(" | ")]
        if len(cells) != len(cols):
            raise SystemExit(f"{name}: expected {len(cols)} columns, got {len(cells)}: {line}")
        item = dict(zip(cols, cells, strict=True))
        item.setdefault("user", DEFAULT_USER.get(name))
        rows[name].append(item)
    for name, items in rows.items():
        with open(ROOT / "eval" / "gold" / f"{name}.jsonl", "w") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"{name:<9} {len(items):>3} items")


if __name__ == "__main__":
    main()
