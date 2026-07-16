"""Dual-role x402 agent with independent seller and buyer responsibilities."""

from __future__ import annotations

from aiohttp import web

from hermes_x402.backends.circle_dcw import CircleDcwBuyerBackend
from hermes_x402.buyer import BuyerBackend, X402BuyerService, X402BuyerTool, create_buyer_tool
from hermes_x402.buyer.errors import BuyerConfigurationError
from hermes_x402.buyer.policy import PaymentPolicy
from hermes_x402.config import X402Config
from hermes_x402.context import X402ContextBridge
from hermes_x402.middleware import create_aiohttp_middleware


class X402HermesAgent:
    """Preserves aiohttp seller middleware and delegates downstream buying."""

    def __init__(
        self,
        seller_address: str,
        chain: str = "arcTestnet",
        facilitator_url: str | None = None,
        description: str = "Paid resource",
        buyer_wallet_id: str = "",
        buyer_wallet_address: str = "",
        buyer_entity_secret: str = "",
        buyer_api_key: str | None = None,
        buyer_blockchain: str = "ARC-TESTNET",
        buyer_max_usdc: str | None = None,
        buyer_host_allowlist: list[str] | None = None,
        *,
        buyer_backend: BuyerBackend | None = None,
        buyer_service: X402BuyerService | None = None,
    ):
        if buyer_backend is not None and buyer_service is not None:
            raise BuyerConfigurationError("Provide buyer_backend or buyer_service, not both")
        legacy_buyer_supplied = any(
            (buyer_wallet_id, buyer_wallet_address, buyer_entity_secret, buyer_api_key)
        )
        if (buyer_backend is not None or buyer_service is not None) and legacy_buyer_supplied:
            raise BuyerConfigurationError(
                "Provide backend/service or legacy buyer credentials, not both"
            )
        if buyer_backend is not None:
            self.buyer = X402BuyerTool(
                backend=buyer_backend,
                policy=PaymentPolicy(
                    max_usdc=buyer_max_usdc,
                    host_allowlist=tuple(buyer_host_allowlist or ()),
                ),
            )
        elif buyer_service is not None:
            self.buyer = X402BuyerTool(backend=buyer_service.backend, policy=buyer_service.policy)
        else:
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
        if self.buyer.wallet_address.lower() == seller_address.lower():
            raise BuyerConfigurationError(
                "Buyer and seller wallets MUST be different addresses "
                "(Circle Gateway self_transfer error)"
            )
        self.seller = create_aiohttp_middleware(
            seller_address=seller_address,
            chain=chain,
            facilitator_url=facilitator_url,
            description=description,
        )

    @classmethod
    def from_config(cls, config: X402Config) -> X402HermesAgent:
        """Build a dual-role agent from explicit config or complete legacy DCW fields."""
        if config.role is None:
            legacy_complete = all(
                (
                    config.seller_address,
                    config.wallet_id,
                    config.wallet_address,
                    config.entity_secret,
                )
            )
            if not legacy_complete:
                raise BuyerConfigurationError(
                    "X402HermesAgent.from_config requires explicit role='dual' "
                    "and buyer_backend='dcw', or complete legacy seller/DCW configuration"
                )
            import warnings

            warnings.warn(
                "X402HermesAgent.from_config legacy seller/DCW configuration is deprecated; "
                "set role='dual' and buyer_backend='dcw' explicitly",
                DeprecationWarning,
                stacklevel=2,
            )
        else:
            config.validate()
            if config.role != "dual":
                raise BuyerConfigurationError("X402HermesAgent.from_config requires role='dual'")

        if config.role is None or config.buyer_backend == "dcw":
            backend = CircleDcwBuyerBackend(
                wallet_id=config.wallet_id,
                wallet_address=config.wallet_address,
                entity_secret=config.entity_secret,
                api_key=config.api_key,
                blockchain=config.blockchain,
                chain=config.chain,
            )
            return cls(
                seller_address=config.seller_address,
                chain=config.chain,
                facilitator_url=config.facilitator_url,
                buyer_backend=backend,
                buyer_max_usdc=config.max_usdc_per_payment,
                buyer_host_allowlist=config.host_allowlist,
            )
        raise BuyerConfigurationError("X402HermesAgent requires an implemented buyer backend")

    async def handle_request(self, request: web.Request, price: str) -> dict | None:
        result = await self.seller.process_request(request, price)
        return (
            None
            if result is not None
            else request.get("x402_402", {"status": 402, "body": {"error": "Payment required"}})
        )

    async def pay(
        self,
        url: str,
        method: str = "GET",
        body: dict | None = None,
        headers: dict | None = None,
        max_usdc: str | None = None,
    ):
        return await self.buyer.pay(url, method, body, headers, max_usdc)

    def get_payment_info(self) -> dict | None:
        context = X402ContextBridge.current()
        if context is None:
            return None
        return {
            "payer": context.payer,
            "amount": context.amount,
            "network": context.network,
            "transaction": context.transaction,
        }
