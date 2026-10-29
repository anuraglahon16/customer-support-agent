"""One turn of the support desk, as a stream of events (SPEC §7.1).

The CLI and the web server only render what `run_turn` yields (P-1). Steps so far:
sanitize → judge → guardrail → recall → agent → mask → save (P-2).
A block ends the turn at once (P-3); a guard that errors ends it with `error` (P-4).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator

import yaml
from google.genai import types
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from guards import guardrail
from guards.sanitizer import sanitize
from support import guard_client, memory
from support.agent import AGENT_NAME, MODEL, UserSession, decode_result
from support.guard_client import GuardError
from support.telemetry import CHAIN, GUARDRAIL, KIND, RETRIEVER, TOOL, flush, trace_url, tracer

log = logging.getLogger("support.pipeline")

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
TOOLS_YAML = ROOT / "mcp_toolbox" / "tools.yaml"
BOUND_PARAMS = {"customer_email", "user_email"}   # bound from the session (M-5)

# Budgets, from THRESHOLDS.md (P-6)
T_BUD_TOOLS = 6
T_BUD_TOKENS = 30_000
T_BUD_WALL_S = 30


def _load_tool_info() -> dict[str, dict]:
    """Statement + $n-ordered params per tool, for display only (§7.1 tool_call.info)."""
    info = {}
    for doc in yaml.safe_load_all(TOOLS_YAML.read_text()):
        if doc and doc.get("kind") == "tool":
            stmt = " ".join(doc["statement"].split())
            params = [p["name"] for p in doc.get("parameters", [])]
            info[doc["name"]] = {
                "kind": "MCP",
                "access": "WRITE" if stmt.upper().startswith("INSERT") else "READ",
                "statement": stmt,
                "params": params,
                "bound": [p for p in params if p in BOUND_PARAMS],
            }
    return info


TOOL_INFO = _load_tool_info()


def short_error(exc: Exception) -> str:
    """One readable line for the event stream; the full text goes to logs/ (P-4: say why, briefly)."""
    text = " ".join(str(exc).split())
    if "RESOURCE_EXHAUSTED" in text and "per day" in text.lower():
        text = "Gemini daily quota exhausted (429 RESOURCE_EXHAUSTED, free tier)"
    elif m := re.search(r"Error code: (\d{3}).*?'type': '(\w+_error)'", text):   # Claude API
        text = f"{type(exc).__name__}: {m.group(1)} {m.group(2)}"
    elif m := re.search(r"\b(4\d\d|5\d\d) [A-Z_]{4,}", text):
        text = f"{type(exc).__name__}: {m.group(0)}" + (f" — {text[:160]}" if len(text) < 160 else "")
    return text[:240]


def new_turn_id() -> str:
    return f"turn_{int(time.time() * 1000):011x}{secrets.token_hex(4)}"


def _decode_args(args: dict[str, Any]) -> dict[str, Any]:
    """The model sends action-log's `parameters` as a JSON string; emit it as an object."""
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and v.strip()[:1] in "{[":
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


BLOCKED_REPLY = {
    "sanitize": "Sorry, I can't accept that message. Please rephrase it in plain text.",
    "judge": "Sorry, I can't help with that request.",
    "guardrail": "I can only help with your orders, deliveries, returns and account. "
                 "Is there something about an order I can help with?",
}


class Cap(Exception):
    def __init__(self, which: str):
        super().__init__(which)
        self.which = which


async def run_turn(session: UserSession, message: str) -> AsyncIterator[dict]:
    """One root `agent.turn` span (CHAIN) around the whole turn (O-2). The ADK runner is
    iterated inside it, in this task, so `invoke_agent` is its child, not its own root."""
    turn_id = new_turn_id()
    with tracer.start_as_current_span("agent.turn", attributes={
        KIND: CHAIN, "input.value": message, "user.id": session.email, "turn.id": turn_id,
    }) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        async for event in _turn(session, message, turn_id, trace_id):
            if event["type"] == "final":
                span.set_attribute("output.value", event["response"])
                span.set_attribute("turn.terminated", event["terminated"])
            elif event["type"] == "error":
                span.set_attribute("turn.terminated", "error")
                span.set_status(Status(StatusCode.ERROR, event["error"]))
            yield event
    await asyncio.to_thread(flush)   # so the trace is in Phoenix when the user opens the link


async def _turn(session: UserSession, message: str, turn_id: str, trace_id: str) -> AsyncIterator[dict]:
    t0 = time.perf_counter()
    ms = lambda since: int((time.perf_counter() - since) * 1000)   # noqa: E731
    record = {
        "turn_id": turn_id, "trace_id": None, "user": session.email, "message": message,
        "terminated": None, "blocked_at": None, "wall_clock_ms": 0,
        "tokens": {"in": 0, "out": 0}, "steps": [], "tool_calls": [], "llm_calls": 0,
    }

    def finish(event: dict) -> dict:
        """Write runs/<turn_id>.json, then hand back the terminal event (P-5)."""
        record["wall_clock_ms"] = ms(t0)
        RUNS_DIR.mkdir(exist_ok=True)
        (RUNS_DIR / f"{turn_id}.json").write_text(json.dumps(record, indent=2))
        log.info("%s terminated=%s wall=%dms", turn_id, record["terminated"], record["wall_clock_ms"])
        return event

    def blocked(key: str, detail: str) -> dict:
        record["terminated"], record["blocked_at"] = "blocked", key
        return finish({"type": "final", "blocked": True, "blocked_at": key, "response": BLOCKED_REPLY[key],
                       "terminated": "blocked", "wall_clock_ms": ms(t0), "tokens": record["tokens"]})

    def errored(key: str, exc: Exception, started: float) -> dict:
        record["terminated"] = "error"
        record["steps"].append({"key": key, "status": "error", "ms": ms(started)})
        log.warning("%s %s failed: %s", turn_id, key, exc)
        return finish({"type": "error", "step": key, "status": 502, "error": short_error(exc), "terminated": "error"})

    def add_tokens(used: dict | None) -> None:
        """Guard model tokens count toward the turn's budget too (T-BUD-TOKENS: all LLM layers)."""
        if used:
            record["tokens"]["in"] += int(used.get("in", 0))
            record["tokens"]["out"] += int(used.get("out", 0))

    record["trace_id"] = trace_id
    yield {"type": "trace", "turn_id": turn_id, "trace_id": trace_id, "url": trace_url(trace_id)}

    # ── sanitize (in-process, no model) ──────────────────────────────────────
    yield {"type": "stage", "key": "sanitize", "label": "Sanitizer"}
    t = time.perf_counter()
    with tracer.start_as_current_span("security.sanitize", attributes={KIND: GUARDRAIL}) as sp:
        ok, detail = sanitize(message)
        sp.set_attributes({"guard.passed": ok, "guard.detail": detail})
    status = "passed" if ok else "blocked"
    record["steps"].append({"key": "sanitize", "status": status, "ms": ms(t)})
    yield {"type": "step", "key": "sanitize", "status": status, "detail": detail, "ms": ms(t),
           "span": "security.sanitize", "kind": "Python fn"}
    if not ok:
        yield blocked("sanitize", detail)
        return

    # ── judge (A2A service) ──────────────────────────────────────────────────
    yield {"type": "stage", "key": "judge", "label": "A2A Security Judge"}
    t = time.perf_counter()
    with tracer.start_as_current_span("security.a2a_judge", attributes={KIND: GUARDRAIL}) as sp:
        try:
            verdict = await guard_client.judge(message)
        except GuardError as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            verdict = exc
        else:
            sp.set_attributes({"guard.verdict": verdict["verdict"], "guard.reason": verdict["reason"],
                               "guard.source": verdict.get("source", "")})
    if isinstance(verdict, GuardError):
        yield errored("judge", verdict, t)
        return
    add_tokens(verdict.get("tokens"))
    status = "passed" if verdict["verdict"] == "allow" else "blocked"
    retries = verdict.get("failed_attempts") or []   # logged as failed attempts, not hidden (SPEC §16)
    record["steps"].append({"key": "judge", "status": status, "ms": ms(t), **({"failed_attempts": retries} if retries else {})})
    detail = f"{verdict['verdict']} ({verdict.get('source', '?')}): {verdict['reason']}"
    if retries:
        detail += f" · after {len(retries)} failed model attempt(s)"
    yield {"type": "step", "key": "judge", "status": status, "ms": ms(t), "span": "security.a2a_judge",
           "kind": "A2A", "detail": detail}
    if status == "blocked":
        yield blocked("judge", verdict["reason"])
        return

    # ── guardrail (in-process ADK agent: is this on-topic for this shop?) ───────
    yield {"type": "stage", "key": "guardrail", "label": "Guardrail (in-process)"}
    t = time.perf_counter()
    with tracer.start_as_current_span("guardrail.check", attributes={KIND: GUARDRAIL}) as sp:
        try:
            decision = await guardrail.check(message)   # its invoke_agent span nests here
        except Exception as exc:   # an error, never a pass (GR-4, P-4)
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            decision = exc
        else:
            sp.set_attributes({"guard.decision": decision["decision"], "guard.reasoning": decision["reasoning"]})
    if isinstance(decision, Exception):
        yield errored("guardrail", decision, t)
        return
    add_tokens(decision.get("tokens"))
    status = "passed" if decision["decision"] == "safe" else "blocked"
    retries = decision.get("failed_attempts") or []
    record["steps"].append({"key": "guardrail", "status": status, "ms": ms(t),
                            **({"failed_attempts": retries} if retries else {})})
    detail = f"{decision['decision']}: {decision['reasoning']}"
    if retries:
        detail += f" · after {len(retries)} failed model attempt(s)"
    yield {"type": "step", "key": "guardrail", "status": status, "ms": ms(t), "span": "guardrail.check",
           "kind": "in-process", "detail": detail}
    if status == "blocked":
        yield blocked("guardrail", decision["reasoning"])
        return

    # ── recall (Mem0 search; a pipeline step, never a tool: R-1, R-6) ──────────
    yield {"type": "stage", "key": "recall", "label": "Memory recall (Mem0)"}
    t = time.perf_counter()
    with tracer.start_as_current_span("memory.recall", attributes={KIND: RETRIEVER, "input.value": message}) as sp:
        try:
            memories = await memory.recall(session.email, message)
        except Exception as exc:   # fail loud: no silent "no memories"
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            memories = exc
        else:
            for i, m in enumerate(memories):
                sp.set_attributes({f"retrieval.documents.{i}.document.content": m["memory"],
                                   f"retrieval.documents.{i}.document.score": m["score"],
                                   f"retrieval.documents.{i}.document.metadata": json.dumps(
                                       {"inserted": m["inserted"], "reason": m["reason"]})})
    if isinstance(memories, Exception):
        yield errored("recall", memories, t)
        return
    inserted = sum(m["inserted"] for m in memories)
    record["steps"].append({"key": "recall", "status": "passed", "ms": ms(t),
                            "inserted": inserted, "skipped": len(memories) - inserted})
    yield {"type": "step", "key": "recall", "status": "passed", "ms": ms(t), "span": "memory.recall",
           "kind": "Python fn", "memories": memories,
           "detail": f"{len(memories)} candidates · inserted {inserted}" if memories else "no memories for this customer"}

    # ── agent ────────────────────────────────────────────────────────────────
    yield {"type": "stage", "key": "agent", "label": "Support agent (ADK + MCP tools)"}
    t_agent = time.perf_counter()
    pending: dict[str, tuple[int, str, float]] = {}   # function_call id → (our id, name, start)
    next_id = 1
    reply = ""
    t_llm = time.perf_counter()
    content = types.Content(role="user", parts=[types.Part(text=memory.agent_input(message, memories))])
    events = session.runner.run_async(
        user_id=session.email, session_id=session.session_id, new_message=content)
    try:
        while True:
            remaining = T_BUD_WALL_S - (time.perf_counter() - t0)
            if remaining <= 0:
                raise Cap("wall")
            try:
                event = await asyncio.wait_for(anext(events), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise Cap("wall") from None

            calls, responses = event.get_function_calls(), event.get_function_responses()

            if event.author == AGENT_NAME and event.usage_metadata and not responses:
                u = event.usage_metadata
                t_in = u.prompt_token_count or 0
                t_out = (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
                record["tokens"]["in"] += t_in
                record["tokens"]["out"] += t_out
                record["llm_calls"] += 1
                decision = ("call " + ", ".join(c.name for c in calls)) if calls else "final answer"
                yield {"type": "llm", "model": MODEL, "decision": decision,
                       "tokens_in": t_in, "tokens_out": t_out, "ms": ms(t_llm), "span": "call_llm"}
                if record["tokens"]["in"] + record["tokens"]["out"] > T_BUD_TOKENS:
                    raise Cap("tokens")

            for call in calls:
                if len(record["tool_calls"]) + len(pending) >= T_BUD_TOOLS:
                    raise Cap("tools")
                pending[call.id] = (next_id, call.name, time.perf_counter())
                yield {"type": "tool_call", "id": next_id, "name": call.name,
                       "args": _decode_args(call.args), "info": TOOL_INFO.get(call.name, {"kind": "MCP"}),
                       "span": f"execute_tool {call.name}"}
                next_id += 1

            for resp in responses:
                our_id, name, started = pending.pop(resp.id, (None, resp.name, time.perf_counter()))
                body = resp.response or {}
                error = body.get("error") if isinstance(body, dict) else None
                result = []
                if not error:
                    try:
                        result = decode_result(body)
                    except ValueError:
                        # the Toolbox reports failures as plain text, not as an exception:
                        # "error processing request: …", "provided parameters were invalid: …"
                        raw = body.get("result", body) if isinstance(body, dict) else body
                        error = " ".join(str(raw).split())[:300] or "tool returned a non-JSON result"
                took = ms(started)
                record["tool_calls"].append(
                    {"name": name, "ok": not error, "ms": took, **({"error": str(error)} if error else {})})
                yield {"type": "tool_result", "id": our_id, "name": name, "ok": not error, "ms": took,
                       "result": result, **({"error": str(error)} if error else {})}
                t_llm = time.perf_counter()

            if event.is_final_response() and event.content and event.content.parts and not calls:
                reply = "".join(p.text or "" for p in event.content.parts if not p.thought)

    except Cap as cap:
        await events.aclose()
        for our_id, name, started in pending.values():   # every tool_call gets its tool_result (§7.3)
            record["tool_calls"].append({"name": name, "ok": False, "ms": ms(started), "error": "turn capped"})
            yield {"type": "tool_result", "id": our_id, "name": name, "ok": False, "ms": ms(started),
                   "result": [], "error": "turn capped"}
        record["terminated"] = "cap"
        record["steps"].append({"key": "agent", "status": "passed", "ms": ms(t_agent)})
        yield {"type": "step", "key": "agent", "status": "passed", "ms": ms(t_agent),
               "detail": f"stopped at budget: {cap.which}", "span": f"invoke_agent {AGENT_NAME}",
               "kind": "in-process"}
        yield finish({"type": "final", "blocked": False, "blocked_at": None,
                      "response": f"Sorry, I had to stop before finishing (limit reached: {cap.which}). "
                                  "Please try a simpler question.",
                      "terminated": "cap", "wall_clock_ms": ms(t0), "tokens": record["tokens"]})
        return
    except Exception as exc:   # fail loud: an error event, never a made-up answer (P-4)
        await events.aclose()
        for our_id, name, started in pending.values():
            record["tool_calls"].append({"name": name, "ok": False, "ms": ms(started), "error": str(exc)})
            yield {"type": "tool_result", "id": our_id, "name": name, "ok": False, "ms": ms(started),
                   "result": [], "error": str(exc)}
        record["terminated"] = "error"
        record["steps"].append({"key": "agent", "status": "error", "ms": ms(t_agent)})
        log.warning("%s agent failed: %s", turn_id, exc)
        otel_trace.get_current_span().record_exception(exc)   # the error turn's span carries the exception
        yield finish({"type": "error", "step": "agent", "status": 502,
                      "error": f"Support agent failed: {short_error(exc)}", "terminated": "error"})
        return

    record["steps"].append({"key": "agent", "status": "passed", "ms": ms(t_agent)})
    yield {"type": "step", "key": "agent", "status": "passed", "ms": ms(t_agent),
           "detail": f"{record['llm_calls']} model calls, {len(record['tool_calls'])} tool calls",
           "span": f"invoke_agent {AGENT_NAME}", "kind": "in-process"}

    # ── mask (A2A Data Masker on the agent's reply) ───────────────────────────
    yield {"type": "stage", "key": "mask", "label": "A2A Data Masker"}
    t = time.perf_counter()
    with tracer.start_as_current_span("security.a2a_mask", attributes={KIND: GUARDRAIL}) as sp:
        try:
            masked = await guard_client.mask(reply, session.email)
        except GuardError as exc:   # an unmasked reply never goes out (P-4)
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            masked = exc
        else:
            sp.set_attributes({"guard.detail": masked["detail"],
                               **{f"guard.masked.{k}": v for k, v in masked["changes"].items()}})
    if isinstance(masked, GuardError):
        yield errored("mask", masked, t)
        return
    record["steps"].append({"key": "mask", "status": "passed", "ms": ms(t), "changes": masked["changes"]})
    yield {"type": "step", "key": "mask", "status": "passed", "ms": ms(t), "span": "security.a2a_mask",
           "kind": "A2A", "detail": masked["detail"]}

    # ── save (Mem0: the user's message only, on a turn that wasn't blocked: R-4) ──
    yield {"type": "stage", "key": "save", "label": "Memory save (Mem0)"}
    t = time.perf_counter()
    with tracer.start_as_current_span("memory.save", attributes={KIND: TOOL, "input.value": message}) as sp:
        try:
            status = await memory.save(session.email, message)
        except Exception as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            status = exc
        else:
            sp.set_attribute("memory.status", status)
    if isinstance(status, Exception):
        yield errored("save", status, t)
        return
    record["steps"].append({"key": "save", "status": "passed", "ms": ms(t)})
    yield {"type": "step", "key": "save", "status": "passed", "ms": ms(t), "span": "memory.save",
           "kind": "Python fn", "detail": f"saved your message ({status})"}

    record["terminated"] = "done"
    yield finish({"type": "final", "blocked": False, "blocked_at": None, "response": masked["masked_text"],
                  "terminated": "done", "wall_clock_ms": ms(t0), "tokens": record["tokens"]})
