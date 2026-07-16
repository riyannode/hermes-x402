"""Arc configuration and explicit role/buyer-backend model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hermes_x402.buyer.errors import BuyerConfigurationError, UnsupportedBuyerBackendError

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
    "gateway_wallet": "",
    "usdc": "",
    "gateway_api": "https://gateway-api.circle.com/v1",
    "facilitator_url": "https://gateway-api.circle.com",
    "is_testnet": False,
}
CHAINS: dict[str, dict] = {"arcTestnet": ARC_TESTNET, "arcMainnet": ARC_MAINNET}


@dataclass
class X402Config:
    """Configuration where seller role and buyer backend are separate concepts."""

    seller_address: str = ""
    chain: str = "arcTestnet"
    facilitator_url: str | None = None
    role: Literal["seller", "buyer", "dual"] | None = None
    buyer_backend: Literal["dcw", "cli"] | None = None
    wallet_id: str = ""
    wallet_address: str = ""
    entity_secret: str = ""
    api_key: str | None = None
    blockchain: str = "ARC-TESTNET"
    max_usdc_per_payment: str | None = None
    daily_budget_usdc: str | None = None
    host_allowlist: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.role is None:
            return  # preserve legacy config construction; constructors select explicitly.
        if self.role not in {"seller", "buyer", "dual"}:
            raise BuyerConfigurationError(f"Unsupported x402 role: {self.role}")
        has_buyer_credentials = any(
            (self.wallet_id, self.wallet_address, self.entity_secret, self.api_key)
        )
        if self.role == "seller":
            if not self.seller_address:
                raise BuyerConfigurationError("seller role requires seller_address")
            if self.buyer_backend is not None or has_buyer_credentials:
                raise BuyerConfigurationError(
                    "seller role must not include buyer backend or credentials"
                )
            return
        if self.role == "dual" and not self.seller_address:
            raise BuyerConfigurationError("dual role requires seller_address")
        if self.buyer_backend is None:
            raise BuyerConfigurationError(f"{self.role} role requires buyer_backend")
        if self.buyer_backend == "cli":
            raise UnsupportedBuyerBackendError("buyer backend 'cli' is not implemented")
        if self.buyer_backend != "dcw":
            raise UnsupportedBuyerBackendError(f"Unsupported buyer backend: {self.buyer_backend}")
        if not all((self.wallet_id, self.wallet_address, self.entity_secret)):
            raise BuyerConfigurationError(
                "DCW buyer requires wallet_id, wallet_address, and entity_secret"
            )

    @classmethod
    def from_env(cls) -> X402Config:
        import os

        host_raw = os.environ.get("X402_HOST_ALLOWLIST", "")
        role = os.environ.get("X402_ROLE") or None
        backend = os.environ.get("X402_BUYER_BACKEND") or None
        config = cls(
            seller_address=os.environ.get("X402_SELLER_ADDRESS", ""),
            chain=os.environ.get("X402_CHAIN", "arcTestnet"),
            facilitator_url=os.environ.get("X402_FACILITATOR_URL") or None,
            role=role,  # type: ignore[arg-type]
            buyer_backend=backend,  # type: ignore[arg-type]
            wallet_id=os.environ.get("CIRCLE_DCW_WALLET_ID", ""),
            wallet_address=os.environ.get("CIRCLE_DCW_WALLET_ADDRESS", ""),
            entity_secret=os.environ.get("CIRCLE_ENTITY_SECRET", ""),
            api_key=os.environ.get("CIRCLE_API_KEY") or None,
            blockchain=os.environ.get("CIRCLE_DCW_BLOCKCHAIN", "ARC-TESTNET"),
            max_usdc_per_payment=os.environ.get("X402_MAX_USDC_PER_PAYMENT") or None,
            daily_budget_usdc=os.environ.get("X402_DAILY_BUDGET_USDC") or None,
            host_allowlist=[item.strip() for item in host_raw.split(",") if item.strip()],
        )
        config.validate()
        return config

    def get_chain_config(self) -> dict:
        if self.chain not in CHAINS:
            raise ValueError(f"Unknown chain: {self.chain}. Supported: {list(CHAINS.keys())}")
        return CHAINS[self.chain]

    def get_facilitator_url(self) -> str:
        return self.facilitator_url or self.get_chain_config()["facilitator_url"]
