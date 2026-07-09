"""Seller example — aiohttp server that accepts x402 payments.

Flow:
    1. Client sends GET /premium-data without payment → 402 Payment Required
    2. Client signs payment via DCW → retries with Payment-Signature header
    3. Server settles directly via Circle Gateway → returns premium content
"""

import asyncio
import logging
import os

from aiohttp import web

from hermes_x402 import create_aiohttp_middleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seller_example")


# Create seller middleware
middleware = create_aiohttp_middleware(
    seller_address=os.environ.get("X402_SELLER_ADDRESS", "0xYOUR_WALLET_ADDRESS"),
    chain="arcTestnet",
    facilitator_url="https://gateway-api-testnet.circle.com",
)


async def premium_data(request: web.Request):
    """Protected endpoint — requires x402 payment."""
    result = await middleware.process_request(request, price="$0.01")

    if result is None:
        # Payment needed or failed — return 402
        resp_402 = request["x402_402"]
        return web.json_response(
            resp_402["body"],
            status=resp_402["status"],
            headers=resp_402["headers"],
        )

    # Payment succeeded
    return web.json_response({
        "secret": "The treasure is hidden under the doormat.",
        "paid_by": result.payer,
        "amount_usdc": result.amount,
        "network": result.network,
        "transaction": result.transaction,
    })


async def health(request: web.Request):
    """Health check — no payment required."""
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/premium-data", premium_data)
    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8080)
