"""The support agent and the per-user session it runs in.

Decision (stage 3): the session email is bound into the toolset at login, and each
logged-in user gets their own agent + runner. Nothing the model writes can change which
customer the tools query (M-5).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from toolbox_core import ToolboxClient

from support.models import gen_config, llm

load_dotenv()

APP_NAME = "customer_support"
AGENT_NAME = "support_agent"
AGENT_TOOLSET = "support-agent"   # the only toolset the agent loads (M-6)
AUTH_TOOLSET = "auth"             # login only; never given to the agent
MODEL = os.environ.get("AGENT_MODEL", os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview"))
TOOLBOX_URL = f"http://127.0.0.1:{os.environ.get('TOOLBOX_PORT', '5001')}"

INSTRUCTION = """\
You are the customer support agent for an online electronics and home-goods shop.
You help the logged-in customer with their own orders, deliveries, returns,
cancellations, account details and delivery preferences.

Tools:
- get-order-status: one order by its number.
- find-customer-orders: the customer's order history, newest first. Use it for
  "my last order", "my orders", or to find an order by product name.
- action-log: record a cancellation, return, address, preference or profile change
  the customer explicitly asks for. It does NOT change the order; say the request
  has been logged for the team. If the request doesn't clearly identify one order,
  ask which order first and do not log anything. Before logging an action on a
  specific order, look that order up with get-order-status; if it returns nothing,
  say you can't find that order on their account and do not call action-log.

Rules:
- Every order fact you state (status, items, price, date, address) must come from a
  tool call in this conversation turn. Never guess or reuse an earlier answer.
- When you give an order's status, use the status word the tool returned (PROCESSING,
  SHIPPED, DELIVERED, CANCELLED or RETURNED), then explain it in plain words if useful.
- The tools only ever return this customer's own orders. If a tool returns nothing
  for an order number, say "I can't find that order on your account." Do not say
  whether it exists for someone else.
- You can only act for the logged-in customer. If asked to look up another person's
  orders, explain you can only help with their own account.
- A message may start with "Relevant memories about this customer (from Mem0):". Use
  those for the customer's standing preferences (delivery instructions, contact
  method, how to address them). For any order fact, the tools win: never state an
  order's status, items or price from a memory.
- Be brief: one or two sentences for simple questions.
"""


def decode_result(raw: Any) -> list[dict]:
    """Toolbox returns JSON text: an object for one row, a list for many, null for none."""
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else None
    if isinstance(raw, dict) and set(raw) == {"result"}:    # older {"result": "<json>"} wrapper
        return decode_result(raw["result"])
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


@dataclass
class UserSession:
    email: str
    full_name: str
    is_premium: bool
    runner: Runner
    session_id: str
    client: ToolboxClient

    async def close(self) -> None:
        await self.client.close()


async def verify_login(email: str, password: str) -> dict | None:
    async with ToolboxClient(TOOLBOX_URL) as client:
        (login,) = await client.load_toolset(AUTH_TOOLSET)
        rows = decode_result(await login(email=email, password=password))
    return rows[0] if rows else None


async def open_session(profile: dict) -> UserSession:
    """Build this user's agent with their email bound into every tool."""
    email = profile["email"]
    client = ToolboxClient(TOOLBOX_URL)
    tools = await client.load_toolset(
        AGENT_TOOLSET,
        bound_params={"customer_email": email, "user_email": email},
        strict=False,
    )
    agent = LlmAgent(
        name=AGENT_NAME,
        model=llm(MODEL),
        generate_content_config=gen_config(MODEL, classifier=False),
        description="Answers a logged-in customer's questions about their own orders.",
        instruction=INSTRUCTION,
        tools=tools,
    )
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
    session = await sessions.create_session(app_name=APP_NAME, user_id=email)
    return UserSession(email, profile["full_name"], profile["is_premium_customer"],
                       runner, session.id, client)
