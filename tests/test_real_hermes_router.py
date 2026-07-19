"""Real Hermes router integration tests for the /x402 slash command.

These tests intentionally exercise Hermes' installed plugin registry and gateway
slash-command router instead of calling handle_x402_command() directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_HERMES_AGENT_ROOT = Path("/usr/local/lib/hermes-agent")
if _HERMES_AGENT_ROOT.exists():
    sys.path.insert(0, str(_HERMES_AGENT_ROOT))


class _Hooks:
    async def emit_collect(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


@pytest.fixture
def real_hermes_x402_command_registry(monkeypatch: pytest.MonkeyPatch):
    """Register x402 through Hermes' real PluginContext/PluginManager registry."""

    from hermes_cli.plugins import (
        PluginContext,
        PluginManifest,
        get_plugin_manager,
    )

    from hermes_x402.hermes_plugin.entry import register

    manager = get_plugin_manager()
    old_commands = dict(manager._plugin_commands)
    old_discovered = manager._discovered

    manifest = PluginManifest(
        name="hermes-x402-test",
        version="0.2.0",
        description="test x402 plugin registration",
        source="entrypoint",
        key="hermes-x402-test",
    )
    ctx = PluginContext(manifest=manifest, manager=manager)

    def dispatch_tool(tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "x402_status":
            return json.dumps(
                {
                    "success": True,
                    "role": "buyer",
                    "backend": "cli",
                    "version": "0.2.0",
                    "wallet": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "configured": True,
                    "available": True,
                }
            )
        if tool_name == "x402_wallet_status":
            return json.dumps(
                {
                    "success": True,
                    "wallet": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "backend": "cli",
                    "session_valid": True,
                    "session_environment": "testnet",
                    "terms_accepted": True,
                }
            )
        if tool_name == "x402_wallet_balance":
            return json.dumps(
                {
                    "success": True,
                    "wallet": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "balance": "1.0",
                }
            )
        if tool_name == "x402_gateway_balance":
            return json.dumps(
                {
                    "success": True,
                    "wallet": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "total_usdc": "1.0",
                    "ready_for_payment": True,
                }
            )
        if tool_name == "x402_networks":
            return json.dumps(
                {
                    "success": True,
                    "active_network": "ARC-TESTNET",
                    "networks": [
                        {
                            "key": "arcTestnet",
                            "display_name": "Arc Testnet",
                            "environment": "testnet",
                            "buyer_cli_supported": True,
                            "gateway_supported": True,
                            "caip2": "eip155:5042002",
                        },
                        {
                            "key": "base",
                            "display_name": "Base",
                            "environment": "mainnet",
                            "buyer_cli_supported": True,
                            "gateway_supported": True,
                        },
                        {
                            "key": "ethereum",
                            "display_name": "Ethereum",
                            "environment": "mainnet",
                            "buyer_cli_supported": False,
                            "gateway_supported": True,
                        },
                    ],
                }
            )
        if tool_name == "x402_supports":
            return json.dumps(
                {
                    "success": True,
                    "url": args["url"],
                    "supported": True,
                    "gateway_batching": True,
                    "x402_version": "2",
                }
            )
        raise AssertionError(f"Unexpected tool dispatch: {tool_name}")

    monkeypatch.setattr(ctx, "dispatch_tool", dispatch_tool)
    register(ctx)
    manager._discovered = True
    yield manager

    manager._plugin_commands.clear()
    manager._plugin_commands.update(old_commands)
    manager._discovered = old_discovered


def _real_gateway_runner() -> Any:
    """Build the minimum GatewayRunner instance needed to run _handle_message."""

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = type("Config", (), {"extra": {}})()
    runner.session_store = object()
    runner.hooks = _Hooks()
    runner._startup_restore_in_progress = False
    runner._update_prompt_pending = {}
    runner._draining = False
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda _source: "test:telegram:dm"
    runner._check_slash_access = lambda _source, _command: None
    runner._adapter_for_source = lambda _source: None
    return runner


async def _route_text(text: str) -> str | None:
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
        user_id="456",
        user_name="tester",
    )
    event = MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        source=source,
        message_id="1",
    )
    return await GatewayRunner._handle_message(_real_gateway_runner(), event)


@pytest.mark.usefixtures("real_hermes_x402_command_registry")
class TestRealHermesX402Router:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("/x402", "x402 Commands"),
            ("/x402 help", "x402 Commands"),
            ("/x402 status", "x402 Status"),
            ("/x402 wallet", "Circle Wallet"),
            ("/x402 balance", "Wallet Balance"),
            ("/x402 gateway", "Gateway Balance"),
            ("/x402 networks", "**Networks**"),
            ("/x402 networks active", "Active Network"),
            ("/x402 networks buyer", "buyer-supported"),
            ("/x402 networks gateway", "gateway-supported"),
            ("/x402 networks all", "all"),
            ("/x402 supports https://example.com", "x402 Support Check"),
            ("/x402 configure", "Configuration"),
        ],
    )
    async def test_argument_bearing_x402_commands_reach_handler(
        self, text: str, expected: str
    ) -> None:
        result = await _route_text(text)
        assert isinstance(result, str)
        assert expected in result
        assert "Unknown command `/x402`" not in result

    async def test_invalid_network_singular_reaches_x402_handler(self) -> None:
        result = await _route_text("/x402 network")
        assert isinstance(result, str)
        assert "Unknown subcommand: 'network'" in result
        assert "Unknown command `/x402`" not in result


def test_x402_command_args_hint_declares_argument_form(
    real_hermes_x402_command_registry: Any,
) -> None:
    entry = real_hermes_x402_command_registry._plugin_commands["x402"]
    assert (
        entry["args_hint"]
        == "[help|status|wallet|balance|gateway|networks|supports|configure] [args]"
    )
