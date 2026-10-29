"""Memory as pipeline steps, not a tool (R-1, R-6): recall before the agent, save after the Masker.

- recall: search Mem0 with the user's message, filtered to that user, T-MEM-TOPK results.
  A memory is inserted only if score ≥ T-MEM-MINSCORE and length ≤ T-MEM-MAXCHARS; every
  candidate is reported with whether it was inserted and why not (R-2, R-5, R-7).
- inserted memories go at the top of the agent's input under a fixed header (R-3).
- save: only the user's message, only on turns that weren't blocked (R-4, R-5). Mem0
  extracts facts asynchronously, so a save returns PENDING.

Decision (stage 10): T-MEM-MINSCORE lowered again, from 0.20 to 0.17. Eval run 1 (13 recalled
memories over the 10 pairs): relevant 0.176–0.342, others 0.085–0.186. The ranges overlap, so
no cutoff is perfect; 0.17 admits every relevant one, plus at most an extra standing
preference of the same customer. Order chatter, which THRESHOLDS' 0.25 was guarding
against, is no longer stored at all (EXTRACTION_INSTRUCTIONS).

Decision (stage 8): T-MEM-MINSCORE lowered from 0.25 to 0.20, on evidence (THRESHOLDS.md
§4 allows this; the recall gate T-MEM-RECALL does not move). Scores seen so far:
  relevant:   "leave parcels on the doormat" 0.27, "packages … at the back door" (M01) 0.24
  unrelated:  "keeps bike in the hallway" 0.14, "works from home" 0.12
At 0.25, M01's memory was found but not inserted, and a new session answered "I don't see
any saved delivery instructions". Re-check against the ten memory pairs at stage 10.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("MEM0_TELEMETRY", "False")   # no usage telemetry to Mem0 from this app

from dotenv import load_dotenv   # noqa: E402

load_dotenv()

# THRESHOLDS.md §4 (T-MEM-MINSCORE changed with evidence, see the docstring)
T_MEM_TOPK = 5
T_MEM_MINSCORE = 0.17
T_MEM_MAXCHARS = 500
MEM0_TIMEOUT_S = 10

HEADER = "Relevant memories about this customer (from Mem0):"

# What Mem0 may extract from a saved message. Without it, every order question became a
# "memory" ("User asked for the status of order 5") that outscored real preferences on
# similar questions and pulled order chatter into the prompt (stage 9 finding).
EXTRACTION_INSTRUCTIONS = (
    "Store only the customer's standing preferences and circumstances that matter for future "
    "service: delivery instructions, contact preferences, schedule and availability, accessibility "
    "or health needs that affect delivery or packaging, and how they want to be addressed. "
    "Do not store questions about orders, order numbers, order statuses, or one-off requests."
)

_client = None


def _mem0():
    global _client
    if _client is None:
        from mem0 import AsyncMemoryClient
        key = os.environ.get("MEM0_API_KEY")
        if not key:
            raise RuntimeError("MEM0_API_KEY is empty in .env")
        _client = AsyncMemoryClient(api_key=key)
    return _client


async def recall(user_id: str, message: str) -> list[dict]:
    """Every candidate: {"memory", "score", "inserted", "reason"} (R-5)."""
    found = await asyncio.wait_for(
        _mem0().search(message, filters={"user_id": user_id}, top_k=T_MEM_TOPK), MEM0_TIMEOUT_S)
    results = found.get("results", []) if isinstance(found, dict) else list(found or [])
    out = []
    for r in results[:T_MEM_TOPK]:
        text, score = str(r.get("memory", "")), float(r.get("score") or 0.0)
        if score < T_MEM_MINSCORE:
            reason = f"below cutoff {T_MEM_MINSCORE}"
        elif len(text) > T_MEM_MAXCHARS:
            reason = f"too long ({len(text)} chars > {T_MEM_MAXCHARS})"
        else:
            reason = None
        shown = text if reason is None else text[:200] + ("…" if len(text) > 200 else "")   # skipped: display only
        out.append({"memory": shown, "score": round(score, 3), "inserted": reason is None, "reason": reason})
    return out


def agent_input(message: str, memories: list[dict]) -> str:
    """The agent's input: inserted memories first, under the fixed header (R-3)."""
    inserted = [m["memory"] for m in memories if m["inserted"]]
    if not inserted:
        return message
    lines = "\n".join(f"- {m}" for m in inserted)
    return f"{HEADER}\n{lines}\n\nCustomer message: {message}"


async def save(user_id: str, message: str) -> str:
    """Save the user's message only (R-4). Returns Mem0's status, e.g. PENDING."""
    result = await asyncio.wait_for(
        _mem0().add(message, user_id=user_id, custom_instructions=EXTRACTION_INSTRUCTIONS), MEM0_TIMEOUT_S)
    return str(result.get("status") or result.get("results") or "saved") if isinstance(result, dict) else "saved"


async def clear(user_id: str) -> None:
    """Delete one user's memories (the eval clears the memory-pair users before a run)."""
    await asyncio.wait_for(_mem0().delete_all(user_id=user_id), MEM0_TIMEOUT_S)
