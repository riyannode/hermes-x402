"""Dual-role agent example — Hermes receives payment AND pays downstream.

This demonstrates the full flow:
    1. External client pays Hermes (seller mode)
    2. Hermes uses received payment to pay a downstream API (buyer mode)
    3. Payment proof propagates via ContextVar
"""

import asyncio
import logging
import os

from aiohttp import web

from hermes_x402 import X402HermesAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dual_role_example")


# Create dual-role agent
agent = X402HermesAgent(
    # Seller: receives payments
    seller_address=os.environ.get("X402_SELLER_ADDRESS", "0xSellerAddress"),
    chain="arcTestnet",
    # Buyer: pays downstream
    buyer_wallet_id=os.environ.get("CIRCLE_DCW_WALLET_ID", ""),
    buyer_wallet_address=os.environ.get("CIRCLE_DCW_WALLET_ADDRESS", ""),
    buyer_entity_secret=os.environ.get("CIRCLE_ENTITY_SECRET", ""),
)


async def analyze(request: web.Request):
    """Paid endpoint that also calls a paid downstream API."""
    # Step 1: Verify incoming payment
    error = await agent.handle_request(request, price="$0.01")
    if error:
        return web.json_response(
            error["body"],
            status=error["status"],
            headers=error.get("headers", {}),
        )

    # Step 2: Get payment info
    payment_info = agent.get_payment_info()
    logger.info("Received payment: %s", payment_info)

    # Step 3: Pay downstream API
    try:
        downstream = await agent.pay(
            url="https://api.example.com/analysis",
            method="POST",
            body={"query": "latest AI news"},
            max_usdc="0.005",
        )
        logger.info("Downstream payment: %s USDC", downstream.amount)
    except Exception as e:
        logger.warning("Downstream payment failed: %s", e)
        downstream = None

    # Step 4: Return result
    return web.json_response({
        "result": "Analysis complete",
        "paid_by": payment_info["payer"] if payment_info else "unknown",
        "downstream_paid": downstream is not None,
    })


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/analyze", analyze)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8080)
