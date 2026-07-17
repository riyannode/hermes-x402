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


def register(ctx: Any) -> None:
    """Register x402 tools with the Hermes plugin context.

    Split registration into focused groups for clarity. Each group
    registers related tools under the ``x402`` toolset.
    """
    from hermes_x402.hermes_plugin.tools import (
        register_discovery_tools,
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

    logger.debug("hermes-x402 plugin: registered x402 tools")
