"""A minimal A2A server: an agent card plus JSON-RPC `message/send` (A2A spec v0.3).

Shared by the Security Judge and the Data Masker (J-1, K-1). The handler receives the
text of the incoming message plus any `data` parts merged into one dict (the Masker gets
the customer's own email that way), and returns a dict, sent back as one `data` part, so
the verdict is structured and explicit rather than an echo of the input (J-3, R-9).
"""
from __future__ import annotations

import logging
import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("guards.a2a")

Handler = Callable[[str, dict], Awaitable[dict]]


def _rpc_error(rpc_id, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def build_app(card: dict, handler: Handler) -> FastAPI:
    app = FastAPI(title=card["name"])

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")          # older A2A drafts look here
    async def agent_card() -> dict:
        return card

    @app.post("/")
    async def rpc(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return _rpc_error(None, -32700, "parse error")
        rpc_id = body.get("id")
        if body.get("method") != "message/send":
            return _rpc_error(rpc_id, -32601, f"method not found: {body.get('method')!r} (use message/send)")
        parts = (body.get("params") or {}).get("message", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if p.get("kind") == "text")
        extra = {k: v for p in parts if p.get("kind") == "data" for k, v in (p.get("data") or {}).items()}
        if not text:
            return _rpc_error(rpc_id, -32602, "message has no text part")
        try:
            data = await handler(text, extra)
        except Exception as exc:   # report it; the caller turns this into an error turn (P-4)
            log.exception("handler failed")
            return _rpc_error(rpc_id, -32603, f"{type(exc).__name__}: {exc}")
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "data", "data": data}],
        }})

    return app
