"""Tool registration functions for the x402 Hermes plugin.

Each function registers a group of related tools. All handlers return
JSON strings. No subprocess, network, or payment calls at registration time.

Registered tools:
  x402_status          — plugin status and configuration
  x402_wallet_status   — Circle wallet status (read-only)
  x402_wallet_balance  — wallet USDC balance (read-only)
  x402_networks        — list supported networks with capability matrix
  x402_service_search  — search Circle marketplace for x402 services
  x402_supports        — check if a URL supports x402 payments
  x402_service_inspect — inspect a service URL without paying
  x402_fetch           — fetch a URL without paying
  x402_pay             — pay for an x402 resource (may transfer USDC)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

from hermes_x402.buyer.errors import (
    BuyerError,
    PaymentSubmissionUnknownError,
)
from hermes_x402.circle_cli.errors import CircleCliError
from hermes_x402.hermes_plugin.errors import format_error_result, format_success_result
from hermes_x402.hermes_plugin.output import safe_wallet_address
from hermes_x402.hermes_plugin.runtime import get_runtime
from hermes_x402.hermes_plugin.schemas import (
    ALLOWED_HTTP_METHODS,
    MAX_BODY_SIZE,
    MAX_HEADER_COUNT,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_SIZE,
    MAX_QUERY_LENGTH,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_RESULTS,
    MAX_URL_LENGTH,
    X402_FETCH_SCHEMA,
    X402_NETWORKS_SCHEMA,
    X402_PAY_SCHEMA,
    X402_SERVICE_INSPECT_SCHEMA,
    X402_SERVICE_SEARCH_SCHEMA,
    X402_STATUS_SCHEMA,
    X402_SUPPORTS_SCHEMA,
    X402_WALLET_BALANCE_SCHEMA,
    X402_WALLET_STATUS_SCHEMA,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _parse_usdc_cap(value: str, *, field: str) -> tuple[Decimal | None, str | None]:
    """Parse a USDC cap string into a Decimal. Returns (parsed, error)."""
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None, f"{field} is not a valid decimal amount."

    if not parsed.is_finite():
        return None, f"{field} must be finite."

    if parsed < 0:
        return None, f"{field} must be non-negative."

    return parsed, None


def _validate_max_usdc(
    caller_value: str | None,
    configured_value: str | None,
) -> tuple[str | None, str | None]:
    """Validate caller and configured payment caps.

    Both caps are parsed explicitly. An invalid configured cap always
    returns an error. Caller cap may reduce but never raise the
    configured cap. Absent caller cap uses the configured cap.
    """
    configured: Decimal | None = None

    if configured_value is not None:
        configured, error = _parse_usdc_cap(
            configured_value,
            field="Configured maximum payment",
        )
        if error:
            return None, error

    if caller_value is None:
        return configured_value, None

    caller, error = _parse_usdc_cap(caller_value, field="max_usdc")
    if error:
        return None, error

    if configured is not None and caller > configured:
        return None, ("Caller cap exceeds configured maximum. The configured cap cannot be raised.")

    return caller_value, None


def _validate_url(url: str) -> str | None:
    """Validate URL scheme and length. Returns error message or None."""
    if not url or not isinstance(url, str):
        return "URL is required."
    if len(url) > MAX_URL_LENGTH:
        return f"URL exceeds maximum length of {MAX_URL_LENGTH}."
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        return "URL must use https or http scheme."
    if not parsed.hostname:
        return "URL must have a valid hostname."
    return None


def _validate_method(method: str) -> str | None:
    """Validate HTTP method. Returns error message or None."""
    if method and method.upper() not in ALLOWED_HTTP_METHODS:
        return f"HTTP method must be one of: {', '.join(sorted(ALLOWED_HTTP_METHODS))}"
    return None


def _validate_query(query: str) -> str | None:
    """Validate search query. Returns error message or None."""
    if not query or not isinstance(query, str):
        return "Query is required."
    if len(query) > MAX_QUERY_LENGTH:
        return f"Query exceeds maximum length of {MAX_QUERY_LENGTH}."
    return None


def _validate_body_size(body: Any) -> str | None:
    """Validate body size. Returns error message or None."""
    if body is None:
        return None
    raw = json.dumps(body, ensure_ascii=False, default=str)
    if len(raw) > MAX_BODY_SIZE:
        return f"Body exceeds maximum size of {MAX_BODY_SIZE} bytes."
    return None


# ---------------------------------------------------------------------------
# Centralized URL and host policy
# ---------------------------------------------------------------------------

# Well-known hosts that should never be fetched (SSRF protection).
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "metadata.google.internal",
        "169.254.169.254",
    }
)


def _validate_allowed_url(
    url: str,
    host_allowlist: Sequence[str],
    mode: str = "strict_allowlist",
    allow_http: bool = False,
) -> str | None:
    """Validate URL scheme, length, hostname, and host allowlist.

    Enforces the authoritative runtime NetworkPolicy mode.
    Returns error message or None. Centralizes all URL/host policy
    checks for inspect, fetch, supports, and pay tools.
    """
    err = _validate_url(url)
    if err:
        return err

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    # HTTP requires explicit allow_http
    if parsed.scheme == "http" and not allow_http:
        return "HTTP URLs are not allowed. Use HTTPS or enable allow_http."

    # Block well-known SSRF targets
    if hostname in _BLOCKED_HOSTS:
        return f"Host is blocked: {hostname}"

    # Block private/reserved IPs (best-effort; DNS-resolved IPs bypass this)
    if hostname:
        import ipaddress

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return f"Host resolves to a private/reserved address: {hostname}"
            if ip.is_multicast or ip.is_unspecified:
                return f"Host is multicast or unspecified: {hostname}"
        except ValueError:
            pass  # hostname is a domain, not an IP — proceed

    # Reject userinfo (credentials in URL)
    if parsed.username or parsed.password:
        return "URL must not contain credentials (userinfo)."

    # Enforce network policy mode
    if mode == "strict_allowlist":
        if host_allowlist:
            allowed = any(
                hostname == item.lower() or hostname.endswith(f".{item.lower()}")
                for item in host_allowlist
            )
            if not allowed:
                return f"Host not in allowlist: {hostname}"
        else:
            # Empty allowlist in strict mode = nothing allowed
            return "No hosts are allowed (empty allowlist in strict_allowlist mode)."
    elif mode == "public" and host_allowlist:
        # In public mode, private/reserved IPs are already blocked above.
        # An allowlist may further restrict destinations.
        allowed = any(
            hostname == item.lower() or hostname.endswith(f".{item.lower()}")
            for item in host_allowlist
        )
        if not allowed:
            return f"Host not in allowlist: {hostname}"

    return None


def _check_redirect(
    response: httpx.Response,
) -> dict[str, Any] | None:
    """Check if response is a redirect and return bounded result."""
    if response.is_redirect:
        location = response.headers.get("location", "")
        # Bound location header
        if len(location) > MAX_URL_LENGTH:
            location = location[:MAX_URL_LENGTH]
        return {
            "success": False,
            "error": "redirect_not_followed",
            "status": response.status_code,
            "location": location,
            "message": "Redirects are not followed automatically.",
        }
    return None


def _bounded_response(
    response: httpx.Response,
) -> dict[str, Any]:
    """Read response body in a bounded manner and return structured result."""
    raw = response.content
    original_size = len(raw)
    truncated = original_size > MAX_OUTPUT_BYTES
    bounded = raw[:MAX_OUTPUT_BYTES]

    content_type = response.headers.get("content-type", "")
    is_json = "application/json" in content_type

    # Attempt JSON parse only on small enough, non-truncated bodies
    if is_json and not truncated:
        try:
            decoded = bounded.decode(response.encoding or "utf-8", errors="replace")
            parsed = json.loads(decoded)
            return {
                "success": True,
                "status": response.status_code,
                "content_type": content_type,
                "data": parsed,
                "truncated": False,
                "original_size": original_size,
            }
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    # Text/binary fallback
    try:
        decoded = bounded.decode(response.encoding or "utf-8", errors="replace")
    except Exception:
        decoded = bounded.decode("latin-1", errors="replace")

    if is_json and truncated:
        return {
            "success": True,
            "status": response.status_code,
            "content_type": content_type,
            "data": decoded,
            "truncated": True,
            "original_size": original_size,
            "error": "invalid_json_response",
            "message": "Response was too large to parse as JSON.",
        }

    return {
        "success": True,
        "status": response.status_code,
        "content_type": content_type,
        "data": decoded,
        "truncated": truncated,
        "original_size": original_size,
    }


def _bound_header(value: str, limit: int = MAX_URL_LENGTH) -> str:
    """Bound a header value before inserting into model context."""
    if len(value) > limit:
        return value[:limit]
    return value


# ---------------------------------------------------------------------------
# Status tools
# ---------------------------------------------------------------------------


def register_status_tools(ctx: Any) -> None:
    """Register x402_status tool."""

    def handler(**kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()

        wallet = runtime.wallet_address
        safe_wallet = safe_wallet_address(wallet) if wallet else ""

        host_allowlist: list[str] = []
        if runtime.config:
            host_allowlist = runtime.config.host_allowlist

        role = runtime.role
        is_configured = role is not None and runtime.is_configured

        result: dict[str, Any] = {
            "success": True,
            "plugin": "hermes-x402",
            "version": runtime.version,
            "role": role or "unconfigured",
            "backend": runtime.backend_name or "none",
            "network": runtime.network or "none",
            "wallet_address": safe_wallet,
            "plugin_loaded": True,
            "configured": is_configured,
            "available": runtime.is_available,
            "max_usdc_per_payment": (
                runtime.config.max_usdc_per_payment if runtime.config else None
            ),
            "host_allowlist": host_allowlist,
        }

        if runtime.init_error:
            result["init_error"] = runtime.init_error
            result["remediation"] = (
                "Check environment variables: X402_ROLE, X402_BUYER_BACKEND, "
                "and backend-specific credentials."
            )

        return format_success_result(result)

    ctx.register_tool(
        name="x402_status",
        toolset="x402",
        schema=X402_STATUS_SCHEMA,
        handler=lambda args, **kw: handler(**kw),
        description="Report x402 plugin status and configuration.",
    )


# ---------------------------------------------------------------------------
# Wallet tools
# ---------------------------------------------------------------------------


def register_wallet_tools(ctx: Any) -> None:
    """Register x402_wallet_status and x402_wallet_balance tools."""

    def wallet_status_handler(**kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()

        if not runtime.is_configured:
            return format_success_result(
                {
                    "success": True,
                    "configured": False,
                    "message": "x402 is not configured.",
                }
            )

        result: dict[str, Any] = {
            "success": True,
            "backend": runtime.backend_name,
            "wallet_address": safe_wallet_address(runtime.wallet_address),
            "network": runtime.network,
        }

        if runtime.backend_name == "cli":
            result["cli_executable"] = (
                runtime.config.circle_cli_executable if runtime.config else "circle"
            )
            result["cli_available"] = runtime.cli_client is not None
        elif runtime.backend_name == "dcw":
            result["dcw_wallet_id"] = runtime.config.wallet_id if runtime.config else ""

        return format_success_result(result)

    ctx.register_tool(
        name="x402_wallet_status",
        toolset="x402",
        schema=X402_WALLET_STATUS_SCHEMA,
        handler=lambda args, **kw: wallet_status_handler(**kw),
        description=("Report Circle wallet status. Read-only. Never exposes secrets."),
    )

    async def wallet_balance_handler(**kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()

        if not runtime.is_configured:
            return format_success_result(
                {
                    "success": True,
                    "configured": False,
                    "message": "x402 is not configured.",
                }
            )

        if runtime.backend_name == "dcw":
            return format_success_result(
                {
                    "success": True,
                    "supported": False,
                    "message": "DCW balance query is not supported.",
                }
            )

        if runtime.backend_name == "cli":
            if not runtime.cli_client:
                return format_success_result(
                    {
                        "success": False,
                        "error": "cli_not_available",
                        "message": "Circle CLI client is not available.",
                    }
                )
            try:
                balances = await runtime.cli_client.get_balance(
                    wallet_address=runtime.wallet_address,
                    network=runtime.network,
                )
                usdc_balance = "0"
                for b in balances:
                    if b.symbol == "USDC":
                        usdc_balance = b.amount
                        break
                return format_success_result(
                    {
                        "success": True,
                        "wallet": safe_wallet_address(runtime.wallet_address),
                        "network": runtime.network,
                        "balance": usdc_balance,
                        "balances": [{"symbol": b.symbol, "amount": b.amount} for b in balances],
                    }
                )
            except CircleCliError as exc:
                return format_error_result(exc)
            except Exception as exc:
                return format_error_result(exc)

        return format_success_result(
            {
                "success": False,
                "error": "unsupported_backend",
                "message": "Balance query is not available for this backend.",
            }
        )

    ctx.register_tool(
        name="x402_wallet_balance",
        toolset="x402",
        schema=X402_WALLET_BALANCE_SCHEMA,
        handler=wallet_balance_handler,
        is_async=True,
        description="Report configured wallet USDC balance. Read-only.",
    )


# ---------------------------------------------------------------------------
# Network tools
# ---------------------------------------------------------------------------


def register_network_tools(ctx: Any) -> None:
    """Register x402_networks tool."""

    def networks_handler(**kwargs: Any) -> str:
        from hermes_x402.networks import list_networks

        runtime = get_runtime()
        runtime.ensure_initialized()

        backend_name = runtime.backend_name
        role = runtime.role

        all_networks = list_networks()

        result_networks = []
        for net in all_networks:
            buyer_cli = net.buyer_cli_supported
            buyer_dcw = net.buyer_dcw_supported
            seller = net.seller_supported

            # Calculate active_role_supported based on role + backend
            active_role_supported = False
            if role == "buyer":
                if backend_name == "cli" and buyer_cli or backend_name == "dcw" and buyer_dcw:
                    active_role_supported = True
            elif role == "seller":
                active_role_supported = seller
            elif role == "dual":
                buyer_ok = (backend_name == "cli" and buyer_cli) or (
                    backend_name == "dcw" and buyer_dcw
                )
                active_role_supported = buyer_ok or seller

            result_networks.append(
                {
                    "key": net.key,
                    "display_name": net.display_name,
                    "caip2": net.caip2,
                    "chain_id": net.chain_id,
                    "environment": net.environment,
                    "gateway_supported": net.gateway_supported,
                    "buyer_backend_supported": (
                        (backend_name == "cli" and buyer_cli)
                        or (backend_name == "dcw" and buyer_dcw)
                    ),
                    "buyer_cli_supported": buyer_cli,
                    "buyer_dcw_supported": buyer_dcw,
                    "seller_supported": seller,
                    "active_role_supported": active_role_supported,
                }
            )

        return format_success_result(
            {
                "success": True,
                "backend": backend_name or "none",
                "role": role or "unconfigured",
                "count": len(result_networks),
                "networks": result_networks,
            }
        )

    ctx.register_tool(
        name="x402_networks",
        toolset="x402",
        schema=X402_NETWORKS_SCHEMA,
        handler=lambda args, **kw: networks_handler(**kw),
        description=(
            "List x402 networks supported by the active backend. "
            "Read-only. Returns capability matrix for each network."
        ),
    )


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


def register_discovery_tools(ctx: Any) -> None:
    """Register x402_service_search tool."""

    async def service_search_handler(args: dict, **kwargs: Any) -> str:
        query = args.get("query", "")
        limit = args.get("limit", 10)

        err = _validate_query(query)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        # Bound limit
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        if limit < 1:
            limit = 1
        if limit > MAX_SEARCH_LIMIT:
            limit = MAX_SEARCH_LIMIT

        runtime = get_runtime()
        runtime.ensure_initialized()

        if runtime.cli_client is None:
            return format_success_result(
                {
                    "success": False,
                    "error": "cli_not_available",
                    "message": (
                        "Service search requires the Circle CLI backend. "
                        "Set X402_BUYER_BACKEND=cli and configure Circle CLI credentials."
                    ),
                }
            )

        try:
            from hermes_x402.discovery.circle_marketplace import CircleCliMarketplaceProvider

            provider = CircleCliMarketplaceProvider(runner=runtime.cli_client.runner)
            services = await provider.search(query, limit=limit)

            results = []
            for svc in services[:MAX_SEARCH_RESULTS]:
                results.append(
                    {
                        "name": svc.name,
                        "description": svc.description or "",
                        "url": svc.url,
                        "advertised_price_usdc": svc.advertised_price_usdc or "",
                        "advertised_networks": list(svc.advertised_networks),
                    }
                )

            return format_success_result(
                {
                    "success": True,
                    "provider": "circle_marketplace",
                    "query": query,
                    "count": len(results),
                    "services": results,
                }
            )
        except Exception as exc:
            return format_error_result(exc)

    ctx.register_tool(
        name="x402_service_search",
        toolset="x402",
        schema=X402_SERVICE_SEARCH_SCHEMA,
        handler=service_search_handler,
        is_async=True,
        description=(
            "Search the Circle service marketplace for x402-enabled services. "
            "Returns bounded results without payment. Read-only."
        ),
    )


# ---------------------------------------------------------------------------
# Supports tools
# ---------------------------------------------------------------------------


def register_supports_tools(ctx: Any) -> None:
    """Register x402_supports tool."""

    async def supports_handler(args: dict, **kwargs: Any) -> str:
        url = args.get("url", "")
        method = (args.get("method") or "GET").upper()

        err = _validate_url(url)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        err = _validate_method(method)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        runtime = get_runtime()
        runtime.ensure_initialized()

        # --- Enforce runtime network policy ---
        policy_mode = runtime.config.network_policy if runtime.config else "strict_allowlist"
        policy_allowlist = runtime.config.host_allowlist if runtime.config else []
        policy_allow_http = runtime.config.allow_http if runtime.config else False

        err = _validate_allowed_url(
            url, policy_allowlist, mode=policy_mode, allow_http=policy_allow_http
        )
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": (
                        "invalid_input"
                        if "required" in err or "exceeds" in err or "must use" in err
                        else "host_rejected"
                        if "allowlist" in err or "private" in err or "blocked" in err
                        else "policy_rejected"
                    ),
                    "message": err,
                }
            )

        # --- DNS destination validation (fail closed) ---
        try:
            from hermes_x402.dns_validator import resolve_and_validate_destination

            await resolve_and_validate_destination(url)
        except ValueError as exc:
            return format_success_result(
                {"success": False, "error": "destination_rejected", "message": str(exc)}
            )

        try:
            from hermes_x402.buyer.supports import check_supports

            result = await check_supports(url, method=method, config=runtime.config)
            return format_success_result(
                {
                    "success": True,
                    "supported": result.supported,
                    "x402": result.x402,
                    "gateway_batching": result.gateway_batching,
                    "resource": result.resource,
                    "method": result.method,
                    "x402_version": result.version,
                    "options": [
                        {
                            "scheme": opt.scheme,
                            "payment_system": opt.payment_system,
                            "network": opt.network,
                            "network_id": opt.network_id,
                            "amount_atomic": opt.amount_atomic,
                            "amount_usdc": opt.amount_usdc,
                            "asset": opt.asset,
                            "supported_by_backend": opt.supported_by_backend,
                        }
                        for opt in result.options
                    ],
                    "unsupported_networks": list(result.unsupported_networks),
                    "preferred_option": (
                        {
                            "network": result.preferred_option.network,
                            "amount_usdc": result.preferred_option.amount_usdc,
                        }
                        if result.preferred_option
                        else None
                    ),
                    "reason": result.reason,
                }
            )
        except Exception as exc:
            return format_error_result(exc)

    ctx.register_tool(
        name="x402_supports",
        toolset="x402",
        schema=X402_SUPPORTS_SCHEMA,
        handler=supports_handler,
        is_async=True,
        description=(
            "Check whether a URL supports x402 payments. Read-only preflight. "
            "Never signs, settles, deposits, or pays."
        ),
    )


# ---------------------------------------------------------------------------
# Service tools
# ---------------------------------------------------------------------------


def register_service_tools(ctx: Any) -> None:
    """Register x402_service_inspect tool."""

    async def service_inspect_handler(args: dict, **kwargs: Any) -> str:
        url = args.get("url", "")

        runtime = get_runtime()
        runtime.ensure_initialized()

        # --- Enforce runtime network policy ---
        policy_mode = runtime.config.network_policy if runtime.config else "strict_allowlist"
        policy_allowlist = runtime.config.host_allowlist if runtime.config else []
        policy_allow_http = runtime.config.allow_http if runtime.config else False

        err = _validate_allowed_url(
            url, policy_allowlist, mode=policy_mode, allow_http=policy_allow_http
        )
        if err:
            error_code = (
                "invalid_input"
                if "required" in err or "exceeds" in err or "must use" in err
                else "host_rejected"
                if "allowlist" in err or "private" in err or "blocked" in err
                else "policy_rejected"
            )
            return format_success_result(
                {
                    "success": False,
                    "error": error_code,
                    "message": err,
                }
            )

        # --- DNS destination validation (fail closed) ---
        try:
            from hermes_x402.dns_validator import resolve_and_validate_destination

            await resolve_and_validate_destination(url)
        except ValueError as exc:
            return format_success_result(
                {"success": False, "error": "destination_rejected", "message": str(exc)}
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.head(url, follow_redirects=False)

                # Check redirect
                redirect = _check_redirect(response)
                if redirect:
                    return format_success_result(redirect)

                result_data: dict[str, Any] = {
                    "success": True,
                    "url": url,
                    "status": response.status_code,
                    "content_type": _bound_header(response.headers.get("content-type", "")),
                    "headers": dict(list(response.headers.items())[:MAX_HEADER_COUNT]),
                }
                if response.status_code == 402:
                    result_data["payment_required"] = True
                    result_data["payment_header"] = _bound_header(
                        response.headers.get("Payment-Required", "")
                    )
                return format_success_result(result_data)
        except httpx.HTTPError as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "resource_failure",
                    "message": f"Failed to inspect service: {exc}",
                }
            )

    ctx.register_tool(
        name="x402_service_inspect",
        toolset="x402",
        schema=X402_SERVICE_INSPECT_SCHEMA,
        handler=service_inspect_handler,
        is_async=True,
        description="Inspect an x402 service URL without paying.",
    )


# ---------------------------------------------------------------------------
# Payment tools
# ---------------------------------------------------------------------------


def register_payment_tools(ctx: Any) -> None:
    """Register x402_fetch and x402_pay tools."""

    async def fetch_handler(args: dict, **kwargs: Any) -> str:
        url = args.get("url", "")
        method = (args.get("method") or "GET").upper()
        body = args.get("body")

        err = _validate_method(method)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        err = _validate_body_size(body)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        runtime = get_runtime()
        runtime.ensure_initialized()

        # --- Enforce runtime network policy ---
        policy_mode = runtime.config.network_policy if runtime.config else "strict_allowlist"
        policy_allowlist = runtime.config.host_allowlist if runtime.config else []
        policy_allow_http = runtime.config.allow_http if runtime.config else False

        err = _validate_allowed_url(
            url, policy_allowlist, mode=policy_mode, allow_http=policy_allow_http
        )
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": (
                        "invalid_input"
                        if "required" in err or "exceeds" in err or "must use" in err
                        else "host_rejected"
                        if "allowlist" in err or "private" in err or "blocked" in err
                        else "policy_rejected"
                    ),
                    "message": err,
                }
            )

        # --- DNS destination validation (fail closed) ---
        try:
            from hermes_x402.dns_validator import resolve_and_validate_destination

            await resolve_and_validate_destination(url)
        except ValueError as exc:
            return format_success_result(
                {"success": False, "error": "destination_rejected", "message": str(exc)}
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                kwargs_req: dict[str, Any] = {
                    "method": method,
                    "url": url,
                    "follow_redirects": False,
                }
                if body is not None and method in {"POST", "PUT", "PATCH"}:
                    kwargs_req["json"] = body

                # --- Streaming bounded read (Finding 10) ---
                async with client.stream(**kwargs_req) as response:
                    # Check redirect
                    redirect = _check_redirect(response)
                    if redirect:
                        return format_success_result(redirect)

                    if response.status_code == 402:
                        # Bound 402 challenge body
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total <= MAX_OUTPUT_BYTES + 1:
                                chunks.append(chunk)
                            else:
                                break
                        return format_success_result(
                            {
                                "success": True,
                                "status": 402,
                                "payment_required": True,
                                "message": (
                                    "This resource requires payment. "
                                    "Use x402_pay to purchase access."
                                ),
                                "payment_header": _bound_header(
                                    response.headers.get("Payment-Required", "")
                                ),
                            }
                        )

                    # Stream body with bounded read
                    chunks = []
                    total = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total <= MAX_OUTPUT_BYTES + 1:
                            chunks.append(chunk)
                        else:
                            truncated = True
                            break

                    raw = b"".join(chunks[:MAX_OUTPUT_BYTES]) if chunks else b""
                    original_size = total if truncated else len(raw)

                    content_type = response.headers.get("content-type", "")
                    is_json = "application/json" in content_type

                    # Attempt JSON parse only on small enough, non-truncated bodies
                    if is_json and not truncated:
                        try:
                            decoded = raw.decode(response.encoding or "utf-8", errors="replace")
                            parsed = json.loads(decoded)
                            return format_success_result(
                                {
                                    "success": True,
                                    "status": response.status_code,
                                    "content_type": content_type,
                                    "data": parsed,
                                    "truncated": False,
                                    "original_size": original_size,
                                }
                            )
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass

                    # Text/binary fallback
                    try:
                        decoded = raw.decode(response.encoding or "utf-8", errors="replace")
                    except Exception:
                        decoded = raw.decode("latin-1", errors="replace")

                    return format_success_result(
                        {
                            "success": True,
                            "status": response.status_code,
                            "content_type": content_type,
                            "data": decoded,
                            "truncated": truncated,
                            "original_size": original_size,
                        }
                    )
        except httpx.HTTPError as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "resource_failure",
                    "message": f"Request failed: {exc}",
                }
            )

    ctx.register_tool(
        name="x402_fetch",
        toolset="x402",
        schema=X402_FETCH_SCHEMA,
        handler=fetch_handler,
        is_async=True,
        description=(
            "Fetch a resource URL without paying. When HTTP 402 occurs, "
            "reports that payment is required but does not pay."
        ),
    )

    async def pay_handler(args: dict, **kwargs: Any) -> str:
        url = args.get("url", "")
        method = (args.get("method") or "GET").upper()
        body = args.get("body")
        max_usdc = args.get("max_usdc")

        err = _validate_url(url)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        err = _validate_method(method)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        err = _validate_body_size(body)
        if err:
            return format_success_result(
                {"success": False, "error": "invalid_input", "message": err}
            )

        runtime = get_runtime()
        runtime.ensure_initialized()

        if not runtime.is_available:
            return format_success_result(
                {
                    "success": False,
                    "error": "configuration_error",
                    "message": "x402 buyer is not available. Check configuration.",
                }
            )

        # --- Enforce authoritative runtime network policy BEFORE payment ---
        policy_mode = runtime.config.network_policy if runtime.config else "strict_allowlist"
        policy_allowlist = runtime.config.host_allowlist if runtime.config else []
        policy_allow_http = runtime.config.allow_http if runtime.config else False

        err = _validate_allowed_url(
            url, policy_allowlist, mode=policy_mode, allow_http=policy_allow_http
        )
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": (
                        "invalid_input"
                        if "required" in err or "exceeds" in err or "must use" in err
                        else "host_rejected"
                        if "allowlist" in err or "private" in err or "blocked" in err
                        else "policy_rejected"
                    ),
                    "message": err,
                }
            )

        configured_max = runtime.config.max_usdc_per_payment if runtime.config else None
        validated_cap, err = _validate_max_usdc(max_usdc, configured_max)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "payment_limit_exceeded",
                    "message": err,
                }
            )

        # New-host approval check — fail closed on any error
        if runtime.config and runtime.config.require_approval_for_new_host:
            try:
                from hermes_x402.buyer.approval import check_approval_required

                approval = check_approval_required(url, config=runtime.config)
                if approval is not None:
                    return format_success_result(approval)
            except Exception as exc:
                return format_success_result(
                    {
                        "success": False,
                        "error": "approval_check_failed",
                        "retry_safe": False,
                        "message": f"Approval check failed: {exc}",
                    }
                )

        # --- DNS destination validation (fail closed) ---
        try:
            from hermes_x402.dns_validator import resolve_and_validate_destination

            await resolve_and_validate_destination(url)
        except ValueError as exc:
            return format_success_result(
                {"success": False, "error": "destination_rejected", "message": str(exc)}
            )

        buyer = runtime.buyer_tool
        if buyer is None:
            return format_success_result(
                {
                    "success": False,
                    "error": "configuration_error",
                    "message": "Buyer tool is not initialized.",
                }
            )

        try:
            result = await buyer.pay(url=url, method=method, body=body, max_usdc=validated_cap)

            output: dict[str, Any] = {
                "success": True,
                "payment_status": result.payment_status,
                "status": result.status,
                "payer": (safe_wallet_address(result.payer) if result.payer else ""),
                "amount": result.amount,
                "network": result.network,
            }
            if result.transaction_id:
                output["transaction_id"] = result.transaction_id
            if result.data is not None:
                data = result.data
                if isinstance(data, str) and len(data) > MAX_OUTPUT_SIZE:
                    data = data[:MAX_OUTPUT_SIZE] + "\n[... truncated ...]"
                output["data"] = data

            return format_success_result(output)

        except PaymentSubmissionUnknownError:
            return format_success_result(
                {
                    "success": False,
                    "error": "payment_outcome_unknown",
                    "message": ("Payment may have been submitted. Do not retry automatically."),
                    "retry_safe": False,
                }
            )
        except BuyerError as exc:
            return format_error_result(exc)
        except Exception as exc:
            return format_error_result(exc)

    ctx.register_tool(
        name="x402_pay",
        toolset="x402",
        schema=X402_PAY_SCHEMA,
        handler=pay_handler,
        is_async=True,
        description=(
            "⚠️ This tool may transfer USDC. Pay for an x402 resource. "
            "Cannot change configured wallet, network, or backend. "
            "Capped by local configuration."
        ),
    )
