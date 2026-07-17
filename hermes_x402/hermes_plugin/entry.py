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

    register_status_tools(ctx)
    register_wallet_tools(ctx)
    register_network_tools(ctx)
    register_discovery_tools(ctx)
    register_supports_tools(ctx)
    register_service_tools(ctx)
    register_payment_tools(ctx)
    register_login_tools(ctx)
    register_gateway_tools(ctx)

    # Register native approval hook
    _register_approval_hook(ctx)

    logger.debug("hermes-x402 plugin: registered x402 tools and approval hook")


def _register_approval_hook(ctx: Any) -> None:
    """Register a pre_tool_call hook for native Hermes approval.

    Requires user approval before executing:
    - x402_pay (may transfer USDC)
    - x402_gateway_deposit_execute (may transfer USDC)
    - x402_login_complete (OTP exposure when chat OTP is used)

    Uses unique per-tool-call rule keys for Hermes approval tracking.
    Fails closed when the Hermes approval API is unavailable.
    """
    if not hasattr(ctx, "register_hook"):
        logger.debug(
            "hermes-x402: Hermes approval API unavailable — "
            "pre_tool_call hook not registered. Approval bypassed."
        )
        return

    async def _approval_hook(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        """Pre-tool-call hook that requests native Hermes approval.

        Returns {"allow": True} to proceed or {"allow": False, "reason": "..."}
        to block execution.
        """
        if tool_name not in _APPROVAL_REQUIRED_TOOLS:
            return {"allow": True}

        # x402_login_complete: only require approval when chat OTP is used
        if tool_name == "x402_login_complete":
            from hermes_x402.hermes_plugin.runtime import get_runtime

            runtime = get_runtime()
            runtime.ensure_initialized()
            allow_chat_otp = runtime.config.allow_chat_otp if runtime.config else False
            if not allow_chat_otp:
                # Chat OTP is disabled — tool will return chat_otp_disabled error
                return {"allow": True}

        # Build human-readable description for each tool
        descriptions = {
            "x402_pay": "Pay for an x402 resource (may transfer USDC)",
            "x402_gateway_deposit_execute": "Execute Gateway deposit (may transfer USDC)",
            "x402_login_complete": (
                "Complete Circle login with OTP via chat (OTP exposed in conversation)"
            ),
        }

        rule_key = f"x402_approval:{tool_name}"
        description = descriptions.get(tool_name, f"Execute {tool_name}")

        try:
            result = await ctx.request_approval(
                rule_key=rule_key,
                tool_name=tool_name,
                description=description,
                tool_args=tool_args,
            )
            if result and result.get("approved"):
                return {"allow": True}
            return {
                "allow": False,
                "reason": f"User denied {tool_name} execution",
            }
        except Exception:
            # Fail closed when approval API is unavailable
            logger.warning(
                "hermes-x402: Approval API unavailable for %s — blocking execution",
                tool_name,
            )
            return {
                "allow": False,
                "reason": f"Approval API unavailable for {tool_name}",
            }

    try:
        ctx.register_hook(
            hook_type="pre_tool_call",
            handler=_approval_hook,
        )
        logger.debug("hermes-x402: registered pre_tool_call approval hook")
    except Exception as exc:
        logger.warning("hermes-x402: failed to register approval hook: %s", exc)
