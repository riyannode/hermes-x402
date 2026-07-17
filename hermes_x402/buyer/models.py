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
class PaymentOption:
    """A single payment option extracted from a 402 challenge."""

    scheme: str  # e.g. "x402", "x402-faucet"
    payment_system: str  # "gateway_batching" | "vanilla"
    network: str  # canonical network key from registry
    network_id: str  # CAIP-2 identifier
    amount_atomic: str  # atomic USDC amount (integer string)
    amount_usdc: str  # human-readable USDC string
    asset: str  # USDC contract address
    supported_by_backend: bool  # whether the active backend supports this network
    pay_to: str  # payTo address from challenge
    max_timeout_seconds: int  # max_seconds from challenge


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
