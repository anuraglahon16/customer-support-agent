"""Bounded, reported retries for guard model calls (SPEC §16), shared by Judge and Guardrail.

The SDKs' own retries are switched off for guard agents (support.models.llm(max_retries=0))
because they are silent. Here every failed attempt is recorded and handed back, so a turn
that passed on its second try says so in its step detail and run log.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

from support.models import api_error

T = TypeVar("T")

RETRY_CODES = {429, 500, 502, 503, 504, 529}   # "try again later"; 529 = Claude API overloaded
MAX_ATTEMPTS = 3
RETRY_BUDGET_S = 8      # no new attempt after 8 s
CALL_DEADLINE_S = 6     # one whole model call, start to finish. The SDK's own timeout bounds
                        # each read, so a slowly streaming reply outlived the caller's 15 s
                        # guard timeout with no retry ever starting (eval run 9, M01).
                        # Worst case: 6 + 0.5 + 6 = 12.5 s, inside the 15 s guard timeout.


async def with_retries(call: Callable[[], Awaitable[T]], log: logging.Logger) -> tuple[T, list[str]]:
    """Run `call`, retrying provider "try again later" errors. Returns (result, failed attempts).
    Raises RuntimeError naming every attempt once it gives up; never returns a default."""
    failed: list[str] = []
    start = time.perf_counter()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(call(), CALL_DEADLINE_S), failed
        except TimeoutError as exc:   # the whole call ran past its deadline: retryable
            failed.append(f"attempt {attempt}: model call exceeded {CALL_DEADLINE_S}s")
            log.warning("model call failed (%s)", failed[-1])
            if attempt == MAX_ATTEMPTS or time.perf_counter() - start > RETRY_BUDGET_S:
                raise RuntimeError(f"model unavailable after {attempt} attempt(s): {'; '.join(failed)}") from exc
            await asyncio.sleep(0.5)
            continue
        except Exception as exc:
            info = api_error(exc)
            if info is None:
                raise
            status, wait, label = info
            failed.append(f"attempt {attempt}: {label}" + (f" (retry in {wait:.0f}s)" if wait else ""))
            log.warning("model call failed (%s)", failed[-1])
            if status not in RETRY_CODES or attempt == MAX_ATTEMPTS or wait > RETRY_BUDGET_S \
                    or time.perf_counter() - start > RETRY_BUDGET_S:
                raise RuntimeError(f"model unavailable after {attempt} attempt(s): {'; '.join(failed)}") from exc
            await asyncio.sleep(max(wait, 0.5 * 2 ** (attempt - 1)))
    raise AssertionError("unreachable")


def usage_tokens(event) -> dict:
    """{"in", "out"} from an ADK event's usage metadata (0 if absent)."""
    u = getattr(event, "usage_metadata", None)
    if not u:
        return {"in": 0, "out": 0}
    return {"in": u.prompt_token_count or 0,
            "out": (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)}
