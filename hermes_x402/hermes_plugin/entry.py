"""Hermes plugin entry point — registers x402 tools with the Hermes tool registry.

This module is discovered via the ``hermes_agent.plugins`` entry point group.
Hermes calls ``register(ctx)`` once at plugin load time.

Side-effect-free at import time: no subprocess calls, no network requests,
no wallet operations, no payment, no secret logging.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tools that require native Hermes approval before execution
_APPROVAL_REQUIRED_TOOLS = frozenset(
    {
        "x402_pay",
        "x402_gateway_deposit_execute",
        "x402_login_complete",
    }
)

# Human-readable descriptions for approval prompts
_APPROVAL_DESCRIPTIONS: dict[str, str] = {
    "x402_pay": "Pay for an x402 resource (may transfer USDC)",
    "x402_gateway_deposit_execute": "Execute Gateway deposit (may transfer USDC)",
    "x402_login_complete": (
        "Complete Circle login with OTP via chat (OTP exposed in conversation)"
    ),
}


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

    logger.debug("hermes-x402 plugin: registered x402 tools and approval hook")


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

        # x402_login_complete: only require approval when chat OTP is used
        if tool_name == "x402_login_complete":
            from hermes_x402.hermes_plugin.runtime import get_runtime

            runtime = get_runtime()
            runtime.ensure_initialized()
            allow_chat_otp = runtime.config.allow_chat_otp if runtime.config else False
            if not allow_chat_otp:
                return None

        # Financial operations MUST require tool_call_id for unique identity.
        if not tool_call_id:
            return {
                "action": "block",
                "message": "Unique tool-call identity is unavailable.",
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


def _build_approval_message(tool_name: str, args: dict[str, Any]) -> str:
    """Build informative sanitized approval message.

    Includes: URL, method, caller max, configured cap, amount, network,
    method, expiry.
    Never includes: body, OTP, credentials, payment headers.
    """
    if tool_name == "x402_pay":
        url = args.get("url", "unknown")
        method = args.get("method", "GET")
        max_usdc = args.get("max_usdc", "no cap")
        # Get configured cap from runtime
        try:
            from hermes_x402.hermes_plugin.runtime import get_runtime

            runtime = get_runtime()
            runtime.ensure_initialized()
            configured_cap = (
                runtime.config.max_usdc_per_payment
                if runtime.config and runtime.config.max_usdc_per_payment
                else "default"
            )
        except Exception:
            configured_cap = "default"
        return (
            f"Pay for {url} via {method}. "
            f"Caller max: {max_usdc} USDC. Configured cap: {configured_cap} USDC."
        )

    if tool_name == "x402_gateway_deposit_execute":
        preview_id = args.get("preview_id", "unknown")
        # Try to load preview details for informative message
        try:
            from hermes_x402.hermes_plugin.runtime import get_runtime

            runtime = get_runtime()
            runtime.ensure_initialized()
            # Preview is not available here, but we can provide basic info
        except Exception:
            pass
        return f"Execute Gateway deposit via preview {preview_id}. This may transfer USDC."

    if tool_name == "x402_login_complete":
        return "Complete Circle login with OTP via chat (OTP exposed in conversation)."

    return f"Execute {tool_name}"
