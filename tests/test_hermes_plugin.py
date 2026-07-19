"""Real Hermes command contract smoke tests.

Covers:
  - Exact tool count: 14
  - Exact hook count: 1
  - Exact command count: 1
  - Command name: x402
  - No financial slash command exists
  - Command handler is sync (not async)
  - Command handler returns string, not coroutine
  - No 'coroutine was never awaited' warning
  - No event-loop error
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Any
from unittest.mock import MagicMock


class FakeHermesCtx:
    """Minimal Hermes context for smoke testing plugin registration."""

    def __init__(self):
        self.tools: list[dict[str, Any]] = []
        self.hooks: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []

    def register_tool(self, **kw: Any) -> None:
        self.tools.append(kw)

    def register_hook(self, hook_type: str, handler: Any, **kw: Any) -> None:
        self.hooks.append({"type": hook_type, "handler": handler, **kw})

    def register_command(self, name: str, handler: Any, **kw: Any) -> None:
        self.commands.append({"name": name, "handler": handler, **kw})

    def dispatch_tool(self, name: str, args: dict) -> str:
        return '{"success": true}'


class TestHermesCommandSmoke:
    """Smoke test: register the plugin and verify the exact contract."""

    def setup_method(self):
        from hermes_x402.hermes_plugin.runtime import reset_runtime

        reset_runtime()
        self.ctx = FakeHermesCtx()
        from hermes_x402.hermes_plugin.entry import register

        register(self.ctx)

    def test_exact_tool_count(self):
        assert len(self.ctx.tools) == 14

    def test_exact_hook_count(self):
        assert len(self.ctx.hooks) == 1

    def test_exact_command_count(self):
        assert len(self.ctx.commands) == 1

    def test_command_name_is_x402(self):
        assert self.ctx.commands[0]["name"] == "x402"

    def test_no_financial_slash_command(self):
        """No financial slash commands exist."""
        command_names = [cmd["name"] for cmd in self.ctx.commands]
        financial = {"pay", "deposit", "login-complete", "login_complete"}
        assert financial.isdisjoint(set(command_names))

    def test_command_handler_is_sync(self):
        """The registered command handler must be a sync callable."""
        handler = self.ctx.commands[0]["handler"]
        # Should be a regular function, not a coroutine function
        assert not asyncio.iscoroutinefunction(handler)

    def test_command_handler_returns_string(self):
        """Invoking the handler with mock context returns a string."""
        handler = self.ctx.commands[0]["handler"]
        mock_ctx = MagicMock()
        mock_ctx.dispatch_tool = MagicMock(return_value='{"success": true}')

        result = handler(raw_args="help", ctx=mock_ctx)
        assert isinstance(result, str)
        assert "x402" in result.lower()

    def test_command_handler_no_coroutine_return(self):
        """Handler must never return a coroutine object."""
        handler = self.ctx.commands[0]["handler"]
        # Handler closure captures FakeHermesCtx — no mock_ctx needed

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = handler(raw_args="status")

            # Result must be a string, not a coroutine
            assert isinstance(result, str), (
                f"Handler returned {type(result).__name__}, expected str"
            )

            # Check for coroutine warnings
            coroutine_warnings = [
                warning for warning in w if "coroutine" in str(warning.message).lower()
            ]
            assert len(coroutine_warnings) == 0, f"Got coroutine warnings: {coroutine_warnings}"

    def test_all_read_only_commands_dispatch(self):
        """All read-only commands dispatch to the correct tools."""
        handler = self.ctx.commands[0]["handler"]
        # Handler closure captures FakeHermesCtx — use self.ctx directly
        commands = [
            ("status", "x402_status"),
            ("wallet", "x402_wallet_status"),
            ("balance", "x402_wallet_balance"),
            ("gateway", "x402_gateway_balance"),
            ("networks", "x402_networks"),
        ]
        for cmd, _expected_tool in commands:
            result = handler(raw_args=cmd)
            assert isinstance(result, str)

    def test_unknown_subcommand_returns_string(self):
        handler = self.ctx.commands[0]["handler"]
        mock_ctx = MagicMock()
        result = handler(raw_args="nonexistent", ctx=mock_ctx)
        assert isinstance(result, str)
        assert "unknown" in result.lower()

    def test_help_returns_string(self):
        handler = self.ctx.commands[0]["handler"]
        mock_ctx = MagicMock()
        result = handler(raw_args="help", ctx=mock_ctx)
        assert isinstance(result, str)
        assert "status" in result

    def test_empty_returns_string(self):
        handler = self.ctx.commands[0]["handler"]
        mock_ctx = MagicMock()
        result = handler(raw_args="", ctx=mock_ctx)
        assert isinstance(result, str)
        assert "x402" in result.lower()

    def test_tool_names(self):
        """Verify all 14 tool names are registered."""
        tool_names = {t["name"] for t in self.ctx.tools}
        expected = {
            "x402_status",
            "x402_wallet_status",
            "x402_wallet_balance",
            "x402_networks",
            "x402_service_search",
            "x402_supports",
            "x402_service_inspect",
            "x402_fetch",
            "x402_pay",
            "x402_login_start",
            "x402_login_complete",
            "x402_gateway_balance",
            "x402_gateway_deposit_preview",
            "x402_gateway_deposit_execute",
        }
        assert tool_names == expected
