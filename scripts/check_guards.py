"""Stage 7 check: run gold sets through the guards only (sanitize → judge → guardrail), and count.

    .venv/bin/python scripts/check_guards.py legit offtopic        # default: legit offtopic attack

Calls the same guard functions as the pipeline, stopping at the first block, but never runs
the support agent, so measuring the guards costs only the Judge and Guardrail calls. Not the
graded eval (stage 10 runs whole turns); this is the both-sides count TECHNICAL stage 7 asks
for. Errors are shown and counted separately, never as passes.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guards import guardrail                    # noqa: E402
from guards.sanitizer import sanitize           # noqa: E402
from support import guard_client                # noqa: E402

GOLD = Path(__file__).resolve().parent.parent / "eval" / "gold"
WANT_BLOCKED = {"attack": True, "offtopic": True, "legit": False}
CONCURRENCY = 4


async def run_guards(message: str) -> tuple[str, str, int]:
    """(outcome, detail, ms): outcome is 'passed', 'blocked:<step>' or 'error:<step>'."""
    t0 = time.perf_counter()
    ms = lambda: int((time.perf_counter() - t0) * 1000)   # noqa: E731
    ok, detail = sanitize(message)
    if not ok:
        return "blocked:sanitize", detail, ms()
    try:
        v = await guard_client.judge(message)
    except Exception as exc:
        return "error:judge", str(exc)[:90], ms()
    if v["verdict"] == "block":
        return "blocked:judge", f"({v.get('source')}) {v['reason']}", ms()
    try:
        d = await guardrail.check(message)
    except Exception as exc:
        return "error:guardrail", str(exc)[:90], ms()
    if d["decision"] == "unsafe":
        return "blocked:guardrail", d["reasoning"], ms()
    return "passed", d["reasoning"], ms()


async def main(sets: list[str]) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(item):
        async with sem:
            return item, *(await run_guards(item["message"]))

    for name in sets:
        items = [json.loads(line) for line in (GOLD / f"{name}.jsonl").read_text().splitlines()]
        results = await asyncio.gather(*(one(i) for i in items))
        print(f"\n── {name} ({len(items)} items): {'should be BLOCKED' if WANT_BLOCKED[name] else 'should PASS'}")
        wrong = errors = 0
        for item, outcome, detail, took in results:
            is_blocked = outcome.startswith("blocked")
            if outcome.startswith("error"):
                errors += 1
                mark = "ERR"
            elif is_blocked != WANT_BLOCKED[name]:
                wrong += 1
                mark = "✗"
            else:
                mark = "✓"
            print(f"  {mark:<3} {item['id']}  {outcome:<18} {took:>5} ms  {item['message'][:48]!r:<52} {detail[:70]}")
        blocked = sum(r[1].startswith("blocked") for r in results)
        where = {k: sum(r[1] == f"blocked:{k}" for r in results) for k in ("sanitize", "judge", "guardrail")}
        print(f"  blocked {blocked}/{len(items)} = {blocked / len(items):.2f}  (by step: {where})  "
              f"wrong: {wrong}  errors: {errors}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["legit", "offtopic", "attack"]))
