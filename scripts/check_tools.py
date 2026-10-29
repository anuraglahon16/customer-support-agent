"""Stage 2 check: call the MCP tools directly, with no agent, as a given customer.

The session email is bound with toolbox-core (M-5), so it disappears from the schema the
model would see; only order_id is left for the caller to fill.

    .venv/bin/python scripts/check_tools.py alice.jones@example.com 3
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from toolbox_core import ToolboxClient

load_dotenv()

TOOLBOX_URL = f"http://127.0.0.1:{os.environ.get('TOOLBOX_PORT', '5001')}"


async def main(email: str, order_id: int) -> None:
    async with ToolboxClient(TOOLBOX_URL) as client:
        tools = {
            t.__name__: t
            for t in await client.load_toolset(
                "support-agent",
                bound_params={"customer_email": email, "user_email": email},
            )
        }
        print("toolset support-agent:", sorted(tools))
        print("model-visible params of get-order-status:",
              list(tools["get-order-status"].__signature__.parameters))
        print(f"\nget-order-status(order_id={order_id}) as {email}:")
        print(await tools["get-order-status"](order_id=order_id))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
