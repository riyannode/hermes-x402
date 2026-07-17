"""Tool registration functions for the x402 Hermes plugin.

Each function registers a group of related tools. All handlers return
JSON strings. No subprocess, network, or payment calls at registration time.

Registered tools:
  x402_status          — plugin status and configuration
  x402_wallet_status   — Circle wallet status (read-only)
  x402_wallet_balance  — wallet USDC balance (read-only)
  x402_service_inspect — inspect a service URL without paying
  x402_fetch           — fetch a URL without paying
  x402_pay             — pay for an x402 resource (may transfer USDC)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from hermes_x402.buyer.errors import BuyerError, PaymentSubmissionUnknownError
from hermes_x402.circle_cli.errors import CircleCliError
from hermes_x402.hermes_plugin.errors import format_error_result, format_success_result
from hermes_x402.hermes_plugin.output import safe_wallet_address
from hermes_x402.hermes_plugin.runtime import get_runtime
from hermes_x402.hermes_plugin.schemas import (
    ALLOWED_HTTP_METHODS,
    MAX_BODY_SIZE,
    MAX_HEADER_COUNT,
    MAX_OUTPUT_SIZE,
    MAX_QUERY_LENGTH,
    MAX_URL_LENGTH,
    X402_FETCH_SCHEMA,
    X402_PAY_SCHEMA,
    X402_SERVICE_INSPECT_SCHEMA,
    X402_STATUS_SCHEMA,
    X402_WALLET_BALANCE_SCHEMA,
    X402_WALLET_STATUS_SCHEMA,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


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


def _validate_max_usdc(
    max_usdc: str | None, configured_max: str | None
) -> tuple[str | None, str | None]:
    """Validate caller cap. Returns (validated_cap, error_message)."""
    if max_usdc is None:
        return configured_max, None
    try:
        from decimal import Decimal

        caller_cap = Decimal(max_usdc)
        if caller_cap < 0:
            return None, "max_usdc must be non-negative."
    except Exception:
        return None, "max_usdc is invalid."

    if configured_max is not None:
        try:
            from decimal import Decimal

            if caller_cap > Decimal(configured_max):
                return None, (
                    "Caller cap exceeds configured maximum. The configured cap cannot be raised."
                )
        except Exception:
            pass

    return max_usdc, None


def _run_async(coro: Any) -> Any:
    """Run an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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

        result: dict[str, Any] = {
            "success": True,
            "plugin": "hermes-x402",
            "version": runtime.version,
            "role": runtime.role or "unconfigured",
            "backend": runtime.backend_name or "none",
            "network": runtime.network or "none",
            "wallet_address": safe_wallet,
            "configured": runtime.is_configured,
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
        description="Report Circle wallet status. Read-only. Never exposes secrets.",
    )

    def wallet_balance_handler(**kwargs: Any) -> str:
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
                balances = _run_async(
                    runtime.cli_client.get_balance(
                        wallet_address=runtime.wallet_address,
                        network=runtime.network,
                    )
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
        handler=lambda args, **kw: wallet_balance_handler(**kw),
        description="Report configured wallet USDC balance. Read-only.",
    )


# ---------------------------------------------------------------------------
# Service tools
# ---------------------------------------------------------------------------


def register_service_tools(ctx: Any) -> None:
    """Register x402_service_inspect tool."""

    def service_inspect_handler(args: dict, **kwargs: Any) -> str:
        url = args.get("url", "")
        err = _validate_url(url)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": err,
                }
            )

        runtime = get_runtime()
        runtime.ensure_initialized()
        if runtime.config and runtime.config.host_allowlist:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            allowed = any(
                host == item.lower() or host.endswith(f".{item.lower()}")
                for item in runtime.config.host_allowlist
            )
            if not allowed:
                return format_success_result(
                    {
                        "success": False,
                        "error": "host_rejected",
                        "message": f"Host not in allowlist: {host}",
                    }
                )

        try:
            with httpx.Client(timeout=15) as client:
                response = client.head(url, follow_redirects=True)
                result_data: dict[str, Any] = {
                    "success": True,
                    "url": url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "headers": dict(list(response.headers.items())[:MAX_HEADER_COUNT]),
                }
                if response.status_code == 402:
                    result_data["payment_required"] = True
                    result_data["payment_header"] = response.headers.get("Payment-Required", "")
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
        description="Inspect an x402 service URL without paying.",
    )


# ---------------------------------------------------------------------------
# Payment tools
# ---------------------------------------------------------------------------


def register_payment_tools(ctx: Any) -> None:
    """Register x402_fetch and x402_pay tools."""

    def fetch_handler(args: dict, **kwargs: Any) -> str:
        url = args.get("url", "")
        method = (args.get("method") or "GET").upper()
        body = args.get("body")

        err = _validate_url(url)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": err,
                }
            )

        err = _validate_method(method)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": err,
                }
            )

        err = _validate_body_size(body)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": err,
                }
            )

        try:
            with httpx.Client(timeout=30) as client:
                kwargs_req: dict[str, Any] = {
                    "method": method,
                    "url": url,
                    "follow_redirects": True,
                }
                if body is not None and method in {"POST", "PUT", "PATCH"}:
                    kwargs_req["json"] = body

                response = client.request(**kwargs_req)

                if response.status_code == 402:
                    return format_success_result(
                        {
                            "success": True,
                            "status": 402,
                            "payment_required": True,
                            "message": (
                                "This resource requires payment. Use x402_pay to purchase access."
                            ),
                            "payment_header": response.headers.get("Payment-Required", ""),
                        }
                    )

                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    data: Any = response.json()
                else:
                    data = response.text
                    if isinstance(data, str) and len(data) > MAX_OUTPUT_SIZE:
                        data = data[:MAX_OUTPUT_SIZE] + "\n[... truncated ...]"

                return format_success_result(
                    {
                        "success": True,
                        "status": response.status_code,
                        "data": data,
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
        description=(
            "Fetch a resource URL without paying. When HTTP 402 occurs, "
            "reports that payment is required but does not pay."
        ),
    )

    def pay_handler(args: dict, **kwargs: Any) -> str:
        url = args.get("url", "")
        method = (args.get("method") or "GET").upper()
        body = args.get("body")
        max_usdc = args.get("max_usdc")

        err = _validate_url(url)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": err,
                }
            )

        err = _validate_method(method)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": err,
                }
            )

        err = _validate_body_size(body)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "invalid_input",
                    "message": err,
                }
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

        configured_max = runtime.config.max_usdc_per_payment if runtime.config else None
        validated_cap, err = _validate_max_usdc(max_usdc, configured_max)
        if err:
            return format_success_result(
                {
                    "success": False,
                    "error": "payment_policy_error",
                    "message": err,
                }
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
            result = _run_async(
                buyer.pay(url=url, method=method, body=body, max_usdc=validated_cap)
            )

            output: dict[str, Any] = {
                "success": True,
                "payment_status": result.payment_status,
                "status": result.status,
                "payer": safe_wallet_address(result.payer) if result.payer else "",
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
                    "message": "Payment may have been submitted. Do not retry automatically.",
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
        description=(
            "⚠️ May transfer USDC. Pay for an x402 resource. "
            "Cannot change configured wallet, network, or backend. "
            "Capped by local configuration."
        ),
    )
