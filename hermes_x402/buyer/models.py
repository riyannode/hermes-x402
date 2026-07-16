"""Immutable data returned by the backend-neutral buyer flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PaymentStatus = Literal[
    "not_submitted",
    "payment_started",
    "proof_created",
    "payment_succeeded",
    "submission_unknown",
    "resource_succeeded",
    "resource_failed_after_payment",
    "payment_failed",
]


@dataclass(frozen=True)
class PaymentProof:
    """A backend-created x402 proof, safe to hand to the common HTTP flow."""

    backend: str
    header_name: str
    header_value: str
    payer: str
    amount: str
    network: str
    transaction_id: str | None = None


@dataclass(frozen=True)
class ManagedPaymentResult:
    """A normalized response from a backend that owns payment and protected fetch."""

    status: int | None
    data: Any
    payer: str
    amount: str
    network: str
    payment_status: PaymentStatus
    transaction_id: str | None = None


@dataclass(frozen=True)
class BuyerResult:
    """Normalized result of a resource request, with payment lifecycle state."""

    status: int | None
    data: Any
    payer: str
    amount: str
    network: str
    payment_status: PaymentStatus
    transaction_id: str | None = None
