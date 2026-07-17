"""Network option selection for x402 payment challenges.

Evaluates payment options from a 402 challenge against the local backend,
network registry, user preferences, and spending caps.  Returns the single
best option (or None if nothing matches).

NEVER signs, settles, deposits, or pays.
"""

from __future__ import annotations

import os

from hermes_x402.buyer.models import PaymentOption

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default deterministic order from the network registry (imported lazily
# to avoid circular imports at module level).
_DEFAULT_NETWORK_ORDER: list[str] | None = None


def _get_default_network_order() -> list[str]:
    """Get deterministic network order from the registry."""
    global _DEFAULT_NETWORK_ORDER
    if _DEFAULT_NETWORK_ORDER is not None:
        return _DEFAULT_NETWORK_ORDER
    try:
        from hermes_x402.networks import _NETWORKS

        _DEFAULT_NETWORK_ORDER = [n.key for n in _NETWORKS]
    except Exception:
        _DEFAULT_NETWORK_ORDER = []
    return _DEFAULT_NETWORK_ORDER


# ---------------------------------------------------------------------------
# Environment / config parsing
# ---------------------------------------------------------------------------


def parse_network_preference() -> tuple[str, ...]:
    """Read X402_NETWORK_PREFERENCE env var (comma-separated list).

    Returns an ordered tuple of network keys the user prefers.
    Returns empty tuple if unset or empty.
    """
    raw = os.environ.get("X402_NETWORK_PREFERENCE", "").strip()
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def parse_require_gateway() -> bool:
    """Read X402_REQUIRE_GATEWAY_BATCHING env var.

    Returns True if set to "true" (case-insensitive).
    """
    raw = os.environ.get("X402_REQUIRE_GATEWAY_BATCHING", "").strip().lower()
    return raw in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Option selection
# ---------------------------------------------------------------------------


def select_payment_option(
    options: tuple[PaymentOption, ...] | list[PaymentOption],
    *,
    configured_backend: str | None = None,
    wallet_network: str | None = None,
    network_preference: tuple[str, ...] | list[str] | None = None,
    require_gateway: bool = False,
    max_usdc: str | None = None,
) -> PaymentOption | None:
    """Select the best payment option from a challenge's accepted list.

    Selection criteria (applied in order):
      1. Discard options with invalid/unresolvable challenge data.
      2. Discard networks absent from the centralized registry.
      3. Discard networks unsupported by the active backend.
      4. Discard non-Gateway options when Gateway is required.
      5. Discard options exceeding the payment cap.
      6. Choose first matching user preference, then registry order.

    Args:
        options: Payment options from the 402 challenge.
        configured_backend: Active buyer backend ("cli" | "dcw" | None).
        wallet_network: The network the wallet is configured for.
        network_preference: User-ordered preferred network keys.
        require_gateway: If True, only gateway_batching options are eligible.
        max_usdc: Maximum USDC the caller is willing to spend.

    Returns:
        The best PaymentOption, or None if nothing matches.
    """
    if not options:
        return None

    # Resolve network preference: explicit > env var
    preference: tuple[str, ...]
    if network_preference is not None:
        preference = tuple(network_preference)
    else:
        preference = parse_network_preference()

    # Resolve max_usdc from env if not provided
    if max_usdc is None:
        env_cap = os.environ.get("X402_MAX_USDC_PER_PAYMENT", "").strip()
        if env_cap:
            max_usdc = env_cap

    # Resolve require_gateway from env if not explicitly set to True
    if not require_gateway:
        require_gateway = parse_require_gateway()

    # Phase 1: Validate and categorize options
    valid_options: list[PaymentOption] = []
    for opt in options:
        # Discard invalid challenge options (missing required fields)
        if not opt.network_id or not opt.amount_atomic:
            continue

        # Validate amount is a valid integer
        try:
            amount_int = int(opt.amount_atomic)
            if amount_int < 0:
                continue
        except (TypeError, ValueError):
            continue

        # Discard networks absent from the registry
        if not _is_network_in_registry(opt.network_id):
            continue

        # Discard networks unsupported by active backend
        if configured_backend and not _is_network_supported_by_backend(
            opt.network, configured_backend
        ):
            continue

        # Discard non-Gateway when Gateway required
        if require_gateway and opt.payment_system != "gateway_batching":
            continue

        # Discard options above payment cap
        if max_usdc is not None and not _is_under_cap(opt.amount_atomic, max_usdc):
            continue

        valid_options.append(opt)

    if not valid_options:
        return None

    # Phase 2: Sort by preference and deterministic order
    return _rank_and_select(valid_options, preference)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_network_in_registry(network_id: str) -> bool:
    """Check if a CAIP-2 network ID exists in the centralized registry."""
    try:
        from hermes_x402.networks import network_for_caip2

        return network_for_caip2(network_id) is not None
    except Exception:
        return False


def _is_network_supported_by_backend(network_key: str, backend: str) -> bool:
    """Check if a canonical network key is supported by the given backend."""
    try:
        from hermes_x402.networks import get_network

        net = get_network(network_key)
    except Exception:
        return False

    if backend == "cli":
        return net.buyer_cli_supported
    elif backend == "dcw":
        return net.buyer_dcw_supported
    return False


def _is_under_cap(amount_atomic: str, max_usdc: str) -> bool:
    """Check if atomic amount is at or below the USDC cap."""
    from decimal import Decimal, InvalidOperation

    try:
        atomic = Decimal(amount_atomic)
    except (InvalidOperation, ValueError):
        return False

    try:
        cap = Decimal(max_usdc)
    except (InvalidOperation, ValueError):
        return False

    if not atomic.is_finite() or not cap.is_finite():
        return False

    # Convert cap (USDC) to atomic (6 decimals)
    max_atomic = cap * Decimal(1_000_000)
    return atomic <= max_atomic


def _network_sort_key(network_key: str) -> int:
    """Return sort index for a network key based on registry order."""
    order = _get_default_network_order()
    try:
        return order.index(network_key)
    except ValueError:
        # Unknown network goes to the end
        return len(order)


def _rank_and_select(
    options: list[PaymentOption],
    preference: tuple[str, ...],
) -> PaymentOption | None:
    """Select the best option based on user preference and deterministic order.

    Preference order:
      1. Options matching user's preferred networks (in preference order).
      2. Remaining options in deterministic registry order.
    """
    if not options:
        return None

    # Score each option: lower is better
    def score(opt: PaymentOption) -> tuple[int, int, int]:
        # First: gateway_batching beats vanilla (if both present)
        system_penalty = 0 if opt.payment_system == "gateway_batching" else 1
        # Second: user preference order (0 if preferred, 1000 if not)
        if preference:
            try:
                pref_idx = preference.index(opt.network)
            except ValueError:
                pref_idx = len(preference)
        else:
            pref_idx = len(preference)  # no preference = equal weight
        # Third: deterministic registry order
        registry_idx = _network_sort_key(opt.network)
        return (pref_idx, system_penalty, registry_idx)

    sorted_options = sorted(options, key=score)
    return sorted_options[0]
