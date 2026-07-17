"""x402 supports preflight — discover payment options without signing or paying.

Issues an unpaid HTTP request, parses the 402 Payment-Required challenge
(v2 base64 header or v1 body challenge), and returns a structured summary
of supported/unsupported payment options.

NEVER signs, settles, deposits, or pays.  This module is read-only.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

from hermes_x402.buyer.challenge import PAYMENT_REQUIRED_HEADER
from hermes_x402.buyer.errors import (
    InvalidPaymentChallengeError,
    PaymentPolicyError,
)
from hermes_x402.buyer.models import PaymentOption
from hermes_x402.buyer.options import select_payment_option
from hermes_x402.network_policy import NetworkPolicy, parse_network_policy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_HEADER_SIZE = 8192  # bytes — bound Payment-Required header length
MAX_BODY_SIZE = 65536  # bytes — bound challenge body length (v1 fallback)
MAX_URL_LENGTH = 2048

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupportResult:
    """Structured result of a supports preflight check."""

    supported: bool
    x402: bool
    gateway_batching: bool
    resource: str  # the URL that was checked
    method: str  # HTTP method
    version: str  # "1" or "2"
    options: tuple[PaymentOption, ...] = ()
    unsupported_networks: tuple[str, ...] = ()
    preferred_option: PaymentOption | None = None
    reason: str | None = None
    payment_required: bool = True  # False for free 200 responses

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dictionary representation."""
        opts = []
        for o in self.options:
            opts.append(
                {
                    "scheme": o.scheme,
                    "payment_system": o.payment_system,
                    "network": o.network,
                    "network_id": o.network_id,
                    "amount_atomic": o.amount_atomic,
                    "amount_usdc": o.amount_usdc,
                    "asset": o.asset,
                    "supported_by_backend": o.supported_by_backend,
                    "pay_to": o.pay_to,
                    "max_timeout_seconds": o.max_timeout_seconds,
                }
            )
        preferred = None
        if self.preferred_option:
            p = self.preferred_option
            preferred = {
                "scheme": p.scheme,
                "payment_system": p.payment_system,
                "network": p.network,
                "network_id": p.network_id,
                "amount_atomic": p.amount_atomic,
                "amount_usdc": p.amount_usdc,
                "asset": p.asset,
                "supported_by_backend": p.supported_by_backend,
                "pay_to": p.pay_to,
                "max_timeout_seconds": p.max_timeout_seconds,
            }
        return {
            "supported": self.supported,
            "x402": self.x402,
            "gateway_batching": self.gateway_batching,
            "resource": self.resource,
            "method": self.method,
            "version": self.version,
            "options": opts,
            "unsupported_networks": list(self.unsupported_networks),
            "preferred_option": preferred,
            "reason": self.reason,
            "payment_required": self.payment_required,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_backend_support(
    network_key: str,
    *,
    configured_backend: str | None,
    wallet_network: str | None,
) -> bool:
    """Check if the network is supported by the configured backend."""
    if not configured_backend:
        return False
    try:
        from hermes_x402.networks import get_network

        net = get_network(network_key)
    except Exception:
        return False

    if configured_backend == "cli":
        return net.buyer_cli_supported
    elif configured_backend == "dcw":
        return net.buyer_dcw_supported
    return False


def _resolve_network(caip2: str) -> str | None:
    """Resolve a CAIP-2 string to the canonical network key. Returns None on failure."""
    try:
        from hermes_x402.networks import get_network

        net = get_network(caip2)
        return net.key
    except Exception:
        return None


def _determine_payment_system(accept_entry: dict[str, Any]) -> str:
    """Determine payment system from challenge accept entry.

    GatewayWalletBatched detection: accept.extra.name == "GatewayWalletBatched"
    (NOT from scheme alone).
    """
    extra = accept_entry.get("extra") or {}
    if isinstance(extra, dict) and extra.get("name") == "GatewayWalletBatched":
        return "gateway_batching"
    return "vanilla"


def _amount_to_usdc(atomic_amount: str) -> str:
    """Convert atomic USDC amount (6 decimals) to human-readable string."""
    try:
        val = Decimal(atomic_amount) / Decimal(1_000_000)
        return format(val.normalize(), "f") if val else "0"
    except (InvalidOperation, ValueError):
        return atomic_amount


def _parse_accept_entry(
    accept_entry: dict[str, Any],
    *,
    configured_backend: str | None,
    wallet_network: str | None,
) -> PaymentOption | None:
    """Parse a single accept entry from the challenge into a PaymentOption.

    Returns None if the entry is malformed or the network is unresolvable.
    """
    if not isinstance(accept_entry, dict):
        return None

    network_id = accept_entry.get("network") or ""
    if not network_id:
        return None

    # Resolve to canonical network key via centralized registry
    network_key = _resolve_network(network_id)

    scheme = accept_entry.get("scheme") or "x402"
    payment_system = _determine_payment_system(accept_entry)

    amount_raw = accept_entry.get("amount") or "0"
    asset = accept_entry.get("asset") or ""
    pay_to = accept_entry.get("payTo") or ""

    max_seconds_raw = accept_entry.get("maxSecondsValid")
    max_timeout = int(max_seconds_raw) if isinstance(max_seconds_raw, (int, float)) else 300

    supported = _detect_backend_support(
        network_key or network_id,
        configured_backend=configured_backend,
        wallet_network=wallet_network,
    )

    return PaymentOption(
        scheme=scheme,
        payment_system=payment_system,
        network=network_key or network_id,
        network_id=network_id,
        amount_atomic=amount_raw,
        amount_usdc=_amount_to_usdc(amount_raw),
        asset=asset,
        supported_by_backend=supported,
        pay_to=pay_to,
        max_timeout_seconds=max_timeout,
    )


def _parse_v2_challenge(
    header_value: str,
    *,
    configured_backend: str | None,
    wallet_network: str | None,
) -> tuple[str, tuple[PaymentOption, ...], tuple[str, ...]]:
    """Parse x402 v2 Payment-Required header (base64-encoded JSON).

    Returns (version, options, unsupported_networks).
    """
    # Bound header length
    bounded = header_value[:MAX_HEADER_SIZE]

    try:
        decoded = base64.b64decode(bounded, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidPaymentChallengeError("Payment-Required header is not valid base64") from None

    try:
        challenge = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidPaymentChallengeError("Payment-Required header is not valid JSON") from exc

    if not isinstance(challenge, dict):
        raise InvalidPaymentChallengeError("Payment-Required payload must be an object")

    version = str(challenge.get("x402Version") or "2")
    accepts = challenge.get("accepts") or []
    if not isinstance(accepts, list) or not accepts:
        raise InvalidPaymentChallengeError("No accepted payment methods in 402 response")

    options: list[PaymentOption] = []
    unsupported: list[str] = []

    for entry in accepts:
        opt = _parse_accept_entry(
            entry,
            configured_backend=configured_backend,
            wallet_network=wallet_network,
        )
        if opt is None:
            continue

        if not _resolve_network(opt.network_id):
            unsupported.append(opt.network_id)
        options.append(opt)

    return version, tuple(options), tuple(unsupported)


def _parse_v1_challenge(
    response: httpx.Response,
    *,
    configured_backend: str | None,
    wallet_network: str | None,
) -> tuple[tuple[PaymentOption, ...], tuple[str, ...]]:
    """Parse x402 v1 body challenge (content-type based detection).

    Only triggered when the response body contains a v1-format challenge.
    """
    content_type = response.headers.get("content-type", "")
    if "application/x402-challenge" not in content_type.lower():
        # Not a v1 challenge — skip
        return (), ()

    raw = response.content[:MAX_BODY_SIZE]
    try:
        challenge = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (), ()

    if not isinstance(challenge, dict):
        return (), ()

    accepts = challenge.get("accepts") or []
    if not isinstance(accepts, list):
        return (), ()

    options: list[PaymentOption] = []
    unsupported: list[str] = []

    for entry in accepts:
        opt = _parse_accept_entry(
            entry,
            configured_backend=configured_backend,
            wallet_network=wallet_network,
        )
        if opt is None:
            continue

        if not _resolve_network(opt.network_id):
            unsupported.append(opt.network_id)
        options.append(opt)

    return tuple(options), tuple(unsupported)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_supports(
    url: str,
    method: str = "GET",
    *,
    config: Any = None,
) -> SupportResult:
    """Check x402 support for a URL without signing, paying, or depositing.

    Issues an unpaid HTTP request and parses the 402 challenge to determine
    what payment options the server supports.

    Args:
        url: The resource URL to check.
        method: HTTP method (default GET).
        config: Optional X402Config-like object with buyer_backend and
                wallet network information.

    Returns:
        SupportResult with full payment option details.
    """
    normalized_method = method.upper()
    urlparse(url)

    # --- URL validation via network policy ---
    network_policy = parse_network_policy()
    # Override with config if available
    if config and hasattr(config, "host_allowlist") and config.host_allowlist:
        network_policy = NetworkPolicy(
            mode=network_policy.mode,
            host_allowlist=tuple(config.host_allowlist),
            allow_http=network_policy.allow_http,
        )
    try:
        network_policy.validate_url(url)
    except PaymentPolicyError as exc:
        return SupportResult(
            supported=False,
            x402=False,
            gateway_batching=False,
            resource=url,
            method=normalized_method,
            version="",
            reason=str(exc),
            payment_required=False,
        )

    # --- Determine backend info ---
    configured_backend: str | None = None
    wallet_network: str | None = None
    if config:
        configured_backend = getattr(config, "buyer_backend", None)
        wallet_network = getattr(config, "blockchain", None) or getattr(config, "network", None)

    # --- Issue unpaid HTTP request ---
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.request(
                method=normalized_method,
                url=url,
            )
    except httpx.HTTPError as exc:
        return SupportResult(
            supported=False,
            x402=False,
            gateway_batching=False,
            resource=url,
            method=normalized_method,
            version="",
            reason=f"HTTP request failed: {type(exc).__name__}",
            payment_required=False,
        )

    # --- Free HTTP 200: no payment required ---
    if response.status_code == 200:
        return SupportResult(
            supported=False,
            x402=False,
            gateway_batching=False,
            resource=url,
            method=normalized_method,
            version="",
            payment_required=False,
        )

    # --- Not a 402: no x402 challenge ---
    if response.status_code != 402:
        return SupportResult(
            supported=False,
            x402=False,
            gateway_batching=False,
            resource=url,
            method=normalized_method,
            version="",
            reason=f"HTTP {response.status_code} (not 402 Payment-Required)",
            payment_required=False,
        )

    # --- Parse x402 v2 header (primary path) ---
    header_raw = response.headers.get(PAYMENT_REQUIRED_HEADER, "")
    version = "2"
    options: tuple[PaymentOption, ...] = ()
    unsupported_networks: tuple[str, ...] = ()

    if header_raw:
        version, options, unsupported_networks = _parse_v2_challenge(
            header_raw,
            configured_backend=configured_backend,
            wallet_network=wallet_network,
        )
    else:
        # --- Fallback: v1 body challenge ---
        version = "1"
        options, unsupported_networks = _parse_v1_challenge(
            response,
            configured_backend=configured_backend,
            wallet_network=wallet_network,
        )
        if not options:
            return SupportResult(
                supported=False,
                x402=True,
                gateway_batching=False,
                resource=url,
                method=normalized_method,
                version=version,
                reason="402 response has no parseable payment options",
            )

    # --- Determine gateway batching presence ---
    has_gateway_batching = any(o.payment_system == "gateway_batching" for o in options)

    # --- Select preferred option ---
    preferred = select_payment_option(
        options,
        configured_backend=configured_backend,
        wallet_network=wallet_network,
        network_preference=None,
        require_gateway=False,
        max_usdc=None,
    )

    supported_options = [o for o in options if o.supported_by_backend]
    supported = len(supported_options) > 0

    return SupportResult(
        supported=supported,
        x402=True,
        gateway_batching=has_gateway_batching,
        resource=url,
        method=normalized_method,
        version=version,
        options=tuple(options),
        unsupported_networks=unsupported_networks,
        preferred_option=preferred,
        payment_required=True,
    )
