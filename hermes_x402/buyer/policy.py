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

    @staticmethod
    def normalize_max_usdc(cap: str) -> str:
        """Canonical decimal USDC suitable for the CLI's ``--max-amount`` flag."""
        try:
            value = Decimal(cap)
        except (InvalidOperation, ValueError) as exc:
            raise PaymentPolicyError("Payment maximum USDC is invalid") from exc
        exponent = value.as_tuple().exponent
        if not value.is_finite() or value < 0 or not isinstance(exponent, int) or exponent < -6:
            raise PaymentPolicyError(
                "Payment maximum USDC must be non-negative with at most 6 decimals"
            )
        return format(value.normalize(), "f") if value else "0"

    def validate_amount(self, amount: str, override_max_usdc: str | None = None) -> None:
        try:
            atomic_amount = int(amount)
        except (TypeError, ValueError) as exc:
            raise PaymentPolicyError("Payment amount is invalid") from exc
        if atomic_amount < 0:
            raise PaymentPolicyError("Payment amount must not be negative")
        cap = override_max_usdc if override_max_usdc is not None else self.max_usdc
        if cap is None:
            return
        max_atomic = int(Decimal(self.normalize_max_usdc(cap)) * Decimal(1_000_000))
        if atomic_amount > max_atomic:
            raise PaymentPolicyError(f"Payment {atomic_amount} exceeds max {max_atomic} USDC")
