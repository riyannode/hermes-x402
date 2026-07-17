"""Ergonomic aiohttp gateway decorator for x402 seller mode.

Wraps the low-level X402SellerMiddleware with a decorator-based API so
sellers can protect routes with a single line::

    gateway = create_aiohttp_gateway("0x...")

    @gateway.require("$0.01")
    async def premium_data(request):
        return web.json_response({"secret": 42})

The gateway builds 402 responses from the centralized network registry and
delegates settlement to X402SellerMiddleware._settle() via Circle Gateway.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from aiohttp import web

from hermes_x402.context import set_payment_context
from hermes_x402.middleware import (
    CIRCLE_BATCHING_NAME,
    CIRCLE_BATCHING_SCHEME,
    CIRCLE_BATCHING_VERSION,
    DEFAULT_MAX_TIMEOUT_SECONDS,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X402_VERSION,
    PaymentResult,
)
from hermes_x402.networks import NetworkConfig, NetworkNotFoundError, get_network

logger = logging.getLogger("hermes_x402.seller_gateway")


async def _call_handler(handler: Callable, request: web.Request) -> web.Response:
    """Invoke an async aiohttp handler (needed for lambda/await bridging)."""
    return await handler(request)


# USDC has 6 decimal places
_USDC_DECIMALS = 6
_USDC_MULTIPLIER = 10**_USDC_DECIMALS

# Max USDC amount that fits safely (conservative: $42,949,672.95 = 2^32 - 1 in 6-dec)
_MAX_ATOMIC = 2**64  # well within uint256, generous safety margin

# Seller address: 0x + 40 hex characters (EIP-55 checksum not enforced)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------


def _parse_price(price: str | Decimal) -> str:
    """Parse a human USDC price like ``"$0.01"`` to an atomic 6-decimal string.

    Returns a string of the atomic amount (e.g. ``"10000"`` for $0.01).

    Raises ``ValueError`` on negative, NaN, Infinity, excess precision, or
    overflow.  Uses ``Decimal`` exclusively — never ``float``.
    """
    if callable(price):
        raise TypeError("price must be a string or Decimal, not a callable")

    # Normalise currency marker: strip $ from anywhere, not just prefix.
    # This handles "$0.01", "-$0.01", "$-0.01", and bare "0.01".
    raw = str(price).strip().replace("$", "")

    if not raw:
        raise ValueError("price must not be empty")

    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"Cannot parse price: {price!r}") from None

    # Reject special values
    if d.is_nan():
        raise ValueError("price must not be NaN")
    if d.is_infinite():
        raise ValueError("price must not be Infinity")
    if d < 0:
        raise ValueError(f"price must not be negative: {price!r}")

    # Reject zero (free resources shouldn't be gated)
    if d == 0:
        raise ValueError("price must be greater than zero")

    # Check excess precision (more than 6 decimal places)
    # Convert to tuple and check the exponent
    sign, digits, exponent = d.as_tuple()
    if isinstance(exponent, int) and exponent < -_USDC_DECIMALS:
        raise ValueError(
            f"price has excess precision (more than {_USDC_DECIMALS} decimals): {price!r}"
        )

    # Multiply to atomic 6-decimal
    atomic = d * _USDC_MULTIPLIER

    # Round to integer (handles edge cases like 0.001 with excess precision after scaling)
    try:
        atomic_int = int(atomic)
    except (OverflowError, ValueError):
        raise ValueError(f"price overflows atomic amount: {price!r}") from None

    # Validate range
    if atomic_int <= 0:
        raise ValueError(f"price too small (must be > $0.000001): {price!r}")
    if atomic_int > _MAX_ATOMIC:
        raise ValueError(f"price exceeds maximum: {price!r}")

    return str(atomic_int)


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


def _validate_address(address: str) -> None:
    """Validate a seller (payTo) address.  Must be 0x + 40 hex chars."""
    if not isinstance(address, str) or not _ADDRESS_RE.match(address):
        raise ValueError(
            f"Invalid seller address: {address!r}. Must be 0x followed by 40 hex characters."
        )


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class X402Gateway:
    """Ergonomic aiohttp gateway for x402 paid routes.

    Create via :func:`create_aiohttp_gateway`, then decorate handlers with
    :meth:`require`::

        gateway = create_aiohttp_gateway("0x...")

        @gateway.require("$0.01")
        async def my_handler(request):
            return web.json_response({"ok": True})

    The decorator intercepts requests, checks for x402 payment headers,
    returns 402 Payment-Required if absent, and settles via Circle Gateway
    before forwarding to the original handler.
    """

    def __init__(
        self,
        seller_address: str,
        networks: list[NetworkConfig],
        facilitator_url: str,
        default_description: str,
    ) -> None:
        _validate_address(seller_address)
        if not networks:
            raise ValueError("At least one network must be specified")

        self._seller_address = seller_address
        self._networks = networks  # list of NetworkConfig
        self._facilitator_url = facilitator_url
        self._default_description = default_description

        # Pre-index networks by key for fast lookup
        self._networks_by_key: dict[str, NetworkConfig] = {n.key: n for n in networks}

        # Build default accepts[] entries (one per network)
        self._default_accepts = self._build_accepts(networks, None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def require(
        self,
        price: str | Decimal | Callable[..., str | Decimal] | None = None,
        *,
        networks: list[str] | None = None,
        description: str | None = None,
    ) -> Callable:
        """Decorator to protect an aiohttp route with x402 payment.

        Usage::

            @gateway.require("$0.01")
            async def handler(request): ...

            @gateway.require(
                price="$0.01",
                networks=["base", "polygon"],
                description="Premium data",
            )
            async def handler(request): ...

            @gateway.require(
                price=lambda request: "$0.01",
            )
            async def handler(request): ...

        Args:
            price: Static price string (e.g. ``"$0.01"``), a ``Decimal``, or
                a callable ``(request) -> str | Decimal`` for dynamic pricing.
            networks: Override accepted networks for this route.  Must be
                valid network keys or aliases from the registry.
            description: Override the default description for this route.
        """
        # Validate and resolve per-route network overrides
        resolved_networks: list[NetworkConfig] | None = None
        if networks is not None:
            resolved_networks = []
            for net in networks:
                try:
                    nc = get_network(net)
                except (NetworkNotFoundError, Exception) as e:
                    raise ValueError(f"Unknown network in require(): {net!r}") from e
                resolved_networks.append(nc)

        route_desc = description or self._default_description

        # Pre-build accepts for this route (if networks differ from default)
        route_accepts: list[dict] | None = None
        if resolved_networks is not None:
            route_accepts = self._build_accepts(resolved_networks, None)

        def decorator(handler: Callable) -> Callable:
            return self._wrap_handler(
                handler,
                price=price,
                networks=resolved_networks,
                accepts=route_accepts,
                description=route_desc,
            )

        return decorator

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wrap_handler(
        self,
        handler: Callable,
        price: str | Decimal | Callable | None,
        networks: list[NetworkConfig] | None,
        accepts: list[dict] | None,
        description: str,
    ) -> Callable:
        """Wrap an aiohttp handler with x402 payment checking.

        Returns a new async handler that checks for x402 payment headers
        before forwarding to the original handler.  The original handler
        is accessible via ``__wrapped_handler__`` on the returned callable.
        """
        gateway = self

        async def x402_wrapped(request: web.Request) -> web.Response:
            return await gateway._handle_request(
                request,
                lambda req: _call_handler(handler, req),
                price,
                networks,
                accepts,
                description,
            )

        x402_wrapped.__wrapped_handler__ = handler  # type: ignore[attr-defined]
        return x402_wrapped

    async def _handle_request(
        self,
        request: web.Request,
        handler_call: Callable,
        price_spec: str | Decimal | Callable | None,
        networks: list[NetworkConfig] | None,
        accepts: list[dict] | None,
        description: str,
    ) -> web.Response:
        """Core request handling: check payment, settle, forward."""
        # Resolve price (static or dynamic)
        if callable(price_spec):
            resolved_price = price_spec(request)
        elif price_spec is not None:
            resolved_price = price_spec
        else:
            # No price specified — shouldn't happen if require() is used correctly
            raise ValueError("No price specified for require()")

        # Parse to atomic amount
        try:
            amount = _parse_price(resolved_price)
        except (ValueError, TypeError) as e:
            logger.error("Invalid price for route %s: %s", request.path, e)
            return web.json_response(
                {"error": "Server configuration error", "detail": str(e)},
                status=500,
            )

        # Determine which networks to use
        route_networks = networks if networks is not None else self._networks
        route_accepts = accepts if accepts is not None else self._default_accepts

        # Check for x402 payment header
        payment_header = request.headers.get(PAYMENT_SIGNATURE_HEADER)

        if not payment_header:
            # No payment → return 402
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # Payment header present → decode and settle
        try:
            raw = base64.b64decode(payment_header).decode()
            decoded = json.loads(raw)
        except Exception as e:
            logger.warning("Invalid payment header: %s", e)
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # Extract nested payload (circlekit/x402-header-agent format)
        inner_payload = decoded.get("payload", {})
        authorization = inner_payload.get("authorization", {})

        # Fallback: flat format (backward compat)
        if not authorization:
            authorization = decoded.get("authorization", {})

        # --- Validate authorization fields before any settlement ---
        if not authorization:
            logger.warning("Missing authorization in payment payload")
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        payer = authorization.get("from", "")
        client_value = str(authorization.get("value", "0"))
        auth_network = authorization.get("network", "")
        auth_asset = authorization.get("asset", "")
        auth_pay_to = authorization.get("payTo", "")

        # Determine which network the client claims to pay on.
        # The accepted-network field in the payload must be CAIP-2.
        accepted = decoded.get("accepted", {})
        payload_network = accepted.get("network", auth_network)

        # Validate network is one we accept (CAIP-2 matching)
        accepted_networks = {n.caip2 for n in route_networks}
        if payload_network not in accepted_networks:
            logger.warning(
                "Payment on unaccepted network %s (accepted: %s)",
                payload_network,
                accepted_networks,
            )
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # Resolve the NetworkConfig for the claimed network
        network_config: NetworkConfig | None = None
        for net in route_networks:
            if net.caip2 == payload_network:
                network_config = net
                break

        if network_config is None:
            logger.warning("No NetworkConfig found for network %s", payload_network)
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # --- Validate client value matches server-computed amount ---
        try:
            client_atomic = int(client_value)
            server_atomic = int(amount)
        except (ValueError, TypeError):
            logger.warning("Malformed authorization value: %s", client_value)
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        if client_atomic != server_atomic:
            logger.warning("Underpayment rejected: client=%s server=%s", client_value, amount)
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # Validate asset matches expected USDC contract
        if not auth_asset or auth_asset.lower() != network_config.usdc_address.lower():
            logger.warning(
                "Payment asset mismatch: got %r, expected %r",
                auth_asset,
                network_config.usdc_address,
            )
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # Validate payTo matches configured seller address
        if not auth_pay_to or auth_pay_to.lower() != self._seller_address.lower():
            logger.warning(
                "Payment payTo mismatch: got %r, expected %r",
                auth_pay_to,
                self._seller_address,
            )
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # --- Build settle requirements from SERVER-computed amount ---
        requirements = self._build_settle_requirements(amount, payload_network, route_networks)

        # Settle via Circle Gateway (skip verify)
        try:
            settle_result = await self._settle(decoded, requirements)
        except Exception as e:
            logger.error("Settle failed: %s", e)
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        if not settle_result.get("success"):
            reason = settle_result.get("errorReason", "unknown")
            logger.warning("Settlement rejected: %s", reason)
            body = self._build_402_body(
                amount, request.path, description, route_networks, route_accepts
            )
            encoded = base64.b64encode(json.dumps(body).encode()).decode()
            return web.json_response(
                body,
                status=402,
                headers={PAYMENT_REQUIRED_HEADER: encoded},
            )

        # Payment succeeded
        transaction = settle_result.get("transaction", "")
        result = PaymentResult(
            payer=payer,
            amount=client_value,
            network=payload_network,
            transaction=transaction,
        )

        # Store on request and set context for tools
        request["x402_payment"] = result
        set_payment_context(
            payer=payer,
            amount=client_value,
            network=payload_network,
            transaction=transaction,
        )

        logger.info("Payment settled: %s USDC by %s tx=%s", client_value, payer, transaction)

        # Forward to the original handler
        return await handler_call(request)

    def _build_accepts(
        self,
        networks: list[NetworkConfig],
        override_amount: str | None,
    ) -> list[dict]:
        """Build the accepts[] array for the 402 response."""
        accepts = []
        for net in networks:
            entry: dict[str, Any] = {
                "scheme": CIRCLE_BATCHING_SCHEME,
                "network": net.caip2,
                "asset": net.usdc_address,
                "amount": override_amount or "0",
                "payTo": self._seller_address,
                "maxTimeoutSeconds": DEFAULT_MAX_TIMEOUT_SECONDS,
                "extra": {
                    "name": CIRCLE_BATCHING_NAME,
                    "version": CIRCLE_BATCHING_VERSION,
                    "verifyingContract": net.gateway_wallet,
                },
            }
            accepts.append(entry)
        return accepts

    def _build_402_body(
        self,
        amount: str,
        path: str,
        description: str,
        networks: list[NetworkConfig],
        accepts: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Build the full 402 x402-v2 response body."""
        if accepts is None:
            accepts = self._build_accepts(networks, amount)
        else:
            # Patch amount into each accept entry
            for entry in accepts:
                entry["amount"] = amount

        return {
            "x402Version": X402_VERSION,
            "resource": {
                "url": path,
                "description": description,
                "mimeType": "application/json",
            },
            "accepts": accepts,
        }

    def _build_settle_requirements(
        self,
        amount: str,
        network: str,
        networks: list[NetworkConfig],
    ) -> dict[str, Any]:
        """Build payment requirements for Circle Gateway settle()."""
        # Find the NetworkConfig for this network
        nc = None
        for n in networks:
            if n.key == network:
                nc = n
                break
        if nc is None:
            raise ValueError(f"Network {network!r} not in accepted networks")

        return {
            "scheme": CIRCLE_BATCHING_SCHEME,
            "network": network,
            "asset": nc.usdc_address,
            "amount": amount,
            "payTo": self._seller_address,
            "maxTimeoutSeconds": DEFAULT_MAX_TIMEOUT_SECONDS,
            "extra": {
                "name": CIRCLE_BATCHING_NAME,
                "version": CIRCLE_BATCHING_VERSION,
                "verifyingContract": nc.gateway_wallet,
            },
        }

    async def _settle(self, payload: dict, requirements: dict) -> dict:
        """Call Circle Gateway settle() endpoint directly (skip verify).

        Delegates to the same endpoint as X402SellerMiddleware._settle().
        """
        settle_url = f"{self._facilitator_url}/v1/x402/settle"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                settle_url,
                json={
                    "paymentPayload": payload,
                    "paymentRequirements": requirements,
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def create_aiohttp_gateway(
    seller_address: str,
    networks: list[str] | None = None,
    facilitator_url: str | None = None,
    default_description: str = "Paid resource",
) -> X402Gateway:
    """Create an ergonomic x402 gateway for aiohttp routes.

    Args:
        seller_address: PayTo address.  Must be ``0x`` + 40 hex characters.
        networks: Network keys or aliases (e.g. ``["base", "polygon"]``).
            If ``None``, defaults to ``["base"]``.
        facilitator_url: Circle Gateway facilitator URL.  If ``None``,
            resolved from the first network's registry entry.
        default_description: Default description for the 402 response body.

    Returns:
        An :class:`X402Gateway` instance whose :meth:`require` decorator
        protects aiohttp route handlers.

    Example::

        gateway = create_aiohttp_gateway(
            seller_address="0x1234...abcd",
            networks=["base", "polygon"],
        )

        @gateway.require("$0.01")
        async def premium_data(request):
            return web.json_response({"data": "secret"})
    """
    _validate_address(seller_address)

    # Resolve networks from registry
    if networks is None:
        networks = ["base"]

    resolved: list[NetworkConfig] = []
    for net in networks:
        try:
            nc = get_network(net)
        except (NetworkNotFoundError, Exception) as e:
            raise ValueError(f"Unknown network: {net!r}") from e
        resolved.append(nc)

    # Resolve facilitator URL from first network if not provided
    if facilitator_url is None:
        facilitator_url = resolved[0].facilitator_url

    return X402Gateway(
        seller_address=seller_address,
        networks=resolved,
        facilitator_url=facilitator_url,
        default_description=default_description,
    )
