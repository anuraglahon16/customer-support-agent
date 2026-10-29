"""The web front end: SPEC §7.2 routes over the same pipeline the CLI uses (W-1, W-2).

    .venv/bin/uvicorn support.web:app --host 127.0.0.1 --port 8000      # or ./run.sh web

POST /api/chat streams `run_turn`'s events as application/x-ndjson, one JSON object per
line as each happens: identical events to `support.cli --events`. No pipeline logic here
(P-1). The agent.turn span is opened inside run_turn, i.e. inside the generator the
response iterates, so web turns are traced like CLI turns (O-2; TECHNICAL troubleshooting).
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from guards.sanitizer import MAX_CHARS
from support import telemetry
from support.agent import MODEL, TOOLBOX_URL, UserSession, open_session, verify_login
from support.guard_client import JUDGE_URL, MASKER_URL
from support.pipeline import run_turn

ROOT = Path(__file__).resolve().parent.parent
(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(filename=ROOT / "logs" / "web.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("google_adk").setLevel(logging.CRITICAL)
log = logging.getLogger("support.web")

SESSIONS: dict[str, UserSession] = {}   # user_id (email) → that user's agent, tools bound to them


@asynccontextmanager
async def lifespan(_app: FastAPI):
    telemetry.setup()          # before any agent is built, so ADK spans are exported
    yield
    for s in list(SESSIONS.values()):
        await s.close()
    telemetry.flush()


# FastAPI ≥0.16x traces requests natively once a tracer provider exists; its "POST /api/chat"
# span would become each turn's root. SPEC §10 wants agent.turn as the root (T-TRACE-ONE).
app = FastAPI(title="Customer Support", lifespan=lifespan, telemetry={"tracing": False})


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@app.post("/api/login")
async def login(request: Request):
    body = await _json_body(request)
    email, password = str(body.get("email", "")).strip(), str(body.get("password", ""))
    if not email or not password:
        return _error(400, "email and password are required")
    profile = await verify_login(email, password)
    if not profile:
        return _error(401, "Invalid email or password.")
    if email not in SESSIONS:
        SESSIONS[email] = await open_session(profile)
    log.info("login %s", email)
    return {"user_id": email, "full_name": profile["full_name"], "is_premium": bool(profile["is_premium_customer"])}


@app.post("/api/logout")
async def logout(request: Request):
    body = await _json_body(request)
    if session := SESSIONS.pop(str(body.get("user_id", "")), None):
        await session.close()
    return {"ok": True}


@app.post("/api/chat")
async def chat(request: Request):
    body = await _json_body(request)
    message = str(body.get("message", ""))
    if not message.strip():
        return _error(400, "message is empty")
    session = SESSIONS.get(str(body.get("user_id", "")))
    if session is None:
        return _error(401, "Not logged in.")
    if len(message) > MAX_CHARS:            # before streaming begins (§7.2)
        return _error(413, "message too long")

    async def stream():
        async for event in run_turn(session, message):
            yield json.dumps(event) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _probe(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            (await client.get(url)).raise_for_status()
        return "ok"
    except Exception as exc:
        return f"unreachable: {exc or type(exc).__name__}"


@app.get("/health")
async def health():
    try:   # the DB through the Toolbox, as the app reaches it: a failed login lookup still proves both
        await verify_login("health-check@example.invalid", "-")
        db = "ok"
    except Exception as exc:
        db = f"error: {exc}"
    checks = {
        "status": "ok", "model": MODEL, "db": db,
        "toolbox": await _probe(TOOLBOX_URL),
        "judge": await _probe(JUDGE_URL + ".well-known/agent-card.json"),
        "masker": await _probe(MASKER_URL + ".well-known/agent-card.json"),
        "mem0": "ok" if os.environ.get("MEM0_API_KEY") else "MEM0_API_KEY is empty",
        "phoenix": telemetry.check_phoenix(),
    }
    failing = [k for k, v in checks.items() if k not in ("status", "model") and v != "ok"]
    if failing:
        checks["status"] = "degraded: " + ", ".join(failing)
    return JSONResponse(checks, status_code=503 if failing else 200)


@app.get("/")
async def index():
    return FileResponse(ROOT / "support" / "static" / "index.html", media_type="text/html")
