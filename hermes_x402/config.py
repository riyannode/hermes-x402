"""Arc testnet/mainnet configuration and X402Config dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Chain Constants ──────────────────────────────────────────────────────────

ARC_TESTNET: dict = {
    "chain": "arcTestnet",
    "network": "eip155:5042002",
    "chain_id": 5042002,
    "domain": 26,
    "gateway_wallet": "0x0077777d7EBA4688BDeF3E311b846F25870A19B9",
    "usdc": "0x3600000000000000000000000000000000000000",
    "gateway_api": "https://gateway-api-testnet.circle.com/v1",
    "facilitator_url": "https://gateway-api-testnet.circle.com",
    "is_testnet": True,
}

ARC_MAINNET: dict = {
    "chain": "arcMainnet",
    "network": "eip155:5042001",
    "chain_id": 5042001,
    "domain": 26,
    "gateway_wallet": "",  # fill from Circle docs
    "usdc": "",  # fill from Circle docs
    "gateway_api": "https://gateway-api.circle.com/v1",
    "facilitator_url": "https://gateway-api.circle.com",
    "is_testnet": False,
}

CHAINS: dict[str, dict] = {
    "arcTestnet": ARC_TESTNET,
    "arcMainnet": ARC_MAINNET,
}


# ── Config Dataclass ─────────────────────────────────────────────────────────


@dataclass
class X402Config:
    """Unified configuration for hermes-x402.

    Seller config (for middleware):
        seller_address: Wallet address to receive payments.
        chain: Chain name (arcTestnet, arcMainnet).
        facilitator_url: Override facilitator URL (default: from chain config).

    Buyer config (for tool):
        wallet_id: Circle DCW wallet ID.
        wallet_address: Circle DCW wallet address.
        entity_secret: Circle entity secret for DCW signing.
        api_key: Circle API key (optional, defaults to CIRCLE_API_KEY env).
        blockchain: Circle blockchain identifier (default: ARC-TESTNET).

    Policy config (optional):
        max_usdc_per_payment: Max USDC per single payment.
        daily_budget_usdc: Daily spending cap.
        host_allowlist: Allowed hostnames (empty = allow all).
    """

    # Seller
    seller_address: str = ""
    chain: str = "arcTestnet"
    facilitator_url: Optional[str] = None

    # Buyer (DCW)
    wallet_id: str = ""
    wallet_address: str = ""
    entity_secret: str = ""
    api_key: Optional[str] = None
    blockchain: str = "ARC-TESTNET"

    # Policy
    max_usdc_per_payment: Optional[str] = None
    daily_budget_usdc: Optional[str] = None
    host_allowlist: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "X402Config":
        """Create config from environment variables.

        Reads:
            X402_SELLER_ADDRESS, X402_CHAIN, X402_FACILITATOR_URL
            CIRCLE_DCW_WALLET_ID, CIRCLE_DCW_WALLET_ADDRESS, CIRCLE_ENTITY_SECRET
            CIRCLE_API_KEY, CIRCLE_DCW_BLOCKCHAIN
            X402_MAX_USDC_PER_PAYMENT, X402_DAILY_BUDGET_USDC, X402_HOST_ALLOWLIST
        """
        import os

        host_raw = os.environ.get("X402_HOST_ALLOWLIST", "")
        hosts = [h.strip() for h in host_raw.split(",") if h.strip()] if host_raw else []

        return cls(
            seller_address=os.environ.get("X402_SELLER_ADDRESS", ""),
            chain=os.environ.get("X402_CHAIN", "arcTestnet"),
            facilitator_url=os.environ.get("X402_FACILITATOR_URL") or None,
            wallet_id=os.environ.get("CIRCLE_DCW_WALLET_ID", ""),
            wallet_address=os.environ.get("CIRCLE_DCW_WALLET_ADDRESS", ""),
            entity_secret=os.environ.get("CIRCLE_ENTITY_SECRET", ""),
            api_key=os.environ.get("CIRCLE_API_KEY") or None,
            blockchain=os.environ.get("CIRCLE_DCW_BLOCKCHAIN", "ARC-TESTNET"),
            max_usdc_per_payment=os.environ.get("X402_MAX_USDC_PER_PAYMENT") or None,
            daily_budget_usdc=os.environ.get("X402_DAILY_BUDGET_USDC") or None,
            host_allowlist=hosts,
        )

    def get_chain_config(self) -> dict:
        """Get chain config dict for the configured chain."""
        if self.chain not in CHAINS:
            raise ValueError(f"Unknown chain: {self.chain}. Supported: {list(CHAINS.keys())}")
        return CHAINS[self.chain]

    def get_facilitator_url(self) -> str:
        """Get facilitator URL (config override → chain default)."""
        if self.facilitator_url:
            return self.facilitator_url
        return self.get_chain_config()["facilitator_url"]
