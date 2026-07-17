"""Comprehensive tests for PR #6: Circle login recovery and Gateway funding.

Reduced scope from PR #5 — 14 tools total.

Covers:
- Section A: Public network policy defaults, SSRF, strict_allowlist
- Section B: Login start/complete lifecycle (v0.0.6), OTP lifecycle
- Section C: Gateway balance, deposit preview, deposit execute
- Section D: x402_wallet_status extended fields
- Section E: Entry-point dispatch, removed tool names absent
- Section F: Runner allowlist, model contracts, error mapping
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockCtx:
    """Minimal Hermes plugin context for tool registration testing."""

    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}

    def register_tool(self, **kwargs: Any) -> None:
        name = kwargs.get("name", "")
        self.tools[name] = kwargs


def _make_config(**overrides: Any) -> Any:
    from hermes_x402.config import X402Config

    defaults = {
        "role": "buyer",
        "buyer_backend": "cli",
        "circle_cli_wallet_address": "0xabcdef1234567890abcdef1234567890abcdef12",
        "circle_cli_network": "ARC-TESTNET",
        "max_usdc_per_payment": "10.0",
    }
    defaults.update(overrides)
    return X402Config(**defaults)


def _make_runtime_mock(
    *,
    configured: bool = True,
    available: bool = True,
    backend: str = "cli",
    role: str = "buyer",
    network: str = "ARC-TESTNET",
    wallet_address: str = "0xabcdef1234567890abcdef1234567890abcdef12",
    allow_chat_otp: bool = False,
) -> MagicMock:
    rt = MagicMock()
    rt.is_configured = configured
    rt.is_available = available
    rt.backend_name = backend
    rt.role = role
    rt.network = network
    rt.wallet_address = wallet_address
    rt.version = "0.1.0"
    rt.config = _make_config(
        role=role,
        buyer_backend=backend if role in {"buyer", "dual"} else None,
        circle_cli_network=network,
        circle_cli_wallet_address=wallet_address,
        host_allowlist=[],
        network_policy="public",
        require_approval_for_new_host=False,
        allow_http=False,
        allow_chat_otp=allow_chat_otp,
    )
    rt.cli_client = AsyncMock()
    rt.buyer_tool = MagicMock()
    rt.init_error = None
    return rt


# ===========================================================================
# SECTION A: Public network policy defaults and SSRF
# ===========================================================================


class TestPublicNetworkPolicyDefaults:
    """Default configuration uses public mode with empty allowlist."""

    def test_default_network_policy_is_public(self) -> None:
        from hermes_x402.config import X402Config

        config = X402Config()
        assert config.network_policy == "public"

    def test_default_host_allowlist_is_empty(self) -> None:
        from hermes_x402.config import X402Config

        config = X402Config()
        assert config.host_allowlist == []

    def test_default_require_approval_is_false(self) -> None:
        from hermes_x402.config import X402Config

        config = X402Config()
        assert config.require_approval_for_new_host is False

    def test_default_allow_http_is_false(self) -> None:
        from hermes_x402.config import X402Config

        config = X402Config()
        assert config.allow_http is False


class TestStrictAllowlistOptIn:
    """strict_allowlist mode blocks non-allowlisted hosts."""

    def test_empty_allowlist_blocks_all(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://example.com", [], mode="strict_allowlist")
        assert err is not None
        assert "empty allowlist" in err.lower()

    def test_allowlisted_host_allowed(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url(
            "https://api.example.com", ["example.com"], mode="strict_allowlist"
        )
        assert err is None

    def test_non_allowlisted_host_blocked(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://evil.com", ["example.com"], mode="strict_allowlist")
        assert err is not None
        assert "allowlist" in err.lower()


class TestSSRFProtection:
    """SSRF protections are always active."""

    def test_localhost_blocked(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://localhost/secret", [], mode="public")
        assert err is not None
        assert "blocked" in err.lower()

    def test_loopback_blocked(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://127.0.0.1/secret", [], mode="public")
        assert err is not None

    def test_metadata_endpoint_blocked(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://169.254.169.254/metadata", [], mode="public")
        assert err is not None

    def test_http_blocked_by_default(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("http://example.com", [], mode="public", allow_http=False)
        assert err is not None
        assert "http" in err.lower()

    def test_http_allowed_when_enabled(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("http://example.com", [], mode="public", allow_http=True)
        assert err is None

    def test_public_mode_empty_allowlist_allows_all(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://example.com", [], mode="public")
        assert err is None

    def test_redirect_not_followed(self) -> None:
        from hermes_x402.hermes_plugin.tools import _check_redirect

        mock_response = MagicMock()
        mock_response.is_redirect = True
        mock_response.status_code = 301
        mock_response.headers = {"location": "https://evil.com/secret"}
        result = _check_redirect(mock_response)
        assert result is not None
        assert result["error"] == "redirect_not_followed"


# ===========================================================================
# SECTION B: Login lifecycle (v0.0.6)
# ===========================================================================


class TestLoginLifecycle:
    """Login start/complete lifecycle."""

    def test_login_tools_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        assert "x402_login_start" in ctx.tools
        assert "x402_login_complete" in ctx.tools

    async def test_login_start_rejects_active_session(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        handler = ctx.tools["x402_login_start"]["handler"]

        rt = _make_runtime_mock()
        status = MagicMock()
        status.authenticated = True
        status.terms_accepted = True
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler({"email": "user@example.com"})

        import json

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "session_active"

    async def test_login_start_rejects_terms_required(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        handler = ctx.tools["x402_login_start"]["handler"]

        rt = _make_runtime_mock()
        status = MagicMock()
        status.authenticated = False
        status.terms_accepted = False
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler({"email": "user@example.com"})

        import json

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "terms_action_required"

    async def test_login_start_generates_opaque_login_id(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        handler = ctx.tools["x402_login_start"]["handler"]

        rt = _make_runtime_mock()
        status = MagicMock()
        status.authenticated = False
        status.terms_accepted = True
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)
        rt.cli_client.login_start = AsyncMock(
            return_value=MagicMock(
                request_id="circle-raw-request-id-123",
                email_masked="u***@example.com",
                otp_required=True,
            )
        )

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler({"email": "user@example.com"})

        import json

        data = json.loads(result)
        assert data["success"] is True
        assert "login_id" in data
        # login_id must NOT be the raw Circle request ID
        assert data["login_id"] != "circle-raw-request-id-123"
        assert data["email_masked"] == "u***@example.com"

    async def test_login_start_rejects_invalid_email(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        handler = ctx.tools["x402_login_start"]["handler"]

        rt = _make_runtime_mock()
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler({"email": "not-an-email"})

        import json

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "invalid_input"

    async def test_login_complete_rejects_invalid_id(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        handler = ctx.tools["x402_login_complete"]["handler"]

        rt = _make_runtime_mock(allow_chat_otp=True)
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler(
                {"login_id": "nonexistent", "otp": "123456", "acknowledge_otp_exposure": True}
            )

        import json

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "invalid_request"

    async def test_login_complete_rejects_empty_otp(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        handler = ctx.tools["x402_login_complete"]["handler"]

        rt = _make_runtime_mock(allow_chat_otp=True)
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler({"login_id": "", "otp": "", "acknowledge_otp_exposure": True})

        import json

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "invalid_input"

    async def test_consumed_otp_cannot_be_reused(self) -> None:
        """After a failed OTP, the pending login is consumed and cannot be reused."""
        from hermes_x402.hermes_plugin.tools import register_login_tools

        ctx = MockCtx()
        register_login_tools(ctx)
        start_handler = ctx.tools["x402_login_start"]["handler"]
        complete_handler = ctx.tools["x402_login_complete"]["handler"]

        rt = _make_runtime_mock(allow_chat_otp=True)
        status = MagicMock()
        status.authenticated = False
        status.terms_accepted = True
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)
        rt.cli_client.login_start = AsyncMock(
            return_value=MagicMock(
                request_id="circle-req-123",
                email_masked="u***@example.com",
                otp_required=True,
            )
        )

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            # Start login
            start_result = await start_handler({"email": "user@example.com"})
            import json

            start_data = json.loads(start_result)
            login_id = start_data["login_id"]

            # Simulate failed OTP (exception)
            rt.cli_client.login_complete = AsyncMock(side_effect=Exception("OTP invalid"))
            await complete_handler(
                {"login_id": login_id, "otp": "000000", "acknowledge_otp_exposure": True}
            )

            # Try reuse — should fail because pending login was consumed
            rt.cli_client.login_complete = AsyncMock(
                return_value=MagicMock(authenticated=True, testnet_status="VALID")
            )
            reuse_result = await complete_handler(
                {"login_id": login_id, "otp": "111111", "acknowledge_otp_exposure": True}
            )
            reuse_data = json.loads(reuse_result)
            assert reuse_data["success"] is False
            assert reuse_data["error"] == "invalid_request"


# ===========================================================================
# SECTION C: Gateway tools
# ===========================================================================


class TestGatewayBalance:
    """Gateway balance tool tests."""

    def test_gateway_balance_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        assert "x402_gateway_balance" in ctx.tools

    async def test_gateway_balance_decimal_comparison(self) -> None:
        """Gateway balance uses Decimal, not string comparison."""
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_balance"]["handler"]

        rt = _make_runtime_mock()
        gw_result = MagicMock()
        gw_result.total_usdc = "1.500000"
        gw_result.network = "ARC-TESTNET"
        gw_result.domain = 26
        rt.cli_client.gateway_balance = AsyncMock(return_value=gw_result)

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler()

        import json

        data = json.loads(result)
        assert data["success"] is True
        assert data["ready_for_payment"] is True

    async def test_gateway_balance_zero_not_ready(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_balance"]["handler"]

        rt = _make_runtime_mock()
        gw_result = MagicMock()
        gw_result.total_usdc = "0"
        gw_result.network = "ARC-TESTNET"
        gw_result.domain = 26
        rt.cli_client.gateway_balance = AsyncMock(return_value=gw_result)

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler()

        import json

        data = json.loads(result)
        assert data["success"] is True
        assert data["ready_for_payment"] is False


class TestGatewayDepositPreview:
    """Gateway deposit preview tool tests."""

    def test_preview_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        assert "x402_gateway_deposit_preview" in ctx.tools

    async def test_preview_rejects_non_positive_amount(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_preview"]["handler"]

        rt = _make_runtime_mock()
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler(
                {
                    "service_url": "https://api.example.com/premium",
                    "method": "GET",
                    "amount": "0",
                }
            )

        import json

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "invalid_input"

    async def test_preview_rejects_negative_amount(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_preview"]["handler"]

        rt = _make_runtime_mock()
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler(
                {
                    "service_url": "https://api.example.com/premium",
                    "method": "GET",
                    "amount": "-5.0",
                }
            )

        import json

        data = json.loads(result)
        assert data["success"] is False

    async def test_preview_rejects_insufficient_balance(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_preview"]["handler"]

        rt = _make_runtime_mock()
        # Mock valid session
        status = MagicMock()
        status.authenticated = True
        status.terms_accepted = True
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)

        # Mock insufficient wallet balance
        from hermes_x402.circle_cli.models import WalletBalance

        rt.cli_client.get_balance = AsyncMock(
            return_value=(WalletBalance(symbol="USDC", amount="1.0"),)
        )

        # Mock 402 response with Gateway option
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(
            {
                "paymentOptions": [
                    {"paymentSystem": "circle_gateway", "network": "ARC-TESTNET", "domain": 26}
                ]
            }
        )

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt),
            patch("hermes_x402.hermes_plugin.tools.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await handler(
                {
                    "service_url": "https://api.example.com/premium",
                    "method": "GET",
                    "amount": "5.0",
                }
            )

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "insufficient_balance"

    async def test_preview_requires_session(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_preview"]["handler"]

        rt = _make_runtime_mock()
        status = MagicMock()
        status.authenticated = False
        status.terms_accepted = True
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)

        # Mock 402 response with Gateway option
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(
            {
                "paymentOptions": [
                    {"paymentSystem": "circle_gateway", "network": "ARC-TESTNET", "domain": 26}
                ]
            }
        )

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt),
            patch("hermes_x402.hermes_plugin.tools.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await handler(
                {
                    "service_url": "https://api.example.com/premium",
                    "method": "GET",
                    "amount": "5.0",
                }
            )

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "session_invalid"


class TestGatewayDepositExecute:
    """Gateway deposit execute tool tests."""

    def test_execute_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        assert "x402_gateway_deposit_execute" in ctx.tools

    async def test_execute_rejects_invalid_preview(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_execute"]["handler"]

        rt = _make_runtime_mock()
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler({"preview_id": "nonexistent"})

        import json

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "invalid_preview"


# ===========================================================================
# SECTION D: x402_wallet_status extended fields
# ===========================================================================


class TestWalletStatusExtended:
    """x402_wallet_status reports extended readiness info."""

    def test_wallet_status_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        ctx = MockCtx()
        register_wallet_tools(ctx)
        assert "x402_wallet_status" in ctx.tools

    def test_wallet_status_when_not_configured(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        ctx = MockCtx()
        register_wallet_tools(ctx)
        handler = ctx.tools["x402_wallet_status"]["handler"]

        rt = _make_runtime_mock(configured=False)
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = asyncio.run(handler())

        import json

        data = json.loads(result)
        assert data["success"] is True
        assert data["configured"] is False

    def test_wallet_status_dcw_backend(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        ctx = MockCtx()
        register_wallet_tools(ctx)
        handler = ctx.tools["x402_wallet_status"]["handler"]

        rt = _make_runtime_mock(backend="dcw")
        rt.cli_client = None
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = asyncio.run(handler())

        import json

        data = json.loads(result)
        assert data["success"] is True
        assert data["backend"] == "dcw"


# ===========================================================================
# SECTION E: Entry-point dispatch and removed tools
# ===========================================================================


class TestEntryRegistration:
    """Verify entry point registers exactly 14 tools and no removed tools."""

    def test_exact_14_tools_registered(self) -> None:
        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
        assert len(ctx.tools) == 14

    def test_all_expected_tool_names(self) -> None:
        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
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
        assert set(ctx.tools.keys()) == expected

    def test_no_removed_tool_names(self) -> None:
        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
        removed = {
            "x402_session_status",
            "x402_logout",
            "x402_wallet_list",
            "x402_wallet_create",
            "x402_wallet_deploy",
            "x402_readiness",
        }
        for name in removed:
            assert name not in ctx.tools, f"Removed tool {name} is still registered"

    def test_no_duplicate_names(self) -> None:
        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
        # Each tool name should be unique (no double registration)
        assert len(ctx.tools) == 14

    def test_every_handler_returns_json_string(self) -> None:
        """Every tool handler returns a JSON-decodable string."""

        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
        for name, tool in ctx.tools.items():
            handler = tool["handler"]
            assert callable(handler), f"{name} handler is not callable"

    def test_wallet_status_mentions_read_only(self) -> None:
        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
        desc = ctx.tools["x402_wallet_status"]["description"].lower()
        assert "read-only" in desc

    def test_login_start_mentions_expiry(self) -> None:
        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
        desc = ctx.tools["x402_login_start"]["description"].lower()
        assert "expiry" in desc

    def test_gateway_deposit_execute_mentions_retry_safe(self) -> None:
        from hermes_x402.hermes_plugin.entry import register

        ctx = MockCtx()
        register(ctx)
        desc = ctx.tools["x402_gateway_deposit_execute"]["description"].lower()
        assert "retry_safe" in desc


# ===========================================================================
# SECTION F: Runner allowlist, models, errors
# ===========================================================================


class TestRunnerAllowlist:
    """Runner allowlist matches PR #6 scope."""

    def test_wallet_login_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        # Should not raise
        CircleCliRunner._validate_args(
            ("wallet", "login", "user@example.com", "--type", "agent", "--init")
        )

    def test_session_status_not_allowed(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(("session", "status"))

    def test_wallet_create_not_allowed(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(("wallet", "create", "--type", "agent"))

    def test_login_otp_not_allowed(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(("login", "otp", "--request-id", "x", "--otp", "123"))

    def test_logout_not_allowed(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(("logout",))

    def test_transfer_blocked_as_mutation(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(
                ("wallet", "transfer", "--dest", "0xabc", "--amount", "0")
            )


class TestModelContracts:
    """Verify removed models are gone and kept models work."""

    def test_session_status_removed(self) -> None:
        from hermes_x402.circle_cli import models

        assert not hasattr(models, "SessionStatus")

    def test_wallet_deploy_result_removed(self) -> None:
        from hermes_x402.circle_cli import models

        assert not hasattr(models, "WalletDeployResult")

    def test_agent_wallet_status_has_terms_accepted(self) -> None:
        from hermes_x402.circle_cli.models import AgentWalletStatus

        status = AgentWalletStatus(
            mainnet_status="NOT_LOGGED_IN",
            testnet_status="VALID",
            email="user@example.com",
            terms_accepted=True,
        )
        assert status.authenticated is True
        assert status.terms_accepted is True

    def test_agent_wallet_status_authenticated(self) -> None:
        from hermes_x402.circle_cli.models import AgentWalletStatus

        status = AgentWalletStatus(
            mainnet_status="NOT_VALID",
            testnet_status="NOT_VALID",
        )
        assert status.authenticated is False

    def test_gateway_balance_result(self) -> None:
        from hermes_x402.circle_cli.models import GatewayBalanceResult

        result = GatewayBalanceResult(total_usdc="1.50", network="ARC-TESTNET", domain=26)
        assert Decimal(result.total_usdc) == Decimal("1.50")


class TestErrorMapping:
    """Verify removed error classes are gone."""

    def test_deployment_errors_removed(self) -> None:
        from hermes_x402.circle_cli import errors

        assert not hasattr(errors, "CircleCliDeploymentTimeoutError")
        assert not hasattr(errors, "CircleCliDeploymentAmbiguousError")

    def test_terms_error_still_exists(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliTermsRequiredError

        assert CircleCliTermsRequiredError is not None
