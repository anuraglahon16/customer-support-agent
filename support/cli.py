"""Command-line front end: log in, then one turn per line, rendered from the pipeline's events.

    .venv/bin/python -m support.cli                               # prompts for email + password
    echo "What is the status of order 3?" | \\
      .venv/bin/python -m support.cli --user alice.jones@example.com --events

`--events` prints the raw event stream as NDJSON on stdout and nothing else (C-3); prompts
and status messages go to stderr. The password is always read hidden (from the terminal),
or from --password for the eval runner.
"""
import argparse
import asyncio
import getpass
import json
import logging
import sys
from pathlib import Path

import httpx

from support import telemetry
from support.agent import TOOLBOX_URL, open_session, verify_login
from support.guard_client import JUDGE_URL, MASKER_URL
from support.pipeline import run_turn
from support.render import line

# Full error text and turn ids go to logs/cli.log (P-7); the terminal gets one line per event.
# ADK's own traceback for a failed model call is dropped: the pipeline reports it as an `error`.
logging.getLogger("google_adk").setLevel(logging.CRITICAL)
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(filename=_LOG_DIR / "cli.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def read_lines(interactive: bool):
    while True:
        if interactive:
            print("You: ", end="", file=sys.stderr, flush=True)
        raw = sys.stdin.readline()
        if not raw:
            return
        text = raw.strip()
        if text.lower() in {"quit", "exit", "q"}:
            return
        if text:
            yield text


def preflight() -> str | None:
    """Name the first service that is down, so the CLI refuses to start (C-4)."""
    import os
    from support.agent import MODEL
    if MODEL.startswith("claude-") and not os.environ.get("ANTHROPIC_API_KEY"):
        return f"AGENT_MODEL is {MODEL} but ANTHROPIC_API_KEY is empty in .env"
    try:
        httpx.get(TOOLBOX_URL, timeout=2)
    except Exception as exc:
        return f"MCP Toolbox unreachable at {TOOLBOX_URL} ({exc}); run ./run.sh toolbox"
    try:
        httpx.get(JUDGE_URL + ".well-known/agent-card.json", timeout=2).raise_for_status()
    except Exception as exc:
        return f"Security Judge unreachable at {JUDGE_URL} ({exc}); run ./run.sh judge"
    try:
        httpx.get(MASKER_URL + ".well-known/agent-card.json", timeout=2).raise_for_status()
    except Exception as exc:
        return f"Data Masker unreachable at {MASKER_URL} ({exc}); run ./run.sh masker"
    if not os.environ.get("MEM0_API_KEY"):
        return "MEM0_API_KEY is empty in .env (memory recall and save need it)"
    if (status := telemetry.check_phoenix()) != "ok":
        return f"{status}; run ./run.sh phoenix"
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(prog="support.cli")
    ap.add_argument("--user", help="login email (otherwise prompted)")
    ap.add_argument("--password", help="login password (otherwise read hidden from the terminal)")
    ap.add_argument("--events", action="store_true", help="print raw NDJSON events only")
    args = ap.parse_args()

    if problem := preflight():
        say(f"Cannot start: {problem}")
        return 2
    telemetry.setup()                     # before any agent is built, so ADK spans are exported

    if args.user:
        email = args.user
    else:
        print("Email: ", end="", file=sys.stderr, flush=True)
        email = sys.stdin.readline().strip()
    password = args.password or getpass.getpass("Password: ")   # getpass reads /dev/tty, not stdin

    profile = await verify_login(email, password)
    if not profile:
        say("Invalid email or password.")
        return 1
    session = await open_session(profile)
    say(f"Logged in as {session.full_name}. Type quit to exit.")
    try:
        for text in read_lines(sys.stdin.isatty()):
            url = None
            async for event in run_turn(session, text):
                url = event.get("url", url)
                if args.events:
                    print(json.dumps(event), flush=True)
                elif (out := line(event)) is not None:
                    print(out, flush=True)
            if url and not args.events:
                print(f"[trace]   {url}", flush=True)   # O-5
    finally:
        await session.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:   # Ctrl-C: leave quietly; type quit for a normal exit
        say("\nInterrupted.")
        sys.exit(130)
