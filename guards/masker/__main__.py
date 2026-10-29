"""The Data Masker: a separate A2A service that masks PII in the agent's reply (K-1 to K-3).

    .venv/bin/python -m guards.masker         # serves on 127.0.0.1:10003 (./run.sh masker)

Decision (stage 8): what counts as PII in this shop's replies.
- Masked: email addresses other than the logged-in customer's own, phone numbers, and
  card-like numbers (13–19 digits passing the Luhn check).
- Not masked: the customer's own email and their own delivery address. They own them,
  and masking them would make "where is my order going?" unanswerable. Another customer's
  street address is not caught here; the SQL ownership check is the defence for that.

Deterministic, no model: each match is replaced in place and nothing else in the text is
touched, so case and whitespace survive byte for byte (K-2, R-8). It reports what it
changed, or "nothing to mask" after actually scanning (K-3).
"""
from __future__ import annotations

import os
import re

import uvicorn
from dotenv import load_dotenv

from guards.a2a_server import build_app

load_dotenv()
PORT = int(os.environ.get("MASKER_PORT", "10003"))

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# +1 (206) 555-0123, 206-555-0123, 555-0100, 555 0100; not dates (2026-10-29), not prices
PHONE = re.compile(r"(?<![\w$.,/-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])?\d{3}[\s.-]\d{4}(?![\w/-])")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

LABEL = {"email": "[email hidden]", "phone": "[phone hidden]", "card": "[card number hidden]"}
NOUN = {"email": ("email address", "email addresses"), "phone": ("phone number", "phone numbers"),
        "card": ("card number", "card numbers")}


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def mask_text(text: str, own_email: str = "") -> dict:
    changes = {"email": 0, "phone": 0, "card": 0}
    own = own_email.strip().lower()

    def sub_email(m: re.Match) -> str:
        if m.group(0).lower() == own:
            return m.group(0)
        changes["email"] += 1
        return LABEL["email"]

    def sub_card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            changes["card"] += 1
            return LABEL["card"]
        return m.group(0)

    def sub_phone(m: re.Match) -> str:
        changes["phone"] += 1
        return LABEL["phone"]

    out = EMAIL.sub(sub_email, text)
    out = CARD.sub(sub_card, out)      # before phones, so a card's digit groups aren't read as phones
    out = PHONE.sub(sub_phone, out)
    parts = [f"{n} {NOUN[k][n != 1]}" for k, n in changes.items() if n]
    return {"masked_text": out, "changes": changes,
            "detail": ("masked " + ", ".join(parts)) if parts else "nothing to mask"}


async def handle(text: str, data: dict) -> dict:
    return mask_text(text, str(data.get("own_email", "")))


CARD_INFO = {
    "name": "Data Masker",
    "description": "Masks other people's emails, phone numbers and card numbers in a reply; changes nothing else.",
    "url": f"http://127.0.0.1:{PORT}/",
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "preferredTransport": "JSONRPC",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [{
        "id": "mask-pii",
        "name": "Mask PII in a reply",
        "description": 'Text part: the reply. Optional data part {"own_email": ...}, never masked. '
                       'Returns {"masked_text", "changes": {email, phone, card}, "detail"}.',
        "tags": ["security", "pii", "masking"],
    }],
}

app = build_app(CARD_INFO, handle)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
