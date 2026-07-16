"""Backend-neutral URL and payment spending policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from hermes_x402.buyer.errors import PaymentPolicyError


@dataclass(frozen=True)
class PaymentPolicy:
    """Local policy enforced before any backend signing operation."""

    max_usdc: str | None = None
    host_allowlist: tuple[str, ...] = field(default_factory=tuple)
    allow_http: bool = True  # preserves the existing local-development behavior
    daily_budget_usdc: str | None = None  # future hook; no budget persistence in this PR

    def validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise PaymentPolicyError("URL must be an absolute HTTP or HTTPS URL")
        if parsed.scheme == "http" and not self.allow_http:
            raise PaymentPolicyError("HTTP URLs are not allowed by payment policy")
        if self.host_allowlist:
            host = parsed.hostname.lower()
            if not any(
                host == item.lower() or host.endswith(f".{item.lower()}")
                for item in self.host_allowlist
            ):
                raise PaymentPolicyError(f"Host not in allowlist: {host}")

    def validate_amount(self, amount: str, override_max_usdc: str | None = None) -> None:
        cap = override_max_usdc if override_max_usdc is not None else self.max_usdc
        if cap is None:
            return
        try:
            atomic_amount = int(amount)
            max_atomic = int(Decimal(cap) * Decimal(1_000_000))
        except (InvalidOperation, ValueError) as exc:
            raise PaymentPolicyError("Payment amount or maximum USDC is invalid") from exc
        if atomic_amount < 0 or atomic_amount > max_atomic:
            raise PaymentPolicyError(f"Payment {atomic_amount} exceeds max {max_atomic} USDC")
