"""One readable line per pipeline event, for the CLI (C-2). Pure formatting, no logic."""
from __future__ import annotations

import json
import re
from typing import Any

BOUND = "⟨bound: session⟩"


def _n(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _ms(n: int) -> str:
    return _n(n) + " ms"


def _sql_value(v: Any) -> str:
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, (dict, list)):
        return "'" + json.dumps(v) + "'"
    return str(v)


def fill_sql(info: dict, args: dict) -> str:
    """The declared statement with the model's values filled in; bound params shown as bound."""
    stmt = info.get("statement", "")
    for n, name in sorted(enumerate(info.get("params", []), 1), key=lambda p: -p[0]):
        value = BOUND if name in info.get("bound", []) else _sql_value(args.get(name))
        stmt = re.sub(rf"\${n}(?!\d)", lambda _m, v=value: v, stmt)
    return stmt


def _summary(rows: list[dict]) -> str:
    if not rows:
        return "0 rows"
    first = rows[0]
    keys = [k for k in ("order_id", "status", "id", "action_type") if k in first]
    head = " ".join(f"{k}={first[k]}" for k in keys)
    return f"{len(rows)} row{'s' if len(rows) != 1 else ''}" + (f": {head}" + (" …" if len(rows) > 1 else "") if head else "")


def line(e: dict) -> str | None:
    t = e["type"]
    if t == "trace":
        return f"[trace]   {e['turn_id']} · trace {e['trace_id']}"
    if t == "stage":
        return None
    if t == "step":
        status = e["status"].upper() if e["status"] != "passed" else "passed"
        out = f"[{e['key']}]".ljust(10) + f"{status} · {_ms(e['ms'])} · {e['span']} · {e.get('detail', '')}"
        for m in e.get("memories", []):
            mark = "✓" if m["inserted"] else "✗"
            why = f" ({m['reason']})" if m.get("reason") else ""
            out += f"\n          {mark} {m['score']:.2f}  \"{m['memory']}\"{why}"
        return out
    if t == "llm":
        return (f"[llm]     {e['model']} → {e['decision']} · "
                f"{_n(e['tokens_in'])} in / {_n(e['tokens_out'])} out · {_ms(e['ms'])}")
    if t == "tool_call":
        args = ", ".join(f"{k}={v!r}" for k, v in e["args"].items())
        sql = fill_sql(e.get("info", {}), e["args"])
        return f"[tool]    {e['name']}({args})\n          {sql}"
    if t == "tool_result":
        status = "ok" if e["ok"] else f"FAILED: {e.get('error')}"
        return f"[result]  {e['name']} · {status} · {_ms(e['ms'])} · {_summary(e['result'])}"
    if t == "final":
        tail = f"[{e['terminated']}]".ljust(10) + f"{_ms(e['wall_clock_ms'])} · {_n(e['tokens']['in'])} in / {_n(e['tokens']['out'])} out"
        if e["blocked"]:
            tail += f" · blocked at {e['blocked_at']}"
        return f"Agent: {e['response']}\n{tail}"
    if t == "error":
        return f"[error]   {e['step']} · {e['status']} · {e['error']}\nAgent: (no answer: {e['step']} failed)"
    return None
