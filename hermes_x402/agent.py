"""Dual-role x402 agent — receives payments AND pays downstream.

Combines seller middleware (aiohttp) + buyer tool (DCW) into one class.
Two different wallets: seller receives, buyer pays.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

from hermes_x402.buyer import X402BuyerTool, create_buyer_tool
from hermes_x402.config import X402Config
from hermes_x402.context import X402ContextBridge
from hermes_x402.middleware import X402SellerMiddleware, create_aiohttp_middleware

logger = logging.getLogger("hermes_x402.agent")


class X402HermesAgent:
    """Dual-role x402 agent for Hermes.

    - Seller: Accepts incoming payments via aiohttp middleware
    - Buyer: Pays downstream x402 APIs via DCW signing
    - Context: Payment proof propagates from seller → buyer via ContextVar

    Usage:
        agent = X402HermesAgent(
            seller_address="0xSeller...",
            buyer_wallet_id="...",
            buyer_wallet_address="0xBuyer...",
            buyer_entity_secret="...",
        )

        # In aiohttp handler:
        result = await agent.handle_request(request, price="$0.01")
        if result is None:
            return web.json_response(**request["x402_402"])

        # Pay downstream:
        response = await agent.pay("https://api.example.com/premium")
    """

    def __init__(
        self,
        # Seller config
        seller_address: str,
        chain: str = "arcTestnet",
        facilitator_url: Optional[str] = None,
        description: str = "Paid resource",
        # Buyer config
        buyer_wallet_id: str = "",
        buyer_wallet_address: str = "",
        buyer_entity_secret: str = "",
        buyer_api_key: Optional[str] = None,
        buyer_blockchain: str = "ARC-TESTNET",
        buyer_max_usdc: Optional[str] = None,
        buyer_host_allowlist: Optional[list[str]] = None,
    ):
        # Validate buyer and seller are different addresses
        if buyer_wallet_address and seller_address:
            if buyer_wallet_address.lower() == seller_address.lower():
                raise ValueError(
                    "Buyer and seller wallets MUST be different addresses "
                    "(Circle Gateway self_transfer error)"
                )

        self.seller = create_aiohttp_middleware(
            seller_address=seller_address,
            chain=chain,
            facilitator_url=facilitator_url,
            description=description,
        )

        self.buyer = create_buyer_tool(
            wallet_id=buyer_wallet_id,
            wallet_address=buyer_wallet_address,
            entity_secret=buyer_entity_secret,
            api_key=buyer_api_key,
            blockchain=buyer_blockchain,
            chain=chain,
            max_usdc=buyer_max_usdc,
            host_allowlist=buyer_host_allowlist,
        )

    @classmethod
    def from_config(cls, config: X402Config) -> "X402HermesAgent":
        """Create agent from X402Config."""
        return cls(
            seller_address=config.seller_address,
            chain=config.chain,
            facilitator_url=config.facilitator_url,
            buyer_wallet_id=config.wallet_id,
            buyer_wallet_address=config.wallet_address,
            buyer_entity_secret=config.entity_secret,
            buyer_api_key=config.api_key,
            buyer_blockchain=config.blockchain,
            buyer_max_usdc=config.max_usdc_per_payment,
            buyer_host_allowlist=config.host_allowlist,
        )

    async def handle_request(
        self,
        request: web.Request,
        price: str,
    ) -> Optional[dict]:
        """Handle incoming aiohttp request for x402 payment.

        Returns:
            None if payment succeeded (caller proceeds).
            Dict with 402 response if payment needed/failed.
        """
        result = await self.seller.process_request(request, price)
        if result is None:
            return request.get("x402_402", {"status": 402, "body": {"error": "Payment required"}})
        return None

    async def pay(
        self,
        url: str,
        method: str = "GET",
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        max_usdc: Optional[str] = None,
    ):
        """Pay for a downstream x402 resource."""
        return await self.buyer.pay(
            url=url,
            method=method,
            body=body,
            headers=headers,
            max_usdc=max_usdc,
        )

    def get_payment_info(self) -> Optional[dict]:
        """Get current payment context (who paid, how much)."""
        ctx = X402ContextBridge.current()
        if ctx is None:
            return None
        return {
            "payer": ctx.payer,
            "amount": ctx.amount,
            "network": ctx.network,
            "transaction": ctx.transaction,
        }
