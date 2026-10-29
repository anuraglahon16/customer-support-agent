"""Calls the A2A guard services. Every failure raises GuardError; nothing here returns
"allow" by default. Unreachable, timed out, an error reply, or a verdict we can't read
all end the turn with an `error` event (P-4, J-4, R-4)."""
from __future__ import annotations

import os
import uuid

import httpx

JUDGE_URL = f"http://127.0.0.1:{os.environ.get('JUDGE_PORT', '10002')}/"
MASKER_URL = f"http://127.0.0.1:{os.environ.get('MASKER_PORT', '10003')}/"
GUARD_TIMEOUT_S = 15


class GuardError(Exception):
    pass


async def a2a_send(url: str, text: str, service: str, data: dict | None = None) -> dict:
    """JSON-RPC message/send; returns the reply's data part."""
    parts = [{"kind": "text", "text": text}] + ([{"kind": "data", "data": data}] if data else [])
    payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send", "params": {
        "message": {"kind": "message", "role": "user", "messageId": uuid.uuid4().hex, "parts": parts}}}
    try:
        async with httpx.AsyncClient(timeout=GUARD_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload)
    except httpx.TimeoutException:
        raise GuardError(f"{service} timed out after {GUARD_TIMEOUT_S}s") from None
    except httpx.HTTPError as exc:
        raise GuardError(f"{service} unreachable: {exc or type(exc).__name__}") from None
    if resp.status_code != 200:
        raise GuardError(f"{service} returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise GuardError(f"{service} returned a non-JSON reply") from None
    if "error" in body:
        raise GuardError(f"{service} error: {body['error'].get('message')}")
    parts = (body.get("result") or {}).get("parts") or []
    data = next((p.get("data") for p in parts if p.get("kind") == "data"), None)
    if not isinstance(data, dict):
        raise GuardError(f"{service} reply has no data part")
    return data


async def judge(message: str) -> dict:
    data = await a2a_send(JUDGE_URL, message, "Security Judge")
    # explicit verdict or nothing: an echo, a missing field or a third value is an error (J-3, R-9)
    if data.get("verdict") not in ("allow", "block") or not str(data.get("reason", "")).strip():
        raise GuardError(f"Security Judge returned an unparseable verdict: {str(data)[:120]}")
    return data


async def mask(text: str, own_email: str) -> dict:
    """{"masked_text", "changes", "detail"}. The Masker must say what it changed (K-3)."""
    data = await a2a_send(MASKER_URL, text, "Data Masker", {"own_email": own_email})
    if not isinstance(data.get("masked_text"), str) or not isinstance(data.get("changes"), dict) \
            or not str(data.get("detail", "")).strip():
        raise GuardError(f"Data Masker returned an unparseable result: {str(data)[:120]}")
    return data
