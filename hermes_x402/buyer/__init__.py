"""Public buyer API with compatibility support for legacy Circle DCW arguments."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from hermes_x402.buyer.backend import BuyerBackend
from hermes_x402.buyer.errors import BuyerConfigurationError
from hermes_x402.buyer.models import BuyerResult
from hermes_x402.buyer.policy import PaymentPolicy
from hermes_x402.buyer.service import X402BuyerService

if TYPE_CHECKING:
    from hermes_x402.backends.circle_dcw import CircleDcwBuyerBackend


class X402BuyerTool:
    """Compatibility façade over :class:`X402BuyerService`."""

    def __init__(
        self,
        wallet_id: str | None = None,
        wallet_address: str | None = None,
        entity_secret: str | None = None,
        api_key: str | None = None,
        blockchain: str = "ARC-TESTNET",
        chain: str = "arcTestnet",
        max_usdc: str | None = None,
        host_allowlist: list[str] | None = None,
        *,
        backend: BuyerBackend | None = None,
        policy: PaymentPolicy | None = None,
    ):
        legacy_values = (wallet_id, wallet_address, entity_secret, api_key)
        legacy_supplied = any(value is not None for value in legacy_values)
        if backend is not None and legacy_supplied:
            raise BuyerConfigurationError(
                "Provide either backend or legacy DCW arguments, not both"
            )
        if backend is None:
            if not wallet_id or not wallet_address or not entity_secret:
                raise BuyerConfigurationError(
                    "DCW wallet_id, wallet_address, and entity_secret are required"
                )
            warnings.warn(
                "Legacy DCW arguments are deprecated; pass "
                "backend=CircleDcwBuyerBackend(...) instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            from hermes_x402.backends.circle_dcw import CircleDcwBuyerBackend

            backend = CircleDcwBuyerBackend(
                wallet_id=wallet_id,
                wallet_address=wallet_address,
                entity_secret=entity_secret,
                api_key=api_key,
                blockchain=blockchain,
                chain=chain,
            )
        self.backend = backend
        self.policy = policy or PaymentPolicy(
            max_usdc=max_usdc, host_allowlist=tuple(host_allowlist or ())
        )
        self.service = X402BuyerService(backend=backend, policy=self.policy)

    @property
    def wallet_address(self) -> str:
        return self.backend.wallet_address

    @property
    def wallet_id(self) -> str | None:
        return getattr(self.backend, "wallet_id", None)

    @property
    def blockchain(self) -> str | None:
        return getattr(self.backend, "blockchain", None)

    @property
    def chain(self) -> str | None:
        return getattr(self.backend, "chain", None)

    @property
    def max_usdc(self) -> str | None:
        return self.policy.max_usdc

    @max_usdc.setter
    def max_usdc(self, value: str | None) -> None:
        self.policy = PaymentPolicy(
            max_usdc=value,
            host_allowlist=self.policy.host_allowlist,
            allow_http=self.policy.allow_http,
            daily_budget_usdc=self.policy.daily_budget_usdc,
        )
        self.service = X402BuyerService(backend=self.backend, policy=self.policy)

    @property
    def host_allowlist(self) -> list[str]:
        return list(self.policy.host_allowlist)

    @host_allowlist.setter
    def host_allowlist(self, value: list[str]) -> None:
        self.policy = PaymentPolicy(
            max_usdc=self.policy.max_usdc,
            host_allowlist=tuple(value),
            allow_http=self.policy.allow_http,
            daily_budget_usdc=self.policy.daily_budget_usdc,
        )
        self.service = X402BuyerService(backend=self.backend, policy=self.policy)

    def _check_host(self, url: str) -> bool:
        try:
            self.policy.validate_url(url)
        except Exception:
            return False
        return True

    async def pay(
        self,
        url: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_usdc: str | None = None,
    ) -> BuyerResult:
        return await self.service.pay(url, method, body, headers, max_usdc)


def create_buyer_tool(
    wallet_id: str | None = None,
    wallet_address: str | None = None,
    entity_secret: str | None = None,
    api_key: str | None = None,
    blockchain: str = "ARC-TESTNET",
    chain: str = "arcTestnet",
    max_usdc: str | None = None,
    host_allowlist: list[str] | None = None,
    *,
    backend: BuyerBackend | None = None,
) -> X402BuyerTool:
    """Create an x402 buyer tool from a backend or legacy Circle DCW credentials."""
    return X402BuyerTool(
        wallet_id=wallet_id,
        wallet_address=wallet_address,
        entity_secret=entity_secret,
        api_key=api_key,
        blockchain=blockchain,
        chain=chain,
        max_usdc=max_usdc,
        host_allowlist=host_allowlist,
        backend=backend,
    )


def __getattr__(name: str):
    if name == "CircleDcwBuyerBackend":
        from hermes_x402.backends.circle_dcw import CircleDcwBuyerBackend

        return CircleDcwBuyerBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BuyerBackend",
    "BuyerResult",
    "CircleDcwBuyerBackend",
    "PaymentPolicy",
    "X402BuyerService",
    "X402BuyerTool",
    "create_buyer_tool",
]
