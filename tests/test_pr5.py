"""Comprehensive tests for PR #5: session, wallet, gateway, readiness, and public mode.

Covers:
- Section A: Public network policy defaults, SSRF cases, strict_allowlist opt-in
- Section B: Session status, login start/complete, logout, OTP lifecycle
- Section C: Wallet list, create, deploy, idempotency
- Section D: Gateway balance, deposit preview, deposit execute
- Section E: Aggregate readiness
- Section F: Human approval model (no host approval tools)
- Section H: All PR #4 tests remain green, entry-point dispatch
"""

from __future__ import annotations

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

    def test_from_env_defaults_to_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes_x402.config import X402Config

        monkeypatch.delenv("X402_NETWORK_POLICY", raising=False)
        monkeypatch.delenv("X402_HOST_ALLOWLIST", raising=False)
        monkeypatch.setenv("X402_ROLE", "buyer")
        monkeypatch.setenv("X402_BUYER_BACKEND", "cli")
        monkeypatch.setenv("CIRCLE_AGENT_WALLET_ADDRESS", "0xabc")
        monkeypatch.setenv("CIRCLE_AGENT_WALLET_NETWORK", "ARC-TESTNET")
        monkeypatch.setenv("X402_MAX_USDC_PER_PAYMENT", "1.0")
        config = X402Config.from_env()
        assert config.network_policy == "public"
        assert config.host_allowlist == []

    def test_from_env_require_approval_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes_x402.config import X402Config

        monkeypatch.delenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", raising=False)
        monkeypatch.setenv("X402_ROLE", "buyer")
        monkeypatch.setenv("X402_BUYER_BACKEND", "cli")
        monkeypatch.setenv("CIRCLE_AGENT_WALLET_ADDRESS", "0xabc")
        monkeypatch.setenv("CIRCLE_AGENT_WALLET_NETWORK", "ARC-TESTNET")
        monkeypatch.setenv("X402_MAX_USDC_PER_PAYMENT", "1.0")
        config = X402Config.from_env()
        assert config.require_approval_for_new_host is False


class TestPublicModeSSRF:
    """Public mode accepts arbitrary HTTPS hosts but blocks private/reserved."""

    def test_public_mode_empty_allowlist_accepts_https(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "https://api.example.com/data",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is None

    def test_paylabs_accepted_without_allowlist(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "https://paylabs.example.com/api/brain/run",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is None

    def test_localhost_rejected(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "http://localhost:3000/secret",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is not None
        assert "blocked" in result.lower() or "http" in result.lower()

    def test_127_0_0_1_rejected(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "http://127.0.0.1:8080/admin",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is not None

    def test_rfc1918_private_rejected(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        for addr in ["10.0.0.1", "172.16.0.1", "192.168.1.1"]:
            result = _validate_allowed_url(
                f"http://{addr}/secret",
                host_allowlist=[],
                mode="public",
                allow_http=False,
            )
            assert result is not None, f"{addr} should be rejected"

    def test_link_local_rejected(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "http://169.254.169.254/metadata",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is not None

    def test_cloud_metadata_rejected(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "http://169.254.169.254/latest/meta-data/",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is not None

    def test_http_rejected_by_default(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "http://example.com/data",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is not None
        assert "http" in result.lower()

    def test_strict_allowlist_opt_in(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "https://example.com/data",
            host_allowlist=["example.com"],
            mode="strict_allowlist",
            allow_http=False,
        )
        assert result is None

    def test_strict_allowlist_blocks_unlisted(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "https://other.com/data",
            host_allowlist=["example.com"],
            mode="strict_allowlist",
            allow_http=False,
        )
        assert result is not None
        assert "allowlist" in result.lower()

    def test_strict_allowlist_empty_blocks_all(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "https://example.com/data",
            host_allowlist=[],
            mode="strict_allowlist",
            allow_http=False,
        )
        assert result is not None
        assert "empty" in result.lower() or "no hosts" in result.lower()

    def test_userinfo_rejected(self) -> None:
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        result = _validate_allowed_url(
            "https://user:pass@example.com/data",
            host_allowlist=[],
            mode="public",
            allow_http=False,
        )
        assert result is not None
        assert "credential" in result.lower() or "userinfo" in result.lower()


# ===========================================================================
# SECTION B: Session and OTP login tools
# ===========================================================================


class TestSessionStatus:
    """Session status tool tests."""

    def test_session_status_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        assert "x402_session_status" in ctx.tools

    def test_login_start_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        assert "x402_login_start" in ctx.tools

    def test_login_complete_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        assert "x402_login_complete" in ctx.tools

    def test_logout_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        assert "x402_logout" in ctx.tools

    def test_session_status_authenticated(self) -> None:
        from hermes_x402.circle_cli.models import SessionStatus

        status = SessionStatus(authenticated=True, environment="testnet", email="user@example.com")
        assert status.authenticated is True
        assert status.environment == "testnet"

    def test_session_status_expired(self) -> None:
        from hermes_x402.circle_cli.models import SessionStatus

        status = SessionStatus(
            authenticated=False,
            environment="unknown",
            status_code="NOT_LOGGED_IN",
        )
        assert status.authenticated is False
        assert status.status_code == "NOT_LOGGED_IN"

    def test_session_status_terms_required(self) -> None:
        from hermes_x402.circle_cli.models import SessionStatus

        status = SessionStatus(
            authenticated=False,
            environment="unknown",
            terms_accepted=False,
            status_code="TERMS_REQUIRED",
        )
        assert status.terms_accepted is False


class TestLoginOTP:
    """Login start, OTP completion, and expiry lifecycle."""

    def test_login_start_result(self) -> None:
        from hermes_x402.circle_cli.models import LoginStartResult

        result = LoginStartResult(request_id="req_123", email_masked="u***@example.com")
        assert result.request_id == "req_123"
        assert result.otp_required is True

    async def test_login_complete_handler_rejects_invalid_email(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        handler = ctx.tools["x402_login_start"]["handler"]
        # Invalid email should fail
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            mock_get_rt.return_value = rt
            result_str = await handler({"email": "invalid"})
            import json

            result = json.loads(result_str)
            assert result["success"] is False
            assert result.get("error") == "invalid_input"

    async def test_login_complete_rejects_missing_otp(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        handler = ctx.tools["x402_login_complete"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            mock_get_rt.return_value = rt
            result_str = await handler({"request_id": "req_1", "otp": ""})
            import json

            result = json.loads(result_str)
            assert result["success"] is False

    async def test_login_complete_rejects_unknown_request(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        handler = ctx.tools["x402_login_complete"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            mock_get_rt.return_value = rt
            result_str = await handler({"request_id": "unknown", "otp": "123456"})
            import json

            result = json.loads(result_str)
            assert result["success"] is False
            assert result.get("error") == "invalid_request"

    async def test_logout_is_idempotent(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_session_tools

        ctx = MockCtx()
        register_session_tools(ctx)
        handler = ctx.tools["x402_logout"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            rt.cli_client.logout = AsyncMock()
            mock_get_rt.return_value = rt
            result_str = await handler({})
            import json

            result = json.loads(result_str)
            assert result["success"] is True


# ===========================================================================
# SECTION C: Agent Wallet readiness
# ===========================================================================


class TestWalletManagement:
    """Wallet list, create, deploy tests."""

    def test_wallet_list_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_wallet_management_tools

        ctx = MockCtx()
        register_wallet_management_tools(ctx)
        assert "x402_wallet_list" in ctx.tools

    def test_wallet_create_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_wallet_management_tools

        ctx = MockCtx()
        register_wallet_management_tools(ctx)
        assert "x402_wallet_create" in ctx.tools

    def test_wallet_deploy_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_wallet_management_tools

        ctx = MockCtx()
        register_wallet_management_tools(ctx)
        assert "x402_wallet_deploy" in ctx.tools

    def test_wallet_deploy_model(self) -> None:
        from hermes_x402.circle_cli.models import WalletDeployResult

        result = WalletDeployResult(
            wallet_address="0xabc",
            operation_id="op_1",
            transaction_hash="0xdef",
            status="submitted",
        )
        assert result.status == "submitted"
        assert result.operation_id == "op_1"

    def test_wallet_deploy_already_deployed(self) -> None:
        from hermes_x402.circle_cli.models import WalletDeployResult

        result = WalletDeployResult(wallet_address="0xabc", status="already_deployed")
        assert result.status == "already_deployed"

    async def test_wallet_create_handler(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_wallet_management_tools

        ctx = MockCtx()
        register_wallet_management_tools(ctx)
        handler = ctx.tools["x402_wallet_create"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            rt.cli_client.wallet_create = AsyncMock(
                return_value=MagicMock(
                    address="0xnew123",
                    blockchain="ARC-TESTNET",
                    created_at="2026-01-01",
                )
            )
            mock_get_rt.return_value = rt
            result_str = await handler({})
            import json

            result = json.loads(result_str)
            assert result["success"] is True
            assert "new123" in result["address"]


# ===========================================================================
# SECTION D: Circle Gateway readiness
# ===========================================================================


class TestGatewayReadiness:
    """Gateway balance, deposit preview, deposit execute tests."""

    def test_gateway_balance_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        assert "x402_gateway_balance" in ctx.tools

    def test_deposit_preview_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        assert "x402_gateway_deposit_preview" in ctx.tools

    def test_deposit_execute_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        assert "x402_gateway_deposit_execute" in ctx.tools

    def test_gateway_balance_model(self) -> None:
        from hermes_x402.circle_cli.models import GatewayBalanceResult

        result = GatewayBalanceResult(total_usdc="5.0", network="ARC-TESTNET", domain=26)
        assert result.total_usdc == "5.0"
        assert result.network == "ARC-TESTNET"

    def test_gateway_deposit_model(self) -> None:
        from hermes_x402.circle_cli.models import GatewayDepositResult

        result = GatewayDepositResult(
            operation_id="op_2",
            transaction_hash="0xhash",
            status="completed",
            network="ARC-TESTNET",
        )
        assert result.status == "completed"

    async def test_deposit_preview_requires_amount(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_preview"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            mock_get_rt.return_value = rt
            result_str = await handler({"amount": ""})
            import json

            result = json.loads(result_str)
            assert result["success"] is False
            assert result.get("error") == "invalid_input"

    async def test_deposit_preview_rejects_non_numeric(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_preview"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            mock_get_rt.return_value = rt
            result_str = await handler({"amount": "abc"})
            import json

            result = json.loads(result_str)
            assert result["success"] is False

    async def test_deposit_execute_rejects_no_preview_id(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_execute"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            mock_get_rt.return_value = rt
            result_str = await handler({"preview_id": ""})
            import json

            result = json.loads(result_str)
            assert result["success"] is False
            assert result.get("error") == "invalid_input"

    async def test_deposit_execute_rejects_unknown_preview(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        ctx = MockCtx()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_execute"]["handler"]
        with patch("hermes_x402.hermes_plugin.tools.get_runtime") as mock_get_rt:
            rt = _make_runtime_mock()
            mock_get_rt.return_value = rt
            result_str = await handler({"preview_id": "nonexistent"})
            import json

            result = json.loads(result_str)
            assert result["success"] is False
            assert result.get("error") == "invalid_preview"


# ===========================================================================
# SECTION E: Aggregate readiness
# ===========================================================================


class TestReadiness:
    """Aggregate readiness tool tests."""

    def test_readiness_tool_registered(self) -> None:
        from hermes_x402.hermes_plugin.tools import register_readiness_tools

        ctx = MockCtx()
        register_readiness_tools(ctx)
        assert "x402_readiness" in ctx.tools

    async def test_readiness_returns_ready_field(self) -> None:
        from hermes_x402.readiness import assess_readiness

        result = await assess_readiness(
            config=None,
            cli_client=None,
            wallet_address="",
            network="",
            role=None,
            backend_name=None,
        )
        assert "ready" in result
        assert "checks" in result
        assert "blockers" in result

    async def test_readiness_not_ready_when_no_config(self) -> None:
        from hermes_x402.readiness import assess_readiness

        result = await assess_readiness(
            config=None,
            cli_client=None,
            wallet_address="",
            network="",
            role=None,
            backend_name=None,
        )
        assert result["ready"] is False
        codes = [b["code"] for b in result["blockers"]]
        assert "not_configured" in codes

    async def test_readiness_has_checks(self) -> None:
        from hermes_x402.readiness import assess_readiness

        result = await assess_readiness(
            config=None,
            cli_client=None,
            wallet_address="",
            network="",
            role=None,
            backend_name=None,
        )
        check_names = [c["name"] for c in result["checks"]]
        assert "plugin_config" in check_names
        assert "network_policy" in check_names


# ===========================================================================
# SECTION F: Human approval model
# ===========================================================================


class TestHumanApprovalModel:
    """Verify no host approval tools are created."""

    def test_no_host_approve_tool(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        tool_names = set(ctx.tools.keys())
        # Must NOT have these tools
        assert "x402_host_approve" not in tool_names
        assert "x402_host_revoke" not in tool_names
        assert "x402_trusted_hosts" not in tool_names

    def test_all_expected_tools_registered(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
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
            "x402_session_status",
            "x402_login_start",
            "x402_login_complete",
            "x402_logout",
            "x402_wallet_list",
            "x402_wallet_create",
            "x402_wallet_deploy",
            "x402_gateway_balance",
            "x402_gateway_deposit_preview",
            "x402_gateway_deposit_execute",
            "x402_readiness",
        }
        registered = set(ctx.tools.keys())
        missing = expected - registered
        assert not missing, f"Missing tools: {missing}"


# ===========================================================================
# SECTION H: Entry-point dispatch, tool descriptions, existing tests green
# ===========================================================================


class TestEntryPointDispatch:
    """Plugin entry point dispatches all new tools."""

    def test_entry_registers_all_new_groups(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        # All 20 tools should be registered
        assert len(ctx.tools) >= 20

    def test_all_tools_have_descriptions(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        for name, spec in ctx.tools.items():
            desc = spec.get("description", "")
            assert desc, f"{name} has no description"

    def test_all_tools_have_schemas(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        for name, spec in ctx.tools.items():
            schema = spec.get("schema")
            assert schema is not None, f"{name} has no schema"
            assert "name" in schema, f"{name} schema has no name"
            assert "parameters" in schema, f"{name} schema has no parameters"


class TestToolDescriptionsGuideModel:
    """Tool descriptions guide the model toward the intended workflow."""

    def test_readiness_mentions_read_only(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_readiness"]["description"].lower()
        assert "read-only" in desc or "readonly" in desc

    def test_search_mentions_workflow(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_service_search"]["description"].lower()
        assert "inspect" in desc or "supports" in desc

    def test_pay_mentions_fresh_challenge(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_pay"]["description"].lower()
        assert "fresh" in desc or "challenge" in desc

    def test_login_mentions_terms(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_login_start"]["description"].lower()
        assert "terms" in desc

    def test_deposit_preview_mentions_read_only(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_gateway_deposit_preview"]["description"].lower()
        assert "read-only" in desc or "must not" in desc

    def test_deposit_execute_mentions_retry_safe(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_gateway_deposit_execute"]["description"].lower()
        assert "retry_safe" in desc or "retry" in desc

    def test_wallet_deploy_mentions_idempotent(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_wallet_deploy"]["description"].lower()
        assert "idempotent" in desc

    def test_session_status_mentions_masked(self) -> None:
        from hermes_x402.hermes_plugin import entry

        ctx = MockCtx()
        entry.register(ctx)
        desc = ctx.tools["x402_session_status"]["description"].lower()
        assert "mask" in desc


class TestNewModels:
    """Verify all new frozen dataclass models."""

    def test_session_status_frozen(self) -> None:
        from hermes_x402.circle_cli.models import SessionStatus

        s = SessionStatus(authenticated=True, environment="testnet")
        with pytest.raises(AttributeError):
            s.authenticated = False  # type: ignore[misc]

    def test_login_start_frozen(self) -> None:
        from hermes_x402.circle_cli.models import LoginStartResult

        r = LoginStartResult(request_id="r1", email_masked="u*@e.com")
        with pytest.raises(AttributeError):
            r.request_id = "r2"  # type: ignore[misc]

    def test_wallet_deploy_frozen(self) -> None:
        from hermes_x402.circle_cli.models import WalletDeployResult

        d = WalletDeployResult(wallet_address="0xabc")
        with pytest.raises(AttributeError):
            d.status = "done"  # type: ignore[misc]

    def test_gateway_balance_frozen(self) -> None:
        from hermes_x402.circle_cli.models import GatewayBalanceResult

        g = GatewayBalanceResult(total_usdc="1.0")
        with pytest.raises(AttributeError):
            g.total_usdc = "2.0"  # type: ignore[misc]

    def test_gateway_deposit_frozen(self) -> None:
        from hermes_x402.circle_cli.models import GatewayDepositResult

        d = GatewayDepositResult()
        with pytest.raises(AttributeError):
            d.status = "done"  # type: ignore[misc]


class TestConfigDefaultsBackwardCompat:
    """Backward compatibility: strict_allowlist still works as explicit opt-in."""

    def test_strict_allowlist_still_works(self) -> None:
        from hermes_x402.config import X402Config

        config = X402Config(network_policy="strict_allowlist")
        assert config.network_policy == "strict_allowlist"

    def test_from_env_explicit_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes_x402.config import X402Config

        monkeypatch.setenv("X402_NETWORK_POLICY", "strict_allowlist")
        monkeypatch.setenv("X402_ROLE", "buyer")
        monkeypatch.setenv("X402_BUYER_BACKEND", "cli")
        monkeypatch.setenv("CIRCLE_AGENT_WALLET_ADDRESS", "0xabc")
        monkeypatch.setenv("CIRCLE_AGENT_WALLET_NETWORK", "ARC-TESTNET")
        monkeypatch.setenv("X402_MAX_USDC_PER_PAYMENT", "1.0")
        config = X402Config.from_env()
        assert config.network_policy == "strict_allowlist"


class TestReadinessModule:
    """Tests for the readiness assessment module."""

    def test_mask_email(self) -> None:
        from hermes_x402.readiness import _mask_email

        assert _mask_email("user@example.com") == "u***@example.com"
        assert _mask_email(None) == "***"
        assert _mask_email("a@b.com") == "*@b.com"

    def test_mask_address(self) -> None:
        from hermes_x402.readiness import _mask_address

        addr = "0xabcdef1234567890abcdef1234567890abcdef12"
        masked = _mask_address(addr)
        assert masked.startswith("0xabc")
        assert masked.endswith("ef12")
        assert "..." in masked

    def test_mask_address_short(self) -> None:
        from hermes_x402.readiness import _mask_address

        assert _mask_address("0xabc") == "***"


class TestRunnerAllowlist:
    """Verify the CLI runner allowlist includes new commands."""

    def test_session_status_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        args = CircleCliRunner._validate_args(("session", "status", "--output", "json"))
        assert args[0] == "session"

    def test_login_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        args = CircleCliRunner._validate_args(("login", "--email", "a@b.com"))
        assert args[0] == "login"

    def test_login_otp_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        args = CircleCliRunner._validate_args(
            ("login", "otp", "--request-id", "r1", "--otp", "123456")
        )
        assert args[0] == "login"
        assert args[1] == "otp"

    def test_logout_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        args = CircleCliRunner._validate_args(("logout",))
        assert args[0] == "logout"

    def test_wallet_create_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        args = CircleCliRunner._validate_args(
            ("wallet", "create", "--chain", "ARC-TESTNET", "--type", "agent")
        )
        assert args[0] == "wallet"
        assert args[1] == "create"

    def test_gateway_balance_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        args = CircleCliRunner._validate_args(
            ("gateway", "balance", "--address", "0xabc", "--chain", "ARC-TESTNET")
        )
        assert args[0] == "gateway"
        assert args[1] == "balance"

    def test_gateway_deposit_allowed(self) -> None:
        from hermes_x402.circle_cli.runner import CircleCliRunner

        args = CircleCliRunner._validate_args(
            (
                "gateway",
                "deposit",
                "--address",
                "0xabc",
                "--chain",
                "ARC-TESTNET",
                "--amount",
                "1.0",
            )
        )
        assert args[0] == "gateway"
        assert args[1] == "deposit"

    def test_terms_still_blocked(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(("terms",))

    def test_transfer_still_blocked(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(("transfer",))

    def test_execute_still_blocked(self) -> None:
        from hermes_x402.circle_cli.errors import CircleCliUnsupportedCapabilityError
        from hermes_x402.circle_cli.runner import CircleCliRunner

        with pytest.raises(CircleCliUnsupportedCapabilityError):
            CircleCliRunner._validate_args(("execute",))
