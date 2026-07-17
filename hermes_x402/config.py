"""Configuration for the x402 plugin — env vars, validation, and role/buyer-backend model.

Extends the existing X402Config with discovery, network policy, approval,
daily budget, and multi-network support from PR #4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from hermes_x402.buyer.errors import BuyerConfigurationError, UnsupportedBuyerBackendError

# ---------------------------------------------------------------------------
# Legacy chain configs (kept for backward compatibility)
# ---------------------------------------------------------------------------

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
    """Configuration where seller role and buyer backend are separate concepts.

    Extended in PR #4 with:
      - network_policy: strict_allowlist | public
      - discovery_providers: tuple[str, ...]
      - discovery_host_allowlist: tuple[str, ...]
      - network_preference: tuple[str, ...]
      - require_gateway_batching: bool
      - require_approval_for_new_host: bool
      - daily_budget_usdc: str | None
      - allow_http: bool
    """

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
    circle_cli_executable: str = "circle"
    circle_cli_cwd: str | None = None
    circle_cli_wallet_address: str = ""
    circle_cli_network: str = ""
    max_usdc_per_payment: str | None = None
    host_allowlist: list[str] = field(default_factory=list)
    # PR #4 extensions
    network_policy: Literal["strict_allowlist", "public"] = "strict_allowlist"
    discovery_providers: tuple[str, ...] = ("circle_marketplace",)
    discovery_host_allowlist: tuple[str, ...] = ()
    network_preference: tuple[str, ...] = ("base",)
    require_gateway_batching: bool = True
    require_approval_for_new_host: bool = False
    daily_budget_usdc: str | None = None
    allow_http: bool = False

    def validate(self) -> None:
        if self.role is None:
            return  # preserve legacy config construction; constructors select explicitly.
        if self.role not in {"seller", "buyer", "dual"}:
            raise BuyerConfigurationError(f"Unsupported x402 role: {self.role}")
        dcw_values = (self.wallet_id, self.wallet_address, self.entity_secret, self.api_key)
        cli_values = (self.circle_cli_wallet_address, self.circle_cli_network)
        has_dcw = any(dcw_values)
        has_cli = any(cli_values)
        if self.role == "seller":
            if not self.seller_address:
                raise BuyerConfigurationError("seller role requires seller_address")
            if self.buyer_backend is not None or has_dcw or has_cli:
                raise BuyerConfigurationError(
                    "seller role must not include buyer backend or buyer credentials"
                )
            return
        if self.role == "dual" and not self.seller_address:
            raise BuyerConfigurationError("dual role requires seller_address")
        if self.buyer_backend is None:
            raise BuyerConfigurationError(f"{self.role} role requires buyer_backend")
        if self.buyer_backend == "dcw":
            if has_cli:
                raise BuyerConfigurationError("DCW buyer must not include Circle CLI configuration")
            if not all((self.wallet_id, self.wallet_address, self.entity_secret)):
                raise BuyerConfigurationError(
                    "DCW buyer requires wallet_id, wallet_address, and entity_secret"
                )
            return
        if self.buyer_backend == "cli":
            if has_dcw:
                raise BuyerConfigurationError("Circle CLI buyer must not include DCW credentials")
            if not all((self.circle_cli_wallet_address, self.circle_cli_network)):
                raise BuyerConfigurationError(
                    "Circle CLI buyer requires circle_cli_wallet_address and circle_cli_network"
                )
            if self.max_usdc_per_payment is None:
                raise BuyerConfigurationError("Circle CLI buyer requires max_usdc_per_payment")
            if self.circle_cli_executable != "circle":
                raise BuyerConfigurationError(
                    "Circle CLI buyer only permits the official 'circle' executable"
                )
            if self.circle_cli_cwd is not None:
                raise BuyerConfigurationError(
                    "Circle CLI buyer does not permit a custom working directory"
                )
            return
        raise UnsupportedBuyerBackendError(f"Unsupported buyer backend: {self.buyer_backend}")

    # Validate daily budget
    def validate_daily_budget(self) -> str | None:
        """Validate and return the daily budget, or None if unset/invalid."""
        if self.daily_budget_usdc is None:
            return None
        try:
            value = Decimal(self.daily_budget_usdc)
        except (InvalidOperation, ValueError):
            return None
        if not value.is_finite() or value < 0:
            return None
        return str(value)

    @classmethod
    def from_env(cls) -> X402Config:
        host_raw = os.environ.get("X402_HOST_ALLOWLIST", "")
        role = os.environ.get("X402_ROLE") or None
        backend = os.environ.get("X402_BUYER_BACKEND") or None

        # PR #4 env vars
        network_policy_raw = (
            os.environ.get("X402_NETWORK_POLICY", "strict_allowlist").strip().lower()
        )
        if network_policy_raw not in {"strict_allowlist", "public"}:
            network_policy_raw = "strict_allowlist"

        discovery_providers_raw = os.environ.get("X402_DISCOVERY_PROVIDERS", "circle_marketplace")
        discovery_providers = tuple(
            item.strip() for item in discovery_providers_raw.split(",") if item.strip()
        )

        discovery_host_allowlist_raw = os.environ.get("X402_DISCOVERY_HOST_ALLOWLIST", "")
        discovery_host_allowlist = tuple(
            item.strip() for item in discovery_host_allowlist_raw.split(",") if item.strip()
        )

        network_preference_raw = os.environ.get("X402_NETWORK_PREFERENCE", "base")
        network_preference = tuple(
            item.strip() for item in network_preference_raw.split(",") if item.strip()
        )

        require_gateway_raw = (
            os.environ.get("X402_REQUIRE_GATEWAY_BATCHING", "true").strip().lower()
        )
        require_gateway_batching = require_gateway_raw in {"1", "true", "yes"}

        require_approval_raw = (
            os.environ.get("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "false").strip().lower()
        )
        require_approval_for_new_host = require_approval_raw in {"1", "true", "yes"}

        allow_http_raw = os.environ.get("X402_ALLOW_HTTP", "").strip().lower()
        allow_http = allow_http_raw in {"1", "true", "yes"}

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
            circle_cli_executable=os.environ.get("CIRCLE_CLI_EXECUTABLE", "circle"),
            circle_cli_cwd=os.environ.get("CIRCLE_CLI_CWD") or None,
            circle_cli_wallet_address=os.environ.get("CIRCLE_AGENT_WALLET_ADDRESS", ""),
            circle_cli_network=os.environ.get("CIRCLE_AGENT_WALLET_NETWORK", ""),
            max_usdc_per_payment=os.environ.get("X402_MAX_USDC_PER_PAYMENT") or None,
            host_allowlist=[item.strip() for item in host_raw.split(",") if item.strip()],
            # PR #4
            network_policy=network_policy_raw,  # type: ignore[arg-type]
            discovery_providers=discovery_providers,
            discovery_host_allowlist=discovery_host_allowlist,
            network_preference=network_preference,
            require_gateway_batching=require_gateway_batching,
            require_approval_for_new_host=require_approval_for_new_host,
            daily_budget_usdc=os.environ.get("X402_DAILY_BUDGET_USDC") or None,
            allow_http=allow_http,
        )
        config.validate()
        return config

    def get_chain_config(self) -> dict:
        if self.chain not in CHAINS:
            raise ValueError(f"Unknown chain: {self.chain}. Supported: {list(CHAINS.keys())}")
        return CHAINS[self.chain]

    def get_facilitator_url(self) -> str:
        return self.facilitator_url or self.get_chain_config()["facilitator_url"]
