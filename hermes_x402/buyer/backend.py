"""Contract implemented by buyer wallet backends."""

from __future__ import annotations

from typing import Any, Protocol

from hermes_x402.buyer.models import PaymentProof


class BuyerBackend(Protocol):
    """Backend-specific signing/payment behavior only; never HTTP policy."""

    @property
    def name(self) -> str: ...

    @property
    def wallet_address(self) -> str: ...

    async def create_payment_proof(
        self,
        *,
        url: str,
        method: str,
        body: dict[str, Any] | None,
        payment_required: dict[str, Any],
    ) -> PaymentProof: ...
