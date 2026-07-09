"""Buyer example — pay for x402-protected resources using Circle DCW.

Flow:
    1. Request resource → get 402 with payment requirements
    2. Sign payment via Circle DCW (no raw private key)
    3. Retry with Payment-Signature header
    4. Get premium content
"""

import asyncio
import logging
import os

from hermes_x402 import create_buyer_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("buyer_example")


async def main():
    # Create buyer tool with DCW credentials
    tool = create_buyer_tool(
        wallet_id=os.environ.get("CIRCLE_DCW_WALLET_ID", ""),
        wallet_address=os.environ.get("CIRCLE_DCW_WALLET_ADDRESS", ""),
        entity_secret=os.environ.get("CIRCLE_ENTITY_SECRET", ""),
        chain="arcTestnet",
        max_usdc="0.01",  # Spending cap
        host_allowlist=["localhost", "127.0.0.1"],  # Only allow local sellers
    )

    # Pay for a resource
    try:
        result = await tool.pay(
            url="http://localhost:8080/premium-data",
            max_usdc="0.001",
        )
        logger.info("Status: %d", result.status)
        logger.info("Data: %s", result.data)
        logger.info("Paid: %s USDC by %s", result.amount, result.payer)
    except Exception as e:
        logger.error("Payment failed: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
