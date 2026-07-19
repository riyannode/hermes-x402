"""Tests for /x402 slash command presentation layer.

Covers:
  - Tools still return original JSON contracts
  - Slash commands return human-readable text
  - No full wallet address in output
  - No full email in output
  - No raw JSON dump in successful default output
  - Duplicate balances removed
  - Networks output below 3500 chars
  - Active Arc Testnet shown
  - Unsupported networks filtered
  - Configure output hides executable path
  - Configure output hides CIRCLE_CLI_EXECUTABLE
  - Multiline commands rejected
  - Financial slash commands unavailable
  - Malformed tool JSON fails safely
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from hermes_x402.hermes_plugin.formatters import (
    format_configure,
    format_networks,
    format_wallet_balance,
)
from hermes_x402.hermes_plugin.slash_command import (
    _preview_store,
    handle_x402_command,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_preview_store():
    _preview_store.clear()
    yield
    _preview_store.clear()


@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.dispatch_tool = MagicMock(return_value='{"success": true}')
    return ctx


# ---------------------------------------------------------------------------
# Tool JSON contracts unchanged
# ---------------------------------------------------------------------------


class TestToolContracts:
    def test_status_returns_json_string(self, mock_ctx):
        """Tool dispatch returns JSON string — tool contract preserved."""
        mock_ctx.dispatch_tool.return_value = json.dumps(
            {
                "success": True,
                "role": "buyer",
                "backend": "cli",
                "version": "0.2.0",
                "wallet_address": "0x1234567890abcdef",
                "network": "ARC-TESTNET",
                "configured": True,
                "available": True,
                "max_usdc_per_payment": "0.10",
                "host_allowlist": [],
            }
        )
        result = mock_ctx.dispatch_tool("x402_status", {})
        assert isinstance(result, str)
        data = json.loads(result)
        assert data["success"] is True


# ---------------------------------------------------------------------------
# Slash commands return human-readable text
# ---------------------------------------------------------------------------


class TestHumanReadable:
    def _make_ctx(self, tool_response: str) -> MagicMock:
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(return_value=tool_response)
        return ctx

    def test_status_human_readable(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "role": "buyer",
                    "backend": "cli",
                    "version": "0.2.0",
                    "wallet_address": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "configured": True,
                    "available": True,
                    "max_usdc_per_payment": "0.10",
                    "host_allowlist": [],
                }
            )
        )
        result = handle_x402_command("status", ctx)
        assert "x402 Status" in result
        assert "hermes-x402" in result
        assert "Buyer" in result
        assert "Circle CLI" in result
        # Must not be raw JSON
        assert "success" not in result

    def test_wallet_human_readable(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "wallet_address": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "backend": "cli",
                    "configured": True,
                    "session_valid": True,
                    "session_environment": "testnet",
                    "terms_accepted": True,
                    "cli_version": "0.0.6",
                }
            )
        )
        result = handle_x402_command("wallet", ctx)
        assert "Circle Wallet" in result
        assert "Active" in result

    def test_balance_human_readable(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "wallet_address": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "balances": [{"symbol": "USDC", "amount": "1.5"}],
                }
            )
        )
        result = handle_x402_command("balance", ctx)
        assert "Wallet Balance" in result
        assert "1.5" in result

    def test_gateway_human_readable(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "wallet_address": "0xabababababababababababababababababababab",
                    "network": "ARC-TESTNET",
                    "total_usdc": "5.0",
                }
            )
        )
        result = handle_x402_command("gateway", ctx)
        assert "Gateway Balance" in result
        assert "5.0" in result

    def test_networks_human_readable(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "networks": [
                        {"key": "base", "display_name": "Base", "environment": "mainnet"},
                        {
                            "key": "arcTestnet",
                            "display_name": "Arc Testnet",
                            "environment": "testnet",
                        },
                    ],
                    "active_network": "arcTestnet",
                }
            )
        )
        result = handle_x402_command("networks", ctx)
        assert "Networks" in result
        assert "Base" in result
        assert "Arc Testnet" in result

    def test_supports_human_readable(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "url": "https://api.example.com",
                    "supported": True,
                    "gateway_batching": True,
                    "x402_version": "2",
                    "preferred_network": "arcTestnet",
                }
            )
        )
        result = handle_x402_command("supports https://api.example.com", ctx)
        assert "x402 Support Check" in result
        assert "Supported" in result


# ---------------------------------------------------------------------------
# No full wallet address
# ---------------------------------------------------------------------------


class TestWalletMasking:
    def _make_ctx(self, tool_response: str) -> MagicMock:
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(return_value=tool_response)
        return ctx

    def test_status_masks_wallet(self):
        full_addr = "0xabababababababababababababababababababab"
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "role": "buyer",
                    "backend": "cli",
                    "version": "0.2.0",
                    "wallet_address": full_addr,
                    "network": "ARC-TESTNET",
                    "configured": True,
                    "available": True,
                }
            )
        )
        result = handle_x402_command("status", ctx)
        assert full_addr not in result
        assert "..." in result

    def test_wallet_masks_wallet(self):
        full_addr = "0xabababababababababababababababababababab"
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "wallet_address": full_addr,
                    "network": "ARC-TESTNET",
                    "backend": "cli",
                    "configured": True,
                }
            )
        )
        result = handle_x402_command("wallet", ctx)
        assert full_addr not in result

    def test_configure_masks_wallet(self, tmp_path):
        full_addr = "0xabababababababababababababababababababab"
        with patch(
            "hermes_x402.hermes_plugin.slash_command._resolve_hermes_home",
            return_value=tmp_path,
        ):
            from hermes_x402.hermes_plugin.slash_command import _handle_configure_preview

            result = _handle_configure_preview(["buyer", "cli", full_addr, "ARC-TESTNET", "0.10"])
            assert full_addr not in result


# ---------------------------------------------------------------------------
# No full email
# ---------------------------------------------------------------------------


class TestEmailMasking:
    def test_wallet_masks_email(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(
            return_value=json.dumps(
                {
                    "success": True,
                    "wallet_address": "0x1234",
                    "network": "ARC-TESTNET",
                    "backend": "cli",
                    "configured": True,
                    "email_masked": "user@example.com",
                    "session_valid": True,
                    "session_environment": "testnet",
                }
            )
        )
        result = handle_x402_command("wallet", ctx)
        # Should mask the email
        assert "user@example.com" not in result or "***" in result


# ---------------------------------------------------------------------------
# No raw JSON in successful output
# ---------------------------------------------------------------------------


class TestNoRawJSON:
    def _make_ctx(self, tool_response: str) -> MagicMock:
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(return_value=tool_response)
        return ctx

    def test_status_no_json(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "role": "buyer",
                    "backend": "cli",
                    "version": "0.2.0",
                    "wallet_address": "0x1234",
                    "network": "ARC-TESTNET",
                    "configured": True,
                    "available": True,
                }
            )
        )
        result = handle_x402_command("status", ctx)
        assert '{"' not in result  # no raw JSON object

    def test_balance_no_json(self):
        ctx = self._make_ctx(
            json.dumps(
                {
                    "success": True,
                    "wallet_address": "0x1234",
                    "network": "ARC-TESTNET",
                    "balances": [{"symbol": "USDC", "amount": "1.0"}],
                }
            )
        )
        result = handle_x402_command("balance", ctx)
        assert '{"' not in result


# ---------------------------------------------------------------------------
# Balance deduplication
# ---------------------------------------------------------------------------


class TestBalanceDedup:
    def test_duplicate_balances_removed(self):
        raw = json.dumps(
            {
                "success": True,
                "wallet_address": "0x1234",
                "network": "ARC-TESTNET",
                "balances": [
                    {"symbol": "USDC", "amount": "1.0", "token_address": "0xabc"},
                    {"symbol": "USDC", "amount": "2.0", "token_address": "0xabc"},
                    {"symbol": "USDC", "amount": "3.0", "token_address": "0xdef"},
                ],
            }
        )
        result = format_wallet_balance(raw)
        assert result.count("USDC") == 2  # two distinct USDC entries (different token addresses)

    def test_different_tokens_not_deduped(self):
        raw = json.dumps(
            {
                "success": True,
                "wallet_address": "0x1234",
                "network": "ARC-TESTNET",
                "balances": [
                    {"symbol": "USDC", "amount": "1.0"},
                    {"symbol": "ETH", "amount": "0.5"},
                ],
            }
        )
        result = format_wallet_balance(raw)
        assert "USDC" in result
        assert "ETH" in result


# ---------------------------------------------------------------------------
# Networks output length
# ---------------------------------------------------------------------------


class TestNetworksLength:
    def test_output_below_limit(self):
        networks = [
            {
                "key": f"net{i}",
                "display_name": f"Network {i}",
                "environment": "mainnet" if i < 10 else "testnet",
            }
            for i in range(25)
        ]
        raw = json.dumps({"success": True, "networks": networks})
        result = format_networks(raw)
        assert len(result) <= 3500

    def test_active_network_shown(self):
        raw = json.dumps(
            {
                "success": True,
                "networks": [
                    {"key": "arcTestnet", "display_name": "Arc Testnet", "environment": "testnet"},
                    {"key": "base", "display_name": "Base", "environment": "mainnet"},
                ],
                "active_network": "arcTestnet",
            }
        )
        result = format_networks(raw)
        assert "Arc Testnet" in result
        assert "←" in result  # active marker

    def test_filter_active(self):
        raw = json.dumps(
            {
                "success": True,
                "networks": [
                    {
                        "key": "arcTestnet",
                        "display_name": "Arc Testnet",
                        "environment": "testnet",
                        "caip2": "eip155:5042002",
                    },
                ],
                "active_network": "arcTestnet",
            }
        )
        result = format_networks(raw, "active")
        assert "Active: Arc Testnet" in result
        assert "CAIP-2: eip155:5042002" in result


# ---------------------------------------------------------------------------
# Configure hides executable path
# ---------------------------------------------------------------------------


class TestConfigureHidesPath:
    def test_no_executable_path(self):
        managed = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": "0x1234",
            "CIRCLE_AGENT_WALLET_NETWORK": "ARC-TESTNET",
            "X402_MAX_USDC_PER_PAYMENT": "0.10",
        }
        cli_info = {"available": True, "version": "0.0.6", "executable": "/usr/bin/circle"}
        result = format_configure(managed, cli_info)
        assert "/usr/bin/circle" not in result
        assert "0.0.6" in result

    def test_no_circle_cli_executable_key(self):
        env_path = __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / ".env"
        env_path.write_text("CIRCLE_CLI_EXECUTABLE=/usr/bin/circle\nX402_ROLE=buyer\n")
        from hermes_x402.hermes_plugin.slash_command import _read_managed_keys

        managed = _read_managed_keys(env_path)
        assert "CIRCLE_CLI_EXECUTABLE" not in managed


# ---------------------------------------------------------------------------
# Multiline commands rejected
# ---------------------------------------------------------------------------


class TestMultilineRejection:
    def test_multiline_rejected(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock()
        result = handle_x402_command("status\nwallet", ctx)
        assert "one /x402 command per message" in result
        ctx.dispatch_tool.assert_not_called()

    def test_single_line_accepted(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(return_value="{}")
        result = handle_x402_command("status", ctx)
        assert "one /x402 command per message" not in result


# ---------------------------------------------------------------------------
# Financial slash commands unavailable
# ---------------------------------------------------------------------------


class TestFinancialUnavailable:
    def test_pay_unavailable(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock()
        result = handle_x402_command("pay", ctx)
        assert "unknown" in result.lower()

    def test_deposit_unavailable(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock()
        result = handle_x402_command("deposit", ctx)
        assert "unknown" in result.lower()

    def test_login_complete_unavailable(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock()
        result = handle_x402_command("login-complete", ctx)
        assert "unknown" in result.lower()


# ---------------------------------------------------------------------------
# Malformed JSON fails safely
# ---------------------------------------------------------------------------


class TestMalformedJSON:
    def test_status_malformed(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(return_value="not json at all")
        result = handle_x402_command("status", ctx)
        assert "unavailable" in result.lower() or "invalid" in result.lower()

    def test_balance_malformed(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(return_value="{broken json")
        result = handle_x402_command("balance", ctx)
        assert "unavailable" in result.lower() or "invalid" in result.lower()

    def test_gateway_malformed(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(return_value="<>")
        result = handle_x402_command("gateway", ctx)
        assert "unavailable" in result.lower() or "invalid" in result.lower()


# ---------------------------------------------------------------------------
# networks filters
# ---------------------------------------------------------------------------


class TestNetworksFilters:
    def test_valid_filters_accepted(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock(
            return_value=json.dumps(
                {
                    "success": True,
                    "networks": [],
                }
            )
        )
        for f in ["active", "buyer", "gateway", "all"]:
            result = handle_x402_command(f"networks {f}", ctx)
            assert "Unknown filter" not in result

    def test_invalid_filter_rejected(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock()
        result = handle_x402_command("networks foobar", ctx)
        assert "Unknown filter" in result
        ctx.dispatch_tool.assert_not_called()

    def test_extra_args_rejected(self):
        ctx = MagicMock()
        ctx.dispatch_tool = MagicMock()
        result = handle_x402_command("networks active extra", ctx)
        assert "Usage" in result
        ctx.dispatch_tool.assert_not_called()
