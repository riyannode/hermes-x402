"""Tool registration functions for the x402 Hermes plugin.

Each function registers a group of related tools. All handlers return
JSON strings. No subprocess, network, or payment calls at registration time.

Registered tools (14 total):
  x402_status                    — plugin status and configuration
  x402_wallet_status             — extended Circle wallet + readiness status (read-only)
  x402_wallet_balance            — wallet USDC balance (read-only)
  x402_networks                  — list supported networks with capability matrix
  x402_service_search            — search Circle marketplace for x402 services
  x402_supports                  — check if a URL supports x402 payments
  x402_service_inspect           — inspect a service URL without paying
  x402_fetch                     — fetch a URL without paying
  x402_pay                       — pay for an x402 resource (may transfer USDC)
  x402_login_start               — start Circle email OTP login
  x402_login_complete            — complete Circle login with OTP
  x402_gateway_balance           — Circle Gateway USDC balance (read-only)
  x402_gateway_deposit_preview   — preview Gateway deposit without moving funds
  x402_gateway_deposit_execute   — execute Gateway deposit from preview
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

from hermes_x402.buyer.errors import (
    BuyerError,
    PaymentSubmissionUnknownError,
)
from hermes_x402.circle_cli.errors import (
    CircleCliError,
    CircleCliPaymentOutcomeUnknownError,
)
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
    X402_GATEWAY_BALANCE_SCHEMA,
    X402_GATEWAY_DEPOSIT_EXECUTE_SCHEMA,
    X402_GATEWAY_DEPOSIT_PREVIEW_SCHEMA,
    X402_LOGIN_COMPLETE_SCHEMA,
    X402_LOGIN_START_SCHEMA,
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

    async def wallet_status_handler(**kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()

        if not runtime.is_configured or runtime.role is None:
            return format_success_result(
                {
                    "success": True,
                    "configured": False,
                    "message": "x402 is not configured.",
                }
            )

        result: dict[str, Any] = {
            "success": True,
            "configured": True,
            "backend": runtime.backend_name,
            "wallet_address": safe_wallet_address(runtime.wallet_address),
            "network": runtime.network,
        }

        blockers: list[dict[str, str]] = []
        next_tool = "x402_service_search"

        if runtime.backend_name == "cli" and runtime.cli_client:
            # Circle CLI availability + version
            try:
                ver = await runtime.cli_client.version()
                result["cli_executable"] = (
                    runtime.config.circle_cli_executable if runtime.config else "circle"
                )
                result["cli_available"] = True
                result["cli_version"] = ver.value
            except Exception:
                result["cli_executable"] = (
                    runtime.config.circle_cli_executable if runtime.config else "circle"
                )
                result["cli_available"] = True
                result["cli_version"] = "version_check_failed"

            # Session status via v0.0.6 wallet status
            session_valid = False
            session_environment = "unknown"
            terms_accepted = False
            session_check_error = None
            try:
                status = await runtime.cli_client.agent_wallet_status()
                session_valid = status.authenticated
                session_environment = "testnet" if status.testnet_status == "VALID" else "mainnet"
                terms_accepted = status.terms_accepted
                if status.email and "@" in status.email:
                    local, domain = status.email.split("@", 1)
                    result["email_masked"] = f"{local[0]}{'*' * max(len(local) - 1, 0)}@{domain}"
            except CircleCliError as exc:
                # Distinguish check-failed from false/not-present
                session_check_error = str(exc)
                if "terms" in str(exc).lower():
                    terms_accepted = False
            except Exception as exc:
                session_check_error = str(exc)

            result["session_valid"] = session_valid
            result["session_environment"] = session_environment
            result["terms_accepted"] = terms_accepted
            if session_check_error:
                result["session_check_error"] = session_check_error

            if not session_valid:
                blockers.append({"code": "session_invalid", "next_tool": "x402_login_start"})
                next_tool = "x402_login_start"
            elif not terms_accepted:
                blockers.append(
                    {
                        "code": "terms_action_required",
                        "next_tool": "Accept Circle Terms of Use manually",
                    }
                )
                next_tool = "Accept Circle Terms of Use manually"

            # Wallet existence
            wallet_exists = False
            if runtime.wallet_address:
                try:
                    wallets = await runtime.cli_client.list_wallets(
                        network=runtime.config.circle_cli_network or runtime.config.blockchain
                        if runtime.config
                        else runtime.network
                    )
                    wallet_exists = any(
                        w.address.lower() == runtime.wallet_address.lower() for w in wallets
                    )
                except Exception:
                    pass
            result["wallet_exists"] = wallet_exists

            if not wallet_exists and runtime.wallet_address:
                blockers.append(
                    {
                        "code": "wallet_missing",
                        "next_tool": "Configure a valid existing Circle Agent Wallet",
                    }
                )

            # Canonical CAIP-2 network
            try:
                network_code = (
                    runtime.config.circle_cli_network or runtime.config.blockchain
                    if runtime.config
                    else runtime.network
                )
                caip2 = await runtime.cli_client.network_x402_identifier(network_code)
                result["canonical_caip2"] = caip2
            except Exception:
                result["canonical_caip2"] = "resolution_failed"

            # On-chain wallet USDC balance (best-effort)
            if wallet_exists and runtime.wallet_address:
                try:
                    network_code = (
                        runtime.config.circle_cli_network or runtime.config.blockchain
                        if runtime.config
                        else runtime.network
                    )
                    balances = await runtime.cli_client.get_balance(
                        wallet_address=runtime.wallet_address, network=network_code
                    )
                    usdc_balance = "0"
                    for b in balances:
                        if b.symbol == "USDC":
                            usdc_balance = b.amount
                            break
                    result["on_chain_usdc_balance"] = usdc_balance
                except Exception:
                    result["on_chain_usdc_balance"] = "unavailable"

            # Gateway balance (best-effort)
            if wallet_exists and runtime.wallet_address:
                try:
                    network_code = (
                        runtime.config.circle_cli_network or runtime.config.blockchain
                        if runtime.config
                        else runtime.network
                    )
                    gw = await runtime.cli_client.gateway_balance(
                        wallet_address=runtime.wallet_address, network=network_code
                    )
                    result["gateway_usdc_balance"] = gw.total_usdc
                except Exception:
                    result["gateway_usdc_balance"] = "unavailable"

        elif runtime.backend_name == "dcw":
            result["dcw_wallet_id"] = runtime.config.wallet_id if runtime.config else ""
            result["session_valid"] = None  # DCW doesn't use CLI sessions
            result["terms_accepted"] = None
            result["wallet_exists"] = True  # DCW wallet is always "existing"

        else:
            result["cli_available"] = False

        # buyer_runtime_ready: configured + backend available
        buyer_runtime_ready = (
            runtime.is_configured
            and runtime.is_available
            and (
                runtime.backend_name != "cli"
                or (runtime.cli_client is not None and result.get("session_valid"))
            )
        )

        # gateway_funding_ready: gateway balance > 0
        gw_balance_str = result.get("gateway_usdc_balance")
        gateway_funding_ready = False
        if gw_balance_str and gw_balance_str != "unavailable":
            try:
                gw_decimal = Decimal(gw_balance_str)
                if gw_decimal.is_finite():
                    gateway_funding_ready = gw_decimal > Decimal("0")
            except (InvalidOperation, ValueError):
                pass

        result["buyer_runtime_ready"] = buyer_runtime_ready
        result["gateway_funding_ready"] = gateway_funding_ready

        # Separate blockers for buyer vs gateway
        buyer_blockers: list[dict[str, str]] = []
        gateway_blockers: list[dict[str, str]] = []

        for blocker in blockers:
            if blocker["code"] in ("gateway_balance_insufficient",):
                gateway_blockers.append(blocker)
            else:
                buyer_blockers.append(blocker)

        if not gateway_funding_ready and buyer_runtime_ready:
            next_tool = "x402_gateway_balance"
            gateway_blockers.append(
                {
                    "code": "gateway_balance_empty",
                    "next_tool": "x402_gateway_deposit_preview",
                }
            )

        result["blockers"] = {
            "buyer": buyer_blockers,
            "gateway": gateway_blockers,
        }
        result["next_tool"] = next_tool

        return format_success_result(result)

    ctx.register_tool(
        name="x402_wallet_status",
        toolset="x402",
        schema=X402_WALLET_STATUS_SCHEMA,
        handler=wallet_status_handler,
        is_async=True,
        description=(
            "Report Circle wallet status: CLI installation, authentication, "
            "session validity, terms state, wallet existence, on-chain balance, "
            "Gateway balance, blockers, and recommended next tool. Read-only. "
            "Never exposes entity secret, API key, or signing operations."
        ),
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
            "Returns bounded results without payment. Read-only. "
            "Step 1 of marketplace discovery: x402_service_search -> "
            "x402_service_inspect -> x402_supports -> x402_pay."
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
        body = args.get("body")

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

            result = await check_supports(url, method=method, body=body, config=runtime.config)
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
            "Never signs, settles, deposits, or pays. This is a preflight check "
            "only — use x402_service_inspect to discover the URL first, then "
            "x402_supports to check payment compatibility, then x402_pay to pay. "
            "Preserve the HTTP method and payload format from inspection."
        ),
    )


# ---------------------------------------------------------------------------
# Service tools
# ---------------------------------------------------------------------------


def register_service_tools(ctx: Any) -> None:
    """Register x402_service_inspect tool."""

    async def service_inspect_handler(args: dict, **kwargs: Any) -> str:
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
                kwargs_req: dict[str, Any] = {
                    "method": method,
                    "url": url,
                    "follow_redirects": False,
                }
                if body is not None and method in {"POST", "PUT", "PATCH"}:
                    kwargs_req["json"] = body

                response = await client.request(**kwargs_req)

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
        description=(
            "Inspect an x402 service URL without paying. "
            "Issue an HTTP request to discover status, headers, and "
            "payment-required challenges. Always inspect BEFORE payment. "
            "Preserve the HTTP method and URL for subsequent x402_supports "
            "and x402_pay calls."
        ),
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
            "reports that payment is required but does not pay. "
            "For direct URL access: x402_fetch -> if 402, "
            "x402_service_inspect -> x402_supports -> x402_pay. "
            "Preserve the HTTP method for subsequent calls."
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
            "Capped by local configuration. x402_pay must obtain a fresh "
            "402 challenge from the server — never reuse a stale one. "
            "Never retry when retry_safe is false or the outcome is "
            "ambiguous. Authentication-required errors must be resolved "
            "before retrying. Insufficient Gateway balance must be "
            "reported as an actionable readiness failure, not handled by "
            "inventing a deposit flow."
        ),
    )


# ---------------------------------------------------------------------------
# Login tools
# ---------------------------------------------------------------------------


def register_login_tools(ctx: Any) -> None:
    """Register x402_login_start and x402_login_complete."""

    # Pending login state (in-memory only) — only used for chat_otp mode
    _pending_logins: dict[str, dict[str, Any]] = {}

    def _resolve_network_info(runtime: Any) -> tuple[str, bool, str]:
        """Resolve network code, testnet flag, and CLI chain name."""
        network_code = (
            runtime.config.circle_cli_network or runtime.config.blockchain
            if runtime.config
            else runtime.network
        )
        try:
            from hermes_x402.networks import get_network

            net = get_network(network_code)
            is_testnet = net.environment == "testnet"
            cli_chain = net.cli_chain or network_code
        except Exception:
            is_testnet = "testnet" in network_code.lower()
            cli_chain = network_code
        return network_code, is_testnet, cli_chain

    async def login_start_handler(args: dict, **kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()
        if not runtime.cli_client:
            return format_success_result(
                {
                    "success": False,
                    "error": "cli_not_available",
                    "message": "Login requires Circle CLI backend.",
                }
            )

        email = args.get("email", "").strip()
        if not email or "@" not in email:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": "A valid email address is required.",
                }
            )

        mode = (args.get("mode") or "manual_cli").strip().lower()
        if mode not in ("manual_cli", "chat_otp"):
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": "mode must be 'manual_cli' or 'chat_otp'.",
                }
            )

        # Fail-closed: check existing session for the configured environment
        try:
            status = await runtime.cli_client.agent_wallet_status()
        except Exception:
            return format_success_result(
                {
                    "success": False,
                    "error": "session_status_unknown",
                    "retry_safe": True,
                    "message": "Could not determine Circle session status.",
                }
            )

        # Check session for the configured environment specifically
        network_code, is_testnet, cli_chain = _resolve_network_info(runtime)
        if is_testnet:
            session_valid = status.testnet_status == "VALID"
        else:
            session_valid = status.mainnet_status == "VALID"

        if session_valid:
            return format_success_result(
                {
                    "success": False,
                    "error": "session_active",
                    "message": "A valid session already exists. No login needed.",
                }
            )

        if not status.terms_accepted:
            return format_success_result(
                {
                    "success": False,
                    "error": "terms_action_required",
                    "message": "Circle Terms of Use must be accepted manually before login.",
                }
            )

        testnet_flag = " --testnet" if is_testnet else ""

        # manual_cli mode: do NOT call login_start(), just return the command
        if mode == "manual_cli":
            manual_command = f"circle wallet login {email} --type agent{testnet_flag}"
            return format_success_result(
                {
                    "success": True,
                    "mode": "manual_cli",
                    "command": manual_command,
                    "message": (
                        "Run this command in your terminal to complete login. "
                        "The OTP never enters Hermes chat."
                    ),
                }
            )

        # chat_otp mode: require X402_ALLOW_CHAT_OTP=true
        allow_chat_otp = runtime.config.allow_chat_otp if runtime.config else False
        if not allow_chat_otp:
            return format_success_result(
                {
                    "success": False,
                    "error": "chat_otp_disabled",
                    "message": (
                        "Chat OTP login is disabled. "
                        "Set X402_ALLOW_CHAT_OTP=true or use mode=manual_cli."
                    ),
                }
            )

        # Reject parallel pending logins
        now = time.time()
        expired = [k for k, v in _pending_logins.items() if now > v.get("expires_at", 0)]
        for k in expired:
            _pending_logins.pop(k, None)

        active = [k for k, v in _pending_logins.items() if v.get("active")]
        if active:
            return format_success_result(
                {
                    "success": False,
                    "error": "pending_login_exists",
                    "message": (
                        "A login request is already pending. Complete it or wait for expiry."
                    ),
                }
            )

        # Call login_start with testnet flag
        try:
            result = await runtime.cli_client.login_start(
                email=email,
                testnet=is_testnet,
            )

            login_id = secrets.token_urlsafe(16)
            _pending_logins[login_id] = {
                "active": True,
                "circle_request_id": result.request_id,
                "email": email,
                "is_testnet": is_testnet,  # Store for verification
                "created_at": now,
                "expires_at": now + 300,
            }

            return format_success_result(
                {
                    "success": True,
                    "mode": "chat_otp",
                    "login_id": login_id,
                    "email_masked": result.email_masked,
                    "otp_required": result.otp_required,
                    "message": (
                        "OTP sent to your email. Use x402_login_complete with the OTP. "
                        "WARNING: The OTP will pass through conversation and "
                        "model/tool context."
                    ),
                }
            )
        except Exception as exc:
            return format_error_result(exc)

    ctx.register_tool(
        name="x402_login_start",
        toolset="x402",
        schema=X402_LOGIN_START_SCHEMA,
        handler=login_start_handler,
        is_async=True,
        description=(
            "Start Circle Agent Wallet email OTP login. Only runs when no "
            "valid session exists. Returns an opaque login_id with 5-minute "
            "expiry. Choice A (recommended): manual CLI login — OTP never "
            "enters chat. Choice B (optional, disabled by default): chat OTP "
            "via x402_login_complete — requires X402_ALLOW_CHAT_OTP=true. "
            "Never accepts or stores Circle Terms of Use."
        ),
    )

    async def login_complete_handler(args: dict, **kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()
        if not runtime.cli_client:
            return format_success_result(
                {
                    "success": False,
                    "error": "cli_not_available",
                    "message": "Login completion requires Circle CLI backend.",
                }
            )

        # Check if chat OTP is allowed
        allow_chat_otp = runtime.config.allow_chat_otp if runtime.config else False
        if not allow_chat_otp:
            return format_success_result(
                {
                    "success": False,
                    "error": "chat_otp_disabled",
                    "message": "Chat OTP login is disabled. Complete login through Circle CLI.",
                    "next_action": "Use the manual Circle CLI login flow.",
                }
            )

        login_id = args.get("login_id", "").strip()
        otp = args.get("otp", "").strip()
        acknowledge = args.get("acknowledge_otp_exposure")

        if not login_id or not otp:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": "Both login_id and otp are required.",
                }
            )

        if acknowledge is not True:
            return format_success_result(
                {
                    "success": False,
                    "error": "otp_exposure_not_acknowledged",
                    "message": (
                        "You must set acknowledge_otp_exposure=true. "
                        "Chat OTP is not secure or private — the OTP passes "
                        "through conversation and model/tool context."
                    ),
                }
            )

        # Validate pending login
        pending = _pending_logins.get(login_id)
        if not pending or not pending.get("active"):
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_request",
                    "message": (
                        "No active login request for this ID. "
                        "Start a new login with x402_login_start."
                    ),
                }
            )

        if time.time() > pending.get("expires_at", 0):
            _pending_logins.pop(login_id, None)
            return format_success_result(
                {
                    "success": False,
                    "error": "request_expired",
                    "message": "Login request expired. Start a new login.",
                }
            )

        # Resolve the raw Circle request ID internally
        circle_request_id = pending["circle_request_id"]
        is_testnet = pending.get("is_testnet", False)

        # Mark consumed before OTP submission (OTP never logged or returned)
        pending["active"] = False
        _pending_logins.pop(login_id, None)

        try:
            # OTP exists in memory only for this call
            session = await runtime.cli_client.login_complete(request_id=circle_request_id, otp=otp)

            # Verify the session is valid for the expected environment
            if is_testnet:
                env_valid = session.testnet_status == "VALID"
            else:
                env_valid = session.mainnet_status == "VALID"

            return format_success_result(
                {
                    "success": True,
                    "authenticated": session.authenticated,
                    "environment_valid": env_valid,
                    "environment": ("testnet" if is_testnet else "mainnet"),
                    "message": "Login successful."
                    if env_valid
                    else f"Login incomplete: session not valid for "
                    f"{'testnet' if is_testnet else 'mainnet'} environment.",
                }
            )
        except Exception as exc:
            return format_error_result(exc)

    ctx.register_tool(
        name="x402_login_complete",
        toolset="x402",
        schema=X402_LOGIN_COMPLETE_SCHEMA,
        handler=login_complete_handler,
        is_async=True,
        description=(
            "Complete Circle Agent Wallet login with OTP via chat. "
            "Disabled by default — requires X402_ALLOW_CHAT_OTP=true. "
            "Requires acknowledge_otp_exposure=true. "
            "OTP exists in memory only for the duration of the call. "
            "Never logs or returns OTP. "
            "Failed OTP consumes the login — require new login_start."
        ),
    )


# ---------------------------------------------------------------------------
# Gateway tools
# ---------------------------------------------------------------------------


def register_gateway_tools(ctx: Any) -> None:
    """Register x402_gateway_balance, x402_gateway_deposit_preview, x402_gateway_deposit_execute."""

    async def gateway_balance_handler(**kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()
        if not runtime.cli_client:
            return format_success_result(
                {
                    "success": False,
                    "error": "cli_not_available",
                    "message": "Gateway balance requires Circle CLI backend.",
                }
            )
        if not runtime.config or not runtime.wallet_address:
            return format_success_result(
                {
                    "success": False,
                    "error": "not_configured",
                    "message": "No wallet configured for Gateway balance.",
                }
            )
        try:
            network = runtime.config.circle_cli_network or runtime.config.blockchain
            gw = await runtime.cli_client.gateway_balance(
                wallet_address=runtime.wallet_address, network=network
            )
            # Use Decimal for safe comparison
            try:
                gw_decimal = Decimal(gw.total_usdc)
                if not gw_decimal.is_finite():
                    raise InvalidOperation("Non-finite Gateway balance")
            except (InvalidOperation, ValueError) as exc:
                from hermes_x402.circle_cli.errors import CircleCliOutputError

                raise CircleCliOutputError(f"Malformed Gateway balance: {gw.total_usdc!r}") from exc
            return format_success_result(
                {
                    "success": True,
                    "wallet": safe_wallet_address(runtime.wallet_address),
                    "total_usdc": gw.total_usdc,
                    "network": gw.network,
                    "domain": gw.domain,
                    "ready_for_payment": gw_decimal > Decimal("0"),
                }
            )
        except CircleCliError as exc:
            return format_error_result(exc)
        except Exception as exc:
            return format_error_result(exc)

    ctx.register_tool(
        name="x402_gateway_balance",
        toolset="x402",
        schema=X402_GATEWAY_BALANCE_SCHEMA,
        handler=gateway_balance_handler,
        is_async=True,
        description=(
            "Report Circle Gateway balance for the active wallet and configured "
            "network. Distinguishes Gateway balance from on-chain wallet USDC "
            "balance. Read-only."
        ),
    )

    # Preview store — delegated to shared module
    from hermes_x402.hermes_plugin.gateway_state import (
        store_preview,
    )

    async def deposit_preview_handler(args: dict, **kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()
        if not runtime.cli_client:
            return format_success_result(
                {
                    "success": False,
                    "error": "cli_not_available",
                    "message": "Deposit preview requires Circle CLI backend.",
                }
            )
        if not runtime.config or not runtime.wallet_address:
            return format_success_result(
                {
                    "success": False,
                    "error": "not_configured",
                    "message": "No wallet configured for deposit.",
                }
            )

        # Require service_url, method, and amount
        service_url = args.get("service_url", "").strip()
        method = (args.get("method") or "").strip().upper()
        amount = args.get("amount", "").strip()

        if not service_url:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": "service_url is required.",
                }
            )
        if method not in ALLOWED_HTTP_METHODS:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": (
                        f"HTTP method must be one of: {', '.join(sorted(ALLOWED_HTTP_METHODS))}"
                    ),
                }
            )
        if not amount:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": "Amount is required.",
                }
            )

        # Validate amount is a valid positive finite decimal
        try:
            parsed_amount = Decimal(amount)
            if not parsed_amount.is_finite() or parsed_amount <= 0:
                raise InvalidOperation()
        except (InvalidOperation, ValueError):
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": "Amount must be a positive decimal number.",
                }
            )

        # Enforce 0.5 USDC minimum deposit BEFORE any network calls
        if parsed_amount < Decimal("0.5"):
            return format_success_result(
                {
                    "success": False,
                    "error": "minimum_deposit_not_met",
                    "message": (f"Minimum Gateway deposit is 0.5 USDC. Requested: {amount}."),
                }
            )

        # Validate and canonicalize body BEFORE any network calls
        body = args.get("body") if method in {"POST", "PUT", "PATCH"} else None
        canonical_body = None
        body_hash = None
        if body is not None:
            import json as _json

            try:
                canonical_body = json.loads(_json.dumps(body, sort_keys=True, default=str))
            except (TypeError, ValueError) as exc:
                return format_success_result(
                    {
                        "success": False,
                        "error": "invalid_input",
                        "message": f"Request body is not valid JSON: {exc}",
                    }
                )
            body_bytes = _json.dumps(canonical_body, sort_keys=True, default=str).encode()
            if len(body_bytes) > 65536:  # 64KB
                return format_success_result(
                    {
                        "success": False,
                        "error": "invalid_input",
                        "message": "Request body exceeds 64KB limit.",
                    }
                )
            body_hash = hashlib.sha256(body_bytes).hexdigest()[:16]

        # Validate URL with public network policy
        policy_mode = runtime.config.network_policy if runtime.config else "public"
        policy_allowlist = runtime.config.host_allowlist if runtime.config else []
        policy_allow_http = runtime.config.allow_http if runtime.config else False

        err = _validate_allowed_url(
            service_url, policy_allowlist, mode=policy_mode, allow_http=policy_allow_http
        )
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "host_rejected"
                    if "allowlist" in err or "private" in err or "blocked" in err
                    else "invalid_input",
                    "message": err,
                }
            )

        # DNS/SSRF validation
        try:
            from hermes_x402.dns_validator import resolve_and_validate_destination

            await resolve_and_validate_destination(service_url)
        except ValueError as exc:
            return format_success_result(
                {"success": False, "error": "destination_rejected", "message": str(exc)}
            )

        # Use the existing x402 challenge parser (check_supports)
        # This handles v2 header, v1 body, GatewayWalletBatched detection,
        # canonical networks, and backend compatibility
        from hermes_x402.buyer.supports import check_supports

        network = (
            runtime.config.circle_cli_network or runtime.config.blockchain
            if runtime.config
            else runtime.network
        )

        body = args.get("body") if method in {"POST", "PUT", "PATCH"} else None
        try:
            support = await check_supports(
                service_url,
                method=method,
                body=canonical_body,
                config=runtime.config,
            )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "challenge_parse_failed",
                    "message": f"Failed to parse x402 challenge: {exc}",
                }
            )

        if not support.x402:
            return format_success_result(
                {
                    "success": False,
                    "error": "not_payment_required",
                    "message": (
                        f"Service returned non-402 status. {support.reason or 'No x402 challenge.'}"
                    ),
                    "next_tool": "x402_pay",
                }
            )

        if not support.gateway_batching:
            return format_success_result(
                {
                    "success": False,
                    "error": "gateway_not_required",
                    "message": (
                        "The service does not require Gateway funding "
                        "(no GatewayWalletBatched payment option found)."
                    ),
                    "next_tool": "x402_pay",
                }
            )

        # Find the gateway_batching option for our network
        gateway_option = None
        for opt in support.options:
            if opt.payment_system == "gateway_batching" and opt.supported_by_backend:
                gateway_option = opt
                break

        if not gateway_option:
            # Get network from the first gateway option (even if not supported)
            fallback_net = next(
                (o.network for o in support.options if o.payment_system == "gateway_batching"),
                network,
            )
            return format_success_result(
                {
                    "success": False,
                    "error": "gateway_network_mismatch",
                    "message": (
                        "Gateway payment option found but not compatible "
                        f"with configured network ({fallback_net})."
                    ),
                }
            )

        # Extract details from the gateway option
        network = gateway_option.network
        gateway_network = gateway_option.network_id

        # Resolve configured environment for session check
        try:
            from hermes_x402.networks import get_network

            resolved_net = get_network(network)
            is_testnet = resolved_net.environment == "testnet"
        except Exception:
            is_testnet = "testnet" in network.lower()

        # Verify session validity for configured environment
        try:
            status = await runtime.cli_client.agent_wallet_status()

            # Check session for the configured environment
            if is_testnet:
                session_valid = status.testnet_status == "VALID"
            else:
                session_valid = status.mainnet_status == "VALID"

            if not session_valid:
                return format_success_result(
                    {
                        "success": False,
                        "error": "session_invalid",
                        "message": "Session is not valid. Login first.",
                    }
                )
            if not status.terms_accepted:
                return format_success_result(
                    {
                        "success": False,
                        "error": "terms_action_required",
                        "message": "Circle Terms of Use must be accepted.",
                    }
                )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "session_check_failed",
                    "message": f"Could not verify session: {exc}",
                }
            )

        # Verify wallet balance
        usdc_balance = "0"
        try:
            balances = await runtime.cli_client.get_balance(
                wallet_address=runtime.wallet_address, network=network
            )
            for b in balances:
                if b.symbol == "USDC":
                    usdc_balance = b.amount
                    break
            if Decimal(usdc_balance) < parsed_amount:
                return format_success_result(
                    {
                        "success": False,
                        "error": "insufficient_balance",
                        "message": (
                            f"On-chain USDC balance ({usdc_balance}) < requested ({amount})."
                        ),
                    }
                )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "balance_check_failed",
                    "message": f"Could not verify wallet balance: {exc}",
                }
            )

        # Get current Gateway balance
        gw_balance = "0"
        try:
            gw = await runtime.cli_client.gateway_balance(
                wallet_address=runtime.wallet_address, network=network
            )
            gw_balance = gw.total_usdc
        except Exception:
            pass  # Gateway balance is informational

        # Resolve network and verify Gateway support — fail closed
        try:
            from hermes_x402.networks import get_network

            resolved_net = get_network(network)
            if not resolved_net.gateway_supported:
                return format_success_result(
                    {
                        "success": False,
                        "error": "gateway_not_supported",
                        "message": (f"Network {network} does not support Gateway deposits."),
                    }
                )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "network_resolution_failed",
                    "message": f"Could not resolve network: {exc}",
                }
            )

        # Deposit method: direct only for this PR
        deposit_method = "direct"

        # Configuration fingerprint
        config_fingerprint = hashlib.sha256(
            f"{runtime.wallet_address}:{network}".encode()
        ).hexdigest()[:16]

        # Body hash already computed during input validation
        # Store canonical body for revalidation (never in output)

        # Service payment-option fingerprint from all option fields
        service_option_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "scheme": gateway_option.scheme,
                    "payment_system": gateway_option.payment_system,
                    "network": gateway_option.network,
                    "network_id": gateway_option.network_id,
                    "amount_atomic": gateway_option.amount_atomic,
                    "asset": gateway_option.asset,
                    "pay_to": gateway_option.pay_to,
                    "max_timeout_seconds": gateway_option.max_timeout_seconds,
                    "x402_version": support.version,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]

        # Generate preview ID with full state
        preview_id = secrets.token_urlsafe(16)
        preview_data = {
            # Deposit parameters
            "deposit_amount": amount,
            # Service payment-option fields (for fingerprint)
            "service_payment_amount": gateway_option.amount_usdc,
            "x402_version": support.version,
            "scheme": gateway_option.scheme,
            "payment_system": gateway_option.payment_system,
            "network": gateway_option.network,
            "network_id": gateway_option.network_id,
            "asset": gateway_option.asset,
            "pay_to": gateway_option.pay_to,
            "max_timeout_seconds": gateway_option.max_timeout_seconds,
            # Service binding
            "service_url": service_url,
            "method": method,
            "body_hash": body_hash,
            "body": canonical_body,  # Stored for revalidation, never in output
            # Wallet and config
            "wallet": runtime.wallet_address,
            "wallet_network": network,
            "deposit_method": deposit_method,
            "config_fingerprint": config_fingerprint,
            "service_option_fingerprint": service_option_fingerprint,
            "created_at": time.time(),
            "expires_at": time.time() + 300,
            "consumed": False,
        }
        store_preview(preview_id, preview_data)

        return format_success_result(
            {
                "success": True,
                "preview_id": preview_id,
                "service_url": service_url,
                "method": method,
                "wallet": safe_wallet_address(runtime.wallet_address),
                "amount": amount,
                "deposit_method": deposit_method,
                "source_network": network,
                "gateway_destination": gateway_network,
                "wallet_usdc_balance": usdc_balance,
                "gateway_usdc_balance": gw_balance,
                "expires_at": preview_data["expires_at"],
                "approval_required": True,
                "message": (
                    "Preview created. Present deposit details to user for approval. "
                    "Use x402_gateway_deposit_execute with this preview_id."
                ),
            }
        )

    ctx.register_tool(
        name="x402_gateway_deposit_preview",
        toolset="x402",
        schema=X402_GATEWAY_DEPOSIT_PREVIEW_SCHEMA,
        handler=deposit_preview_handler,
        is_async=True,
        description=(
            "Preview a Gateway deposit for a specific service. Requires "
            "service_url, HTTP method, and amount. Validates URL policy, "
            "verifies the seller advertises a Gateway payment option, "
            "checks network compatibility, session, terms, and wallet balance. "
            "Returns a short-lived preview ID bound to all parameters. "
            "Read-only — must not move USDC."
        ),
    )

    async def deposit_execute_handler(args: dict, **kwargs: Any) -> str:
        runtime = get_runtime()
        runtime.ensure_initialized()
        if not runtime.cli_client:
            return format_success_result(
                {
                    "success": False,
                    "error": "cli_not_available",
                    "message": "Deposit execution requires Circle CLI backend.",
                }
            )

        preview_id = args.get("preview_id", "").strip()
        if not preview_id:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": "preview_id is required.",
                }
            )

        # Atomically claim the preview under a single lock
        from hermes_x402.hermes_plugin.gateway_state import (
            claim_preview_for_execution,
        )

        preview = claim_preview_for_execution(preview_id)
        if preview is None:
            return format_success_result(
                {
                    "success": False,
                    "error": "preview_invalid",
                    "message": (
                        "Preview is missing, expired, or already consumed. Create a new preview."
                    ),
                }
            )

        # preview is now claimed — consumed flag is set under the lock

        # Resolve configured environment for session check
        preview_network = preview.get("wallet_network", "")
        try:
            from hermes_x402.networks import get_network

            resolved_net = get_network(preview_network)
            is_testnet = resolved_net.environment == "testnet"
        except Exception:
            is_testnet = "testnet" in preview_network.lower()

        # Revalidate session for configured environment
        try:
            status = await runtime.cli_client.agent_wallet_status()

            if is_testnet:
                session_valid = status.testnet_status == "VALID"
            else:
                session_valid = status.mainnet_status == "VALID"

            if not session_valid:
                return format_success_result(
                    {
                        "success": False,
                        "error": "session_invalid",
                        "message": "Session is no longer valid. Login again.",
                    }
                )
            if not status.terms_accepted:
                return format_success_result(
                    {
                        "success": False,
                        "error": "terms_action_required",
                        "message": "Circle Terms of Use must be accepted.",
                    }
                )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "session_check_failed",
                    "message": f"Could not verify session: {exc}",
                }
            )

        # Revalidate config fingerprint
        current_wallet = runtime.wallet_address
        current_network = (
            runtime.config.circle_cli_network or runtime.config.blockchain if runtime.config else ""
        )
        current_fingerprint = hashlib.sha256(
            f"{current_wallet}:{current_network}".encode()
        ).hexdigest()[:16]

        if preview["wallet"] != current_wallet:
            return format_success_result(
                {
                    "success": False,
                    "error": "config_mismatch",
                    "message": "Wallet changed since preview. Create a new preview.",
                }
            )
        if preview["wallet_network"] != current_network:
            return format_success_result(
                {
                    "success": False,
                    "error": "config_mismatch",
                    "message": "Network changed since preview. Create a new preview.",
                }
            )
        if preview["config_fingerprint"] != current_fingerprint:
            return format_success_result(
                {
                    "success": False,
                    "error": "config_mismatch",
                    "message": "Configuration changed since preview. Create a new preview.",
                }
            )

        # Enforce minimum deposit of 0.5 USDC (defense in depth)
        deposit_amount = Decimal(preview["deposit_amount"])
        if deposit_amount < Decimal("0.5"):
            return format_success_result(
                {
                    "success": False,
                    "error": "minimum_deposit_not_met",
                    "message": (
                        f"Minimum Gateway deposit is 0.5 USDC. "
                        f"Requested: {preview['deposit_amount']}."
                    ),
                }
            )

        # Resolve network through get_network() — fail closed
        try:
            from hermes_x402.networks import get_network

            resolved_net = get_network(current_network)
            if not resolved_net.gateway_supported:
                return format_success_result(
                    {
                        "success": False,
                        "error": "gateway_not_supported",
                        "message": f"Network {current_network} does not support Gateway deposits.",
                    }
                )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "network_resolution_failed",
                    "message": f"Could not resolve network: {exc}",
                }
            )

        # Verify deposit method is direct only
        deposit_method = preview.get("deposit_method", "direct")
        if deposit_method != "direct":
            return format_success_result(
                {
                    "success": False,
                    "error": "unsupported_deposit_method",
                    "message": f"Only 'direct' deposit is supported. Got: {deposit_method}.",
                }
            )

        # Reverify wallet has sufficient USDC
        try:
            balances = await runtime.cli_client.get_balance(
                wallet_address=runtime.wallet_address, network=current_network
            )
            usdc_balance = "0"
            for b in balances:
                if b.symbol == "USDC":
                    usdc_balance = b.amount
                    break
            if Decimal(usdc_balance) < deposit_amount:
                return format_success_result(
                    {
                        "success": False,
                        "error": "insufficient_balance",
                        "message": (
                            f"On-chain USDC balance ({usdc_balance}) < "
                            f"deposit amount ({preview['deposit_amount']})."
                        ),
                    }
                )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "balance_check_failed",
                    "message": f"Could not verify wallet balance: {exc}",
                }
            )

        # Re-fetch challenge and compare service payment-option fingerprint
        from hermes_x402.buyer.supports import check_supports

        try:
            fresh_support = await check_supports(
                preview["service_url"],
                method=preview["method"],
                body=preview.get("body"),  # Pass stored body for revalidation
                config=runtime.config,
            )
        except Exception as exc:
            return format_success_result(
                {
                    "success": False,
                    "error": "challenge_revalidation_failed",
                    "message": f"Could not re-fetch x402 challenge: {exc}",
                    "retry_safe": False,
                }
            )

        if not fresh_support.x402 or not fresh_support.gateway_batching:
            return format_success_result(
                {
                    "success": False,
                    "error": "service_changed",
                    "message": "Service no longer offers Gateway payment. Create a new preview.",
                }
            )

        # Find ALL compatible gateway options and search for exact fingerprint
        fresh_gateway_option = None
        for opt in fresh_support.options:
            if opt.payment_system == "gateway_batching" and opt.supported_by_backend:
                # Recompute fingerprint from this option
                opt_fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "scheme": opt.scheme,
                            "payment_system": opt.payment_system,
                            "network": opt.network,
                            "network_id": opt.network_id,
                            "amount_atomic": opt.amount_atomic,
                            "asset": opt.asset,
                            "pay_to": opt.pay_to,
                            "max_timeout_seconds": opt.max_timeout_seconds,
                            "x402_version": fresh_support.version,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()[:16]
                if opt_fingerprint == preview.get("service_option_fingerprint"):
                    fresh_gateway_option = opt
                    break

        if not fresh_gateway_option:
            return format_success_result(
                {
                    "success": False,
                    "error": "service_changed",
                    "message": (
                        "Service payment option changed since preview. Create a new preview."
                    ),
                }
            )

        # Mark consumed before submission
        preview["consumed"] = True

        try:
            result = await runtime.cli_client.gateway_deposit(
                wallet_address=preview["wallet"],
                network=preview["wallet_network"],
                amount=preview["deposit_amount"],
                method=deposit_method,
            )
            output: dict[str, Any] = {
                "success": True,
                "status": result.status,
                "network": result.network,
            }
            if result.operation_id:
                output["operation_id"] = result.operation_id
            if result.transaction_hash:
                output["transaction_hash"] = result.transaction_hash
            return format_success_result(output)
        except CircleCliPaymentOutcomeUnknownError:
            return format_success_result(
                {
                    "success": False,
                    "error": "gateway_deposit_outcome_unknown",
                    "retry_safe": False,
                    "message": (
                        "The Gateway deposit may have been submitted. "
                        "Check balances or transaction status before any new deposit."
                    ),
                }
            )
        except Exception as exc:
            # Consumed tombstone remains — same-process replay rejected
            return format_error_result(exc)

    ctx.register_tool(
        name="x402_gateway_deposit_execute",
        toolset="x402",
        schema=X402_GATEWAY_DEPOSIT_EXECUTE_SCHEMA,
        handler=deposit_execute_handler,
        is_async=True,
        description=(
            "Execute a Gateway deposit using a preview ID from "
            "x402_gateway_deposit_preview. Do not accept replacement amount, "
            "wallet, network, or method. Revalidates session, config, wallet, "
            "service, and preview expiry. Execute exactly once. "
            "retry_safe=false for ambiguous outcomes."
        ),
    )
