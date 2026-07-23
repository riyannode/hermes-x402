"""Tests for /x402 slash command presentation layer.

Covers:
  - Wallet field aliases (wallet vs wallet_address)
  - Gateway readiness alias (ready_for_payment)
  - Wallet status field aliases (on_chain_usdc_balance, buyer_runtime_ready, etc.)
  - Blockers normalization (dict → list)
  - Networks filters (active, buyer, gateway, all)
  - Active Arc Testnet marker with alias resolution
  - Concurrent command guard
  - Financial model preservation
  - Malformed JSON fails safely
  - Human-readable output format
  - No raw JSON dump
  - No full wallet/email exposure
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from hermes_x402.hermes_plugin.formatters import (
    format_configure,
    format_gateway_balance,
    format_networks,
    format_status,
    format_supports,
    format_wallet_balance,
    format_wallet_status,
)
from hermes_x402.hermes_plugin.slash_command import (
    _preview_store,
    handle_x402_command,
)

VALID_WALLET = "0xabababababababababababababababababababab"

SAMPLE_NETWORKS = [
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
    {
        "key": "arcTestnet",
        "display_name": "Arc Testnet",
        "environment": "testnet",
        "buyer_cli_supported": True,
        "gateway_supported": True,
        "caip2": "eip155:5042002",
    },
    {
        "key": "baseSepolia",
        "display_name": "Base Sepolia",
        "environment": "testnet",
        "buyer_cli_supported": False,
        "gateway_supported": False,
    },
]


@pytest.fixture(autouse=True)
def _clear():
    _preview_store.clear()
    yield
    _preview_store.clear()


def _ctx(tool_response: str) -> MagicMock:
    ctx = MagicMock()
    ctx.dispatch_tool = MagicMock(return_value=tool_response)
    return ctx


# ---------------------------------------------------------------------------
# 1. Wallet field aliases
# ---------------------------------------------------------------------------


class TestWalletFieldAliases:
    def test_wallet_balance_accepts_wallet_key(self):
        raw = json.dumps(
            {
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "balance": "11.39",
                "balances": [],
            }
        )
        result = format_wallet_balance(raw)
        assert "..." in result or "***" in result
        assert "Not set" not in result

    def test_wallet_balance_accepts_wallet_address_key(self):
        raw = json.dumps(
            {
                "wallet_address": VALID_WALLET,
                "network": "ARC-TESTNET",
                "balance": "5.0",
                "balances": [],
            }
        )
        result = format_wallet_balance(raw)
        assert "..." in result or "***" in result
        assert "Not set" not in result

    def test_gateway_balance_accepts_wallet_key(self):
        raw = json.dumps(
            {
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "total_usdc": "1.489947",
                "ready_for_payment": True,
            }
        )
        result = format_gateway_balance(raw)
        assert "..." in result or "***" in result
        assert "Not set" not in result

    def test_gateway_readiness_alias(self):
        raw = json.dumps(
            {
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "total_usdc": "1.0",
                "ready_for_payment": True,
            }
        )
        result = format_gateway_balance(raw)
        assert "Payment ready: Yes" in result

    def test_gateway_readiness_fallback(self):
        raw = json.dumps(
            {
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "total_usdc": "1.0",
                "payment_ready": True,
            }
        )
        result = format_gateway_balance(raw)
        assert "Payment ready: Yes" in result

    def test_wallet_balance_flat_format(self):
        raw = json.dumps(
            {
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "balance": "11.39",
            }
        )
        result = format_wallet_balance(raw)
        assert "USDC: 11.39" in result


# ---------------------------------------------------------------------------
# 2. Wallet-status field aliases
# ---------------------------------------------------------------------------


class TestWalletStatusAliases:
    def _raw(self, **overrides) -> str:
        base = {
            "wallet": VALID_WALLET,
            "network": "ARC-TESTNET",
            "session_valid": False,
            "session_environment": "unknown",
            "terms_accepted": False,
        }
        base.update(overrides)
        return json.dumps(base)

    def test_on_chain_usdc_balance_alias(self):
        result = format_wallet_status(self._raw(on_chain_usdc_balance="5.0"))
        assert "On-chain balance: 5.0 USDC" in result

    def test_on_chain_balance_fallback(self):
        result = format_wallet_status(self._raw(on_chain_balance="3.0"))
        assert "On-chain balance: 3.0 USDC" in result

    def test_gateway_usdc_balance_alias(self):
        result = format_wallet_status(self._raw(gateway_usdc_balance="2.0"))
        assert "Gateway balance: 2.0 USDC" in result

    def test_buyer_runtime_ready_alias(self):
        result = format_wallet_status(self._raw(buyer_runtime_ready=True))
        assert "Buyer runtime: Ready" in result

    def test_buyer_ready_fallback(self):
        result = format_wallet_status(self._raw(buyer_ready=True))
        assert "Buyer runtime: Ready" in result

    def test_next_tool_alias(self):
        result = format_wallet_status(self._raw(next_tool="x402_login_start"))
        assert "Next action: x402_login_start" in result

    def test_next_action_fallback(self):
        result = format_wallet_status(self._raw(next_action="configure"))
        assert "Next action: configure" in result

    def test_blockers_dict_normalized(self):
        result = format_wallet_status(
            self._raw(
                blockers={
                    "buyer": ["missing wallet"],
                    "gateway": [],
                }
            )
        )
        assert "Blockers:" in result
        assert "buyer: missing wallet" in result

    def test_blockers_list_unchanged(self):
        result = format_wallet_status(self._raw(blockers=["some issue"]))
        assert "Blockers:" in result
        assert "some issue" in result


# ---------------------------------------------------------------------------
# 3. Networks filters
# ---------------------------------------------------------------------------


class TestNetworksFilters:
    def _raw(self, networks=None, active="arcTestnet"):
        return json.dumps(
            {
                "success": True,
                "networks": networks or SAMPLE_NETWORKS,
                "active_network": active,
            }
        )

    def test_default_shows_all(self):
        result = format_networks(self._raw())
        assert "Base" in result
        assert "Ethereum" in result
        assert "Arc Testnet" in result
        assert "Base Sepolia" in result

    def test_active_filter(self):
        result = format_networks(self._raw(), "active")
        assert "Active Network" in result
        assert "Arc Testnet" in result
        assert "CAIP-2: eip155:5042002" in result
        # Must NOT show other networks
        assert "Base Sepolia" not in result

    def test_buyer_filter(self):
        result = format_networks(self._raw(), "buyer")
        assert "buyer-supported" in result
        assert "Base" in result
        assert "Arc Testnet" in result
        # Ethereum not buyer-supported
        assert "Ethereum" not in result
        assert "Base Sepolia" not in result

    def test_gateway_filter(self):
        result = format_networks(self._raw(), "gateway")
        assert "gateway-supported" in result
        assert "Base" in result
        assert "Ethereum" in result
        assert "Arc Testnet" in result
        # Base Sepolia not gateway-supported
        assert "Base Sepolia" not in result

    def test_all_filter(self):
        result = format_networks(self._raw(), "all")
        assert "(all)" in result
        assert "Base" in result
        assert "Ethereum" in result
        assert "Arc Testnet" in result
        assert "Base Sepolia" in result

    def test_active_arc_testnet_marker(self):
        result = format_networks(self._raw())
        assert "Active: Arc Testnet" in result
        # Testnet list should have ← marker
        assert "Arc Testnet ←" in result

    def test_active_alias_arc_testnet(self):
        """ARC-TESTNET alias resolves to Arc Testnet."""
        result = format_networks(self._raw(active="ARC-TESTNET"))
        assert "Active: Arc Testnet" in result

    def test_active_alias_eip155(self):
        """CAIP-2 alias resolves to Arc Testnet."""
        result = format_networks(self._raw(active="eip155:5042002"))
        assert "Active: Arc Testnet" in result


# ---------------------------------------------------------------------------
# 4. Concurrent command guard
# ---------------------------------------------------------------------------


class TestConcurrentGuard:
    def test_second_concurrent_wallet_rejected(self):
        from hermes_x402.hermes_plugin.slash_command import (
            _acquire_command_guard,
            _release_command_guard,
        )

        _release_command_guard()  # ensure clean state
        err = _acquire_command_guard("wallet")
        assert err is None
        err = _acquire_command_guard("balance")
        assert err is not None and "already running" in err
        _release_command_guard()

    def test_guard_clears_after_exception(self):
        from hermes_x402.hermes_plugin.slash_command import (
            _acquire_command_guard,
            _release_command_guard,
        )

        _release_command_guard()
        _acquire_command_guard("wallet")
        _release_command_guard()
        # Should be able to acquire again
        err = _acquire_command_guard("wallet")
        assert err is None
        _release_command_guard()

    def test_status_not_guarded(self):
        ctx = _ctx(
            json.dumps(
                {
                    "success": True,
                    "role": "buyer",
                    "backend": "cli",
                    "version": "0.2.1",
                    "wallet_address": VALID_WALLET,
                    "network": "ARC-TESTNET",
                    "configured": True,
                    "available": True,
                }
            )
        )
        result = handle_x402_command("status", ctx)
        assert "x402 Status" in result


# ---------------------------------------------------------------------------
# 5. Financial model preservation
# ---------------------------------------------------------------------------


class TestFinancialModel:
    def test_pay_unavailable(self):
        ctx = _ctx("{}")
        result = handle_x402_command("pay", ctx)
        assert "unknown" in result.lower()

    def test_deposit_unavailable(self):
        ctx = _ctx("{}")
        result = handle_x402_command("deposit", ctx)
        assert "unknown" in result.lower()

    def test_login_complete_unavailable(self):
        ctx = _ctx("{}")
        result = handle_x402_command("login-complete", ctx)
        assert "unknown" in result.lower()

    def test_help_mentions_agent_tools(self):
        result = handle_x402_command("help", _ctx(""))
        assert "agent tools" in result.lower()


# ---------------------------------------------------------------------------
# 6. Human-readable output
# ---------------------------------------------------------------------------


class TestHumanReadable:
    def test_status_format(self):
        raw = json.dumps(
            {
                "success": True,
                "role": "buyer",
                "backend": "cli",
                "version": "0.2.1",
                "wallet_address": VALID_WALLET,
                "network": "ARC-TESTNET",
                "configured": True,
                "available": True,
                "max_usdc_per_payment": "0.10",
                "host_allowlist": [],
            }
        )
        result = format_status(raw)
        assert "**x402 Status**" in result
        assert "hermes-x402 v0.2.1" in result
        assert "Circle CLI" in result
        assert "Arc Testnet" in result
        assert "Status: Ready" in result
        assert '{"' not in result  # no raw JSON

    def test_wallet_format(self):
        raw = json.dumps(
            {
                "success": True,
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "backend": "cli",
                "configured": True,
                "session_valid": True,
                "session_environment": "testnet",
                "terms_accepted": True,
                "cli_version": "0.0.6",
            }
        )
        result = format_wallet_status(raw)
        assert "**Circle Wallet**" in result
        assert "Active (testnet)" in result
        assert "Accepted" in result

    def test_balance_dedup(self):
        raw = json.dumps(
            {
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "balances": [
                    {"symbol": "USDC", "amount": "1.0", "token_address": "0xabc"},
                    {"symbol": "USDC", "amount": "2.0", "token_address": "0xabc"},
                ],
            }
        )
        result = format_wallet_balance(raw)
        assert result.count("USDC") == 1

    def test_gateway_format(self):
        raw = json.dumps(
            {
                "wallet": VALID_WALLET,
                "network": "ARC-TESTNET",
                "total_usdc": "1.489947",
                "ready_for_payment": True,
            }
        )
        result = format_gateway_balance(raw)
        assert "**Gateway Balance**" in result
        assert "1.489947 USDC" in result
        assert "Payment ready: Yes" in result

    def test_supports_format(self):
        raw = json.dumps(
            {
                "url": "https://api.example.com",
                "supported": True,
                "gateway_batching": True,
                "x402_version": "2",
            }
        )
        result = format_supports(raw)
        assert "**x402 Support Check**" in result
        assert "Supported" in result

    def test_malformed_json_safe(self):
        assert "unavailable" in format_status("not json").lower()
        assert "unavailable" in format_wallet_balance("{broken").lower()
        assert "unavailable" in format_gateway_balance("<>").lower()

    def test_multiline_rejected(self):
        ctx = _ctx("{}")
        result = handle_x402_command("status\nwallet", ctx)
        assert "one /x402 command per message" in result
        ctx.dispatch_tool.assert_not_called()

    def test_configure_hides_path(self):
        managed = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": VALID_WALLET,
            "CIRCLE_AGENT_WALLET_NETWORK": "ARC-TESTNET",
            "X402_MAX_USDC_PER_PAYMENT": "0.10",
        }
        cli_info = {"available": True, "version": "0.0.6", "executable": "/usr/bin/circle"}
        result = format_configure(managed, cli_info)
        assert "/usr/bin/circle" not in result
        assert "Circle CLI: Available" in result
