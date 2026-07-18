"""Hermes plugin entry point — registers x402 tools with the Hermes tool registry.

This module is discovered via the ``hermes_agent.plugins`` entry point group.
Hermes calls ``register(ctx)`` once at plugin load time.

Side-effect-free at import time: no subprocess calls, no network requests,
no wallet operations, no payment, no secret logging.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Tools that require native Hermes approval before execution
_APPROVAL_REQUIRED_TOOLS = frozenset(
    {
        "x402_pay",
        "x402_gateway_deposit_execute",
        "x402_login_complete",
    }
)

# Maximum displayed URL length in approval messages
_MAX_DISPLAY_URL_LENGTH = 120

# Control characters: all ASCII control chars including CR/LF
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def register(ctx: Any) -> None:
    """Register x402 tools with the Hermes plugin context.

    Split registration into focused groups for clarity. Each group
    registers related tools under the ``x402`` toolset.

    14 tools total:
      x402_status, x402_wallet_status, x402_wallet_balance,
      x402_networks, x402_service_search, x402_supports,
      x402_service_inspect, x402_fetch, x402_pay,
      x402_login_start, x402_login_complete,
      x402_gateway_balance, x402_gateway_deposit_preview,
      x402_gateway_deposit_execute
    """
    from hermes_x402.hermes_plugin.tools import (
        register_discovery_tools,
        register_gateway_tools,
        register_login_tools,
        register_network_tools,
        register_payment_tools,
        register_service_tools,
        register_status_tools,
        register_supports_tools,
        register_wallet_tools,
    )

    # Register native approval hook FIRST — fail closed if unavailable.
    # Hermes does not roll back globally registered tools when plugin
    # registration later fails, so the hook must be registered before tools.
    _register_approval_hook(ctx)

    register_status_tools(ctx)
    register_wallet_tools(ctx)
    register_network_tools(ctx)
    register_discovery_tools(ctx)
    register_supports_tools(ctx)
    register_service_tools(ctx)
    register_payment_tools(ctx)
    register_login_tools(ctx)
    register_gateway_tools(ctx)

    # Register /x402 slash command
    _register_slash_command(ctx)

    logger.debug("hermes-x402 plugin: registered x402 tools, approval hook, and command")


def _register_approval_hook(ctx: Any) -> None:
    """Register a pre_tool_call hook for native Hermes approval.

    Synchronous callback that returns action=approve for financial tools
    and None for unaffected tools. Uses tool_call_id as unique identity.

    Fails closed when the Hermes approval API is unavailable.
    """
    if not hasattr(ctx, "register_hook"):
        raise RuntimeError(
            "Hermes native approval API is required for hermes-x402. Plugin registration aborted."
        )

    def approval_hook(
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str = "",
        turn_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Synchronous pre_tool_call hook for native Hermes approval.

        Returns action=approve for financial tools, None for unaffected tools.
        Blocks when tool_call_id is missing for financial operations.
        """
        if tool_name not in _APPROVAL_REQUIRED_TOOLS:
            return None

        # Financial operations MUST require tool_call_id for unique identity.
        if not tool_call_id:
            return {
                "action": "block",
                "message": "Unique tool-call identity is unavailable.",
            }

        # Gateway deposit execute: block if preview is invalid
        if tool_name == "x402_gateway_deposit_execute":
            preview_id = args.get("preview_id", "")
            if not isinstance(preview_id, str):
                return {
                    "action": "block",
                    "message": "Gateway preview ID is required.",
                }
            preview_id = preview_id.strip()
            if not preview_id or len(preview_id) > 128:
                return {
                    "action": "block",
                    "message": "Gateway preview ID must be 1..128 characters.",
                }
            from hermes_x402.hermes_plugin.gateway_state import (
                get_gateway_preview_approval_summary,
            )

            summary = get_gateway_preview_approval_summary(preview_id)
            if summary is None:
                return {
                    "action": "block",
                    "message": ("Gateway preview is missing, expired, or already consumed."),
                }

        rule_key = f"hermes-x402:{tool_name}:{tool_call_id}"

        # Build informative sanitized approval messages
        description = _build_approval_message(tool_name, args)

        return {
            "action": "approve",
            "message": description,
            "rule_key": rule_key,
        }

    # Register without try/except — let exceptions propagate
    ctx.register_hook("pre_tool_call", approval_hook)
    logger.debug("hermes-x402: registered pre_tool_call approval hook")


def _register_slash_command(ctx: Any) -> None:
    """Register /x402 slash command for read-only status and safe configuration.

    Uses ctx.register_command for registration and ctx.dispatch_tool
    for delegating to existing tools. No duplicated handler logic.
    """
    from hermes_x402.hermes_plugin.slash_command import handle_x402_command

    def command_handler(raw_args: str = "", **kwargs: Any) -> str:
        return handle_x402_command(raw_args, ctx)

    ctx.register_command(
        "x402",
        command_handler,
        args_hint="",
    )
    logger.debug("hermes-x402: registered /x402 slash command")


def _sanitize_url_for_display(raw_url: Any) -> str:
    """Sanitize URL for approval display.

    - requires raw_url to be a string
    - strips all ASCII control characters
    - rebuilds netloc from hostname and port (never uses parsed.netloc)
    - removes userinfo
    - removes query
    - removes fragment
    - returns "[invalid URL]" on malformed input
    - limits displayed length
    """
    if not isinstance(raw_url, str):
        return "[invalid URL]"

    # Strip control characters
    cleaned = _CONTROL_CHARS.sub("", raw_url)

    try:
        parsed = urlparse(cleaned)
    except Exception:
        return "[invalid URL]"

    # Rebuild netloc from hostname and port — never use parsed.netloc
    # which may retain userinfo
    try:
        hostname = parsed.hostname
        port = parsed.port
    except (ValueError, UnicodeError):
        return "[invalid URL]"

    if not hostname:
        return "[invalid URL]"

    # IPv6 addresses must be bracketed in netloc
    if ":" in hostname:
        netloc = f"[{hostname}]:{port}" if port is not None else f"[{hostname}]"
    else:
        netloc = f"{hostname}:{port}" if port is not None else hostname

    # Rebuild without userinfo, fragment, or query
    sanitized = urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            "",  # params
            "",  # query — redacted
            "",  # fragment — removed
        )
    )

    # Limit length
    if len(sanitized) > _MAX_DISPLAY_URL_LENGTH:
        sanitized = sanitized[:_MAX_DISPLAY_URL_LENGTH] + "..."

    return sanitized


def _build_approval_message(tool_name: str, args: dict[str, Any]) -> str:
    """Build informative sanitized approval message.

    Includes: sanitized URL, method, caller max, configured cap.
    For Gateway: service URL, amount, wallet, network, method, expiry.
    Never includes: body, OTP, credentials, payment headers, raw query.
    """
    if tool_name == "x402_pay":
        raw_url = args.get("url", "unknown")
        display_url = _sanitize_url_for_display(raw_url)
        method = str(args.get("method", "GET"))[:10]  # Bound length
        max_usdc = str(args.get("max_usdc", "no cap"))[:20]  # Bound length

        # Get configured cap from runtime
        configured_cap = "default"
        try:
            from hermes_x402.hermes_plugin.runtime import get_runtime

            runtime = get_runtime()
            runtime.ensure_initialized()
            if runtime.config and runtime.config.max_usdc_per_payment:
                configured_cap = str(runtime.config.max_usdc_per_payment)[:20]
        except Exception:
            pass

        return (
            f"Pay for {display_url} via {method}. "
            f"Caller max: {max_usdc} USDC. "
            f"Configured cap: {configured_cap} USDC."
        )

    if tool_name == "x402_gateway_deposit_execute":
        from hermes_x402.hermes_plugin.gateway_state import (
            get_gateway_preview_approval_summary,
        )

        preview_id = args.get("preview_id", "")
        summary = get_gateway_preview_approval_summary(preview_id)

        if summary is None:
            return (
                f"Execute Gateway deposit via preview {preview_id}. "
                "Preview is missing, expired, or consumed."
            )

        display_url = _sanitize_url_for_display(summary["service_url"])
        expires_at = summary.get("expires_at", 0)
        remaining = max(0, int(expires_at - time.time()))

        return (
            f"Gateway deposit: {summary['deposit_amount']} USDC "
            f"to {display_url}. "
            f"Wallet: {summary['masked_wallet']}. "
            f"Network: {summary['network']}. "
            f"Method: {summary['deposit_method']}. "
            f"Expires in {remaining}s."
        )

    if tool_name == "x402_login_complete":
        return "Complete Circle login with OTP via chat (OTP exposed in conversation)."

    return f"Execute {tool_name}"
