"""Deterministic tests for the /x402 slash command.

Covers:
  - One command registered (exact name x402)
  - Help and empty invocation
  - Each read-only mapping dispatches the correct tool exactly once
  - supports requires exactly one HTTPS URL argument
  - Unknown subcommand is rejected
  - No financial subcommands exist
  - configure is read-only
  - preview never writes
  - apply writes only managed keys
  - wallet validation
  - network validation
  - Decimal validation
  - restart_required=true
  - output masks wallet and does not expose env contents
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hermes_x402.hermes_plugin.slash_command import (
    _handle_configure_apply,
    _handle_configure_preview,
    _validate_configure_args,
    handle_x402_command,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runtime():
    from hermes_x402.hermes_plugin.runtime import reset_runtime

    reset_runtime()
    yield
    reset_runtime()


@pytest.fixture
def mock_ctx():
    """Create a mock Hermes context with dispatch_tool and register_command."""
    ctx = MagicMock()
    ctx.dispatch_tool = MagicMock(return_value='{"success": true}')
    ctx.register_command = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------


class TestCommandRegistration:
    def test_one_command_registered(self):
        """Exactly one command is registered."""
        from hermes_x402.hermes_plugin.entry import register

        class FakeCtx:
            def __init__(self):
                self.tools = []
                self.hooks = []
                self.commands = []

            def register_tool(self, **kw):
                self.tools.append(kw.get("name", ""))

            def register_hook(self, hook_type, handler, **kw):
                self.hooks.append(hook_type)

            def register_command(self, name, handler, **kw):
                self.commands.append(name)

        ctx = FakeCtx()
        register(ctx)
        assert len(ctx.commands) == 1

    def test_command_name_is_x402(self):
        """The registered command name is exactly 'x402'."""
        from hermes_x402.hermes_plugin.entry import register

        class FakeCtx:
            def __init__(self):
                self.tools = []
                self.hooks = []
                self.commands = []

            def register_tool(self, **kw):
                self.tools.append(kw.get("name", ""))

            def register_hook(self, hook_type, handler, **kw):
                self.hooks.append(hook_type)

            def register_command(self, name, handler, **kw):
                self.commands.append(name)

        ctx = FakeCtx()
        register(ctx)
        assert ctx.commands == ["x402"]


# ---------------------------------------------------------------------------
# Help and empty invocation
# ---------------------------------------------------------------------------


class TestHelpAndEmpty:
    def test_empty_invocation_shows_help(self, mock_ctx):
        result = handle_x402_command("", mock_ctx)
        assert "/x402" in result
        assert "help" in result.lower()
        assert "status" in result

    def test_help_subcommand(self, mock_ctx):
        result = handle_x402_command("help", mock_ctx)
        assert "/x402" in result
        assert "status" in result


# ---------------------------------------------------------------------------
# Read-only dispatch mappings
# ---------------------------------------------------------------------------


class TestReadOnlyDispatch:
    def test_status_dispatches_x402_status(self, mock_ctx):
        handle_x402_command("status", mock_ctx)
        mock_ctx.dispatch_tool.assert_called_once_with("x402_status", {})

    def test_wallet_dispatches_x402_wallet_status(self, mock_ctx):
        handle_x402_command("wallet", mock_ctx)
        mock_ctx.dispatch_tool.assert_called_once_with("x402_wallet_status", {})

    def test_balance_dispatches_x402_wallet_balance(self, mock_ctx):
        handle_x402_command("balance", mock_ctx)
        mock_ctx.dispatch_tool.assert_called_once_with("x402_wallet_balance", {})

    def test_gateway_dispatches_x402_gateway_balance(self, mock_ctx):
        handle_x402_command("gateway", mock_ctx)
        mock_ctx.dispatch_tool.assert_called_once_with("x402_gateway_balance", {})

    def test_networks_dispatches_x402_networks(self, mock_ctx):
        handle_x402_command("networks", mock_ctx)
        mock_ctx.dispatch_tool.assert_called_once_with("x402_networks", {})

    def test_supports_dispatches_x402_supports(self, mock_ctx):
        handle_x402_command("supports https://example.com/api", mock_ctx)
        mock_ctx.dispatch_tool.assert_called_once_with(
            "x402_supports", {"url": "https://example.com/api"}
        )


# ---------------------------------------------------------------------------
# supports validation
# ---------------------------------------------------------------------------


class TestSupportsValidation:
    def test_supports_requires_url(self, mock_ctx):
        result = handle_x402_command("supports", mock_ctx)
        assert "usage" in result.lower()
        mock_ctx.dispatch_tool.assert_not_called()

    def test_supports_requires_https(self, mock_ctx):
        result = handle_x402_command("supports http://example.com", mock_ctx)
        assert "https" in result.lower()
        mock_ctx.dispatch_tool.assert_not_called()


# ---------------------------------------------------------------------------
# Unknown subcommand
# ---------------------------------------------------------------------------


class TestUnknownSubcommand:
    def test_unknown_rejected(self, mock_ctx):
        result = handle_x402_command("foobar", mock_ctx)
        assert "unknown" in result.lower()


# ---------------------------------------------------------------------------
# No financial subcommands
# ---------------------------------------------------------------------------


class TestNoFinancialSubcommands:
    def test_no_pay_subcommand(self, mock_ctx):
        result = handle_x402_command("pay", mock_ctx)
        assert "unknown" in result.lower()

    def test_no_deposit_subcommand(self, mock_ctx):
        result = handle_x402_command("deposit", mock_ctx)
        assert "unknown" in result.lower()

    def test_no_login_complete_subcommand(self, mock_ctx):
        result = handle_x402_command("login-complete", mock_ctx)
        assert "unknown" in result.lower()


# ---------------------------------------------------------------------------
# Configure: read-only
# ---------------------------------------------------------------------------


class TestConfigureReadOnly:
    def test_configure_show_is_read_only(self, mock_ctx):
        """configure with no args shows state, doesn't dispatch any tool."""
        result = handle_x402_command("configure", mock_ctx)
        assert "configure" in result.lower() or "circle cli" in result.lower()
        mock_ctx.dispatch_tool.assert_not_called()


# ---------------------------------------------------------------------------
# Configure: preview never writes
# ---------------------------------------------------------------------------


class TestConfigurePreview:
    def test_preview_never_writes(self, tmp_path):
        """Preview returns proposed keys but never touches the filesystem."""
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=value\n")
        with patch(
            "hermes_x402.hermes_plugin.slash_command._resolve_hermes_home",
            return_value=tmp_path,
        ):
            result = _handle_configure_preview(
                ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
            )
            assert "preview" in result.lower() or "proposed" in result.lower()
            assert "no changes written" in result.lower()
            # File unchanged
            assert env_path.read_text() == "EXISTING=value\n"

    def test_preview_masks_wallet(self):
        result = _handle_configure_preview(
            ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
        )
        assert "0xabab" in result  # masked prefix
        assert "ab" * 20 not in result  # full address not shown


# ---------------------------------------------------------------------------
# Configure: apply writes only managed keys
# ---------------------------------------------------------------------------


class TestConfigureApply:
    def test_apply_writes_managed_keys(self, tmp_path):
        with (
            patch(
                "hermes_x402.hermes_plugin.slash_command._resolve_hermes_home",
                return_value=tmp_path,
            ),
            patch("shutil.which", return_value="/usr/local/bin/hermes"),
        ):
            result = _handle_configure_apply(
                ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
            )
            assert "applied" in result.lower()
            env_path = tmp_path / ".env"
            assert env_path.exists()
            content = env_path.read_text()
            assert "X402_ROLE=buyer" in content
            assert "X402_BUYER_BACKEND=cli" in content
            assert "CIRCLE_AGENT_WALLET_NETWORK=ARC-TESTNET" in content
            assert "X402_MAX_USDC_PER_PAYMENT=0.10" in content
            assert "X402_NETWORK_POLICY=public" in content
            assert "X402_REQUIRE_GATEWAY_BATCHING=true" in content
            assert "X402_ALLOW_HTTP=false" in content
            assert "X402_ALLOW_CHAT_OTP=false" in content

    def test_apply_masks_wallet_in_output(self, tmp_path):
        with (
            patch(
                "hermes_x402.hermes_plugin.slash_command._resolve_hermes_home",
                return_value=tmp_path,
            ),
            patch("shutil.which", return_value="/usr/local/bin/hermes"),
        ):
            result = _handle_configure_apply(
                ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
            )
            assert "0xabab" in result  # masked
            assert "ab" * 20 not in result  # not full address

    def test_apply_restart_required(self, tmp_path):
        with (
            patch(
                "hermes_x402.hermes_plugin.slash_command._resolve_hermes_home",
                return_value=tmp_path,
            ),
            patch("shutil.which", return_value="/usr/local/bin/hermes"),
        ):
            result = _handle_configure_apply(
                ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
            )
            assert "restart_required=true" in result
            assert "gateway restart" in result

    def test_apply_preserves_unrelated_vars(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# comment\nUNRELATED_VAR=keep\n")
        with (
            patch(
                "hermes_x402.hermes_plugin.slash_command._resolve_hermes_home",
                return_value=tmp_path,
            ),
            patch("shutil.which", return_value="/usr/local/bin/hermes"),
        ):
            _handle_configure_apply(["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"])
            content = env_path.read_text()
            assert "# comment" in content
            assert "UNRELATED_VAR=keep" in content


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestWalletValidation:
    def test_valid_wallet(self):
        params, err = _validate_configure_args(
            ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
        )
        assert err is None
        assert params is not None
        assert params["wallet"] == "0x" + "ab" * 20

    def test_invalid_wallet_short(self):
        _, err = _validate_configure_args(["buyer", "cli", "0xabc", "ARC-TESTNET", "0.10"])
        assert err is not None
        assert "wallet" in err.lower()

    def test_invalid_wallet_no_prefix(self):
        _, err = _validate_configure_args(["buyer", "cli", "ab" * 20, "ARC-TESTNET", "0.10"])
        assert err is not None
        assert "wallet" in err.lower()

    def test_invalid_wallet_non_hex(self):
        _, err = _validate_configure_args(["buyer", "cli", "0x" + "zz" * 20, "ARC-TESTNET", "0.10"])
        assert err is not None
        assert "wallet" in err.lower()


class TestNetworkValidation:
    def test_valid_network(self):
        params, err = _validate_configure_args(
            ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
        )
        assert err is None
        assert params is not None

    def test_invalid_network(self):
        _, err = _validate_configure_args(["buyer", "cli", "0x" + "ab" * 20, "BASE", "0.10"])
        assert err is not None
        assert "network" in err.lower()


class TestDecimalValidation:
    def test_valid_decimal(self):
        params, err = _validate_configure_args(
            ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
        )
        assert err is None
        assert params is not None
        assert params["max_usdc"] == "0.10"

    def test_invalid_decimal(self):
        _, err = _validate_configure_args(["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "abc"])
        assert err is not None
        assert "max_usdc" in err.lower()

    def test_negative_decimal(self):
        _, err = _validate_configure_args(
            ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "-0.10"]
        )
        assert err is not None
        assert "max_usdc" in err.lower()

    def test_zero_decimal(self):
        _, err = _validate_configure_args(["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0"])
        assert err is not None
        assert "max_usdc" in err.lower()

    def test_nan_decimal(self):
        _, err = _validate_configure_args(["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "NaN"])
        assert err is not None

    def test_infinity_decimal(self):
        _, err = _validate_configure_args(
            ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "Infinity"]
        )
        assert err is not None


class TestRoleBackendValidation:
    def test_only_buyer_role(self):
        _, err = _validate_configure_args(
            ["seller", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
        )
        assert err is not None
        assert "role" in err.lower()

    def test_only_cli_backend(self):
        _, err = _validate_configure_args(["buyer", "dcw", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"])
        assert err is not None
        assert "backend" in err.lower()

    def test_insufficient_args(self):
        _, err = _validate_configure_args(["buyer", "cli"])
        assert err is not None
        assert "usage" in err.lower()


# ---------------------------------------------------------------------------
# Env content safety
# ---------------------------------------------------------------------------


class TestEnvContentSafety:
    def test_output_does_not_expose_env_contents(self, tmp_path):
        """Apply output never dumps the full .env file."""
        env_path = tmp_path / ".env"
        env_path.write_text("SECRET_API_KEY=supersecretvalue\n")
        with (
            patch(
                "hermes_x402.hermes_plugin.slash_command._resolve_hermes_home",
                return_value=tmp_path,
            ),
            patch("shutil.which", return_value="/usr/local/bin/hermes"),
        ):
            result = _handle_configure_apply(
                ["buyer", "cli", "0x" + "ab" * 20, "ARC-TESTNET", "0.10"]
            )
            assert "supersecretvalue" not in result
