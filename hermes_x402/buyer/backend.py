"""Contract implemented by buyer wallet backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from hermes_x402.buyer.models import ManagedPaymentResult, PaymentProof


class BuyerBackend(Protocol):
    """Backend-specific signing/payment behavior only; never HTTP policy."""

    @property
    def name(self) -> str: ...

    @property
    def wallet_address(self) -> str: ...


@runtime_checkable
class PaymentProofBackend(BuyerBackend, Protocol):
    """Backend that creates a proof for the common protected-resource retry."""

    async def create_payment_proof(
        self,
        *,
        url: str,
        method: str,
        body: dict[str, Any] | None,
        payment_required: dict[str, Any],
    ) -> PaymentProof: ...


@runtime_checkable
class ManagedPaymentBackend(BuyerBackend, Protocol):
    """Backend that owns the official payment-and-fetch mechanism."""

    async def pay_and_fetch(
        self,
        *,
        url: str,
        method: str,
        body: dict[str, Any] | None,
        headers: dict[str, str],
        payment_required: dict[str, Any],
        max_usdc: str | None,
    ) -> ManagedPaymentResult: ...
