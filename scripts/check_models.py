"""Which models do our API keys reach, and how fast? (Stage 3 setup; Claude added stage 6.)

    .venv/bin/python scripts/check_models.py [model ...]

claude-* names use ANTHROPIC_API_KEY through the official anthropic SDK; anything else
uses GOOGLE_API_KEY. Never prints a key. Each model gets one tiny call, timed.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("GOOGLE_API_KEY", "")
MODELS = sys.argv[1:] or [os.environ.get("AGENT_MODEL", ""), os.environ.get("JUDGE_MODEL", "")]
URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

def check_claude(model: str) -> None:
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"FAIL  {model:<26}      -   ANTHROPIC_API_KEY is empty in .env")
        return
    client = anthropic.Anthropic(max_retries=0)
    t0 = time.perf_counter()
    try:
        msg = client.messages.create(model=model, max_tokens=256,
                                     messages=[{"role": "user", "content": "Reply with the word ok"}])
        text = next((b.text for b in msg.content if b.type == "text"), "").strip()
        print(f"OK    {model:<26} {(time.perf_counter() - t0) * 1000:6.0f} ms  reply={text!r}")
    except anthropic.APIStatusError as e:
        print(f"FAIL  {model:<26} {(time.perf_counter() - t0) * 1000:6.0f} ms  HTTP {e.status_code}: {e.message[:110]}")
    except anthropic.APIConnectionError as e:
        print(f"FAIL  {model:<26}      -   connection error: {e}")


for model in filter(None, MODELS):
    if model.startswith("claude-"):
        check_claude(model)
        continue
    if not KEY:
        print(f"FAIL  {model:<26}      -   GOOGLE_API_KEY is empty in .env")
        continue
    body = json.dumps({"contents": [{"parts": [{"text": "Reply with the word ok"}]}]}).encode()
    req = urllib.request.Request(URL.format(model), data=body, method="POST",
                                 headers={"x-goog-api-key": KEY, "content-type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        ms = (time.perf_counter() - t0) * 1000
        text = data["candidates"][0]["content"]["parts"][0].get("text", "").strip()
        print(f"OK    {model:<26} {ms:6.0f} ms  reply={text!r}")
    except urllib.error.HTTPError as e:
        ms = (time.perf_counter() - t0) * 1000
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except ValueError:
            msg = "(empty body)"
        print(f"FAIL  {model:<26} {ms:6.0f} ms  HTTP {e.code}: {msg[:110]}")
