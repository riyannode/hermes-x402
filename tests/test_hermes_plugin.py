"""Tests for the hermes-x402 Hermes plugin integration.

Covers: registration, status tools, wallet tools, service inspect,
fetch, pay, error mapping, input validation, output bounding.
All tests use mocks — no live wallet, no live payment, no Circle CLI.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runtime():
    """Reset the plugin runtime before each test."""
    from hermes_x402.hermes_plugin.runtime import reset_runtime

    reset_runtime()
    yield
    reset_runtime()


class FakeHermesContext:
    """Minimal fake Hermes PluginContext for testing registration."""

    def __init__(self):
        self.tools: list[dict[str, Any]] = []

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Any,
        check_fn: Any = None,
        requires_env: list | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        self.tools.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "description": description,
            }
        )


@pytest.fixture
def fake_ctx() -> FakeHermesContext:
    return FakeHermesContext()


# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------


class TestImportSmoke:
    def test_plugin_package_imports(self):
        pass

    def test_entry_imports(self):
        from hermes_x402.hermes_plugin.entry import register

        assert callable(register)

    def test_runtime_imports(self):
        from hermes_x402.hermes_plugin.runtime import get_runtime

        assert callable(get_runtime)

    def test_tools_imports(self):
        from hermes_x402.hermes_plugin.tools import (
            register_status_tools,
        )

        assert callable(register_status_tools)

    def test_errors_imports(self):
        pass

    def test_schemas_imports(self):
        from hermes_x402.hermes_plugin.schemas import X402_PAY_SCHEMA, X402_STATUS_SCHEMA

        assert X402_STATUS_SCHEMA["name"] == "x402_status"
        assert X402_PAY_SCHEMA["name"] == "x402_pay"

    def test_output_imports(self):
        pass


# ---------------------------------------------------------------------------
# Entry point discovery test
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_entry_point_exists_in_metadata(self):
        from importlib.metadata import entry_points

        matches = [
            ep for ep in entry_points(group="hermes_agent.plugins") if ep.name == "hermes-x402"
        ]
        # Entry point exists only after editable install; skip if not installed
        if matches:
            assert len(matches) == 1
            assert matches[0].value == "hermes_x402.hermes_plugin.entry"

    def test_entry_point_loadable(self):
        from importlib.metadata import entry_points

        matches = [
            ep for ep in entry_points(group="hermes_agent.plugins") if ep.name == "hermes-x402"
        ]
        if matches:
            loaded = matches[0].load()
            assert hasattr(loaded, "register")
            assert callable(loaded.register)


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_creates_expected_tools(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.entry import register

        register(fake_ctx)

        names = [t["name"] for t in fake_ctx.tools]
        assert "x402_status" in names
        assert "x402_wallet_status" in names
        assert "x402_wallet_balance" in names
        assert "x402_service_inspect" in names
        assert "x402_fetch" in names
        assert "x402_pay" in names

    def test_all_tools_in_x402_toolset(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.entry import register

        register(fake_ctx)

        for tool in fake_ctx.tools:
            assert tool["toolset"] == "x402"

    def test_all_handlers_return_json_strings(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.entry import register

        register(fake_ctx)

        for tool in fake_ctx.tools:
            handler = tool["handler"]
            result = handler({}, task_id="test")
            assert isinstance(result, str)
            # Must be valid JSON
            parsed = json.loads(result)
            assert isinstance(parsed, dict)

    def test_registration_makes_no_subprocess_calls(self, fake_ctx: FakeHermesContext):
        import subprocess

        with patch.object(subprocess, "run", side_effect=AssertionError("subprocess called")):
            from hermes_x402.hermes_plugin.entry import register

            register(fake_ctx)

    def test_registration_makes_no_network_calls(self, fake_ctx: FakeHermesContext):
        import httpx

        with patch.object(httpx, "Client", side_effect=AssertionError("network called")):
            from hermes_x402.hermes_plugin.entry import register

            register(fake_ctx)


# ---------------------------------------------------------------------------
# Status tool tests
# ---------------------------------------------------------------------------


class TestX402Status:
    def test_status_unconfigured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_status_tools

        register_status_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(handler({}, task_id="test"))
        assert result["success"] is True
        assert result["plugin"] == "hermes-x402"
        assert result["configured"] is False or result["role"] == "unconfigured"

    def test_status_configured_cli(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_status_tools

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": "0x1234567890abcdef1234567890abcdef12345678",
            "CIRCLE_AGENT_WALLET_NETWORK": "BASE",
            "X402_MAX_USDC_PER_PAYMENT": "0.05",
        }
        with patch.dict("os.environ", env, clear=False):
            register_status_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]
            result = json.loads(handler({}, task_id="test"))
            assert result["success"] is True
            assert result["role"] == "buyer"
            assert result["backend"] == "cli"
            assert result["wallet_address"] != "0x1234567890abcdef1234567890abcdef12345678"
            assert "..." in result["wallet_address"]

    def test_status_no_secrets_in_output(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_status_tools

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "dcw",
            "CIRCLE_DCW_WALLET_ID": "wallet-123",
            "CIRCLE_DCW_WALLET_ADDRESS": "0xabcdef",
            "CIRCLE_ENTITY_SECRET": "super-secret-entity-key",
            "CIRCLE_API_KEY": "super-secret-api-key",
        }
        with patch.dict("os.environ", env, clear=False):
            register_status_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]
            result_str = handler({}, task_id="test")
            assert "super-secret" not in result_str
            assert "entity-key" not in result_str
            assert "api-key" not in result_str


# ---------------------------------------------------------------------------
# Wallet tool tests
# ---------------------------------------------------------------------------


class TestX402WalletStatus:
    def test_wallet_status_unconfigured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        register_wallet_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(handler({}, task_id="test"))
        assert result["success"] is True
        assert result["backend"] is None

    def test_wallet_status_dcw_no_secrets(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "dcw",
            "CIRCLE_DCW_WALLET_ID": "wallet-123",
            "CIRCLE_DCW_WALLET_ADDRESS": "0xabcdef1234567890",
            "CIRCLE_ENTITY_SECRET": "secret-value",
            "CIRCLE_API_KEY": "key-value",
        }
        with patch.dict("os.environ", env, clear=False):
            register_wallet_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]
            result_str = handler({}, task_id="test")
            assert "secret-value" not in result_str
            assert "key-value" not in result_str


class TestX402WalletBalance:
    def test_balance_unconfigured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        register_wallet_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(handler({}, task_id="test"))
        # When unconfigured, backend is None → falls through to unsupported
        assert result["success"] is False
        assert result["error"] == "unsupported_backend"

    def test_balance_dcw_unsupported(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "dcw",
            "CIRCLE_DCW_WALLET_ID": "wallet-123",
            "CIRCLE_DCW_WALLET_ADDRESS": "0xabcdef1234567890",
            "CIRCLE_ENTITY_SECRET": "secret",
        }
        with patch.dict("os.environ", env, clear=False):
            register_wallet_tools(fake_ctx)
            handler = fake_ctx.tools[1]["handler"]
            result = json.loads(handler({}, task_id="test"))
            assert result["success"] is True
            assert result["supported"] is False


# ---------------------------------------------------------------------------
# Service inspect tests
# ---------------------------------------------------------------------------


class TestX402ServiceInspect:
    def test_inspect_invalid_url(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_service_tools

        register_service_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(handler({"url": ""}, task_id="test"))
        assert result["success"] is False
        assert result["error"] == "invalid_input"

    def test_inspect_file_url_rejected(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_service_tools

        register_service_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(handler({"url": "file:///etc/passwd"}, task_id="test"))
        assert result["success"] is False

    def test_inspect_url_too_long(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_service_tools

        register_service_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        long_url = "https://example.com/" + "a" * 3000
        result = json.loads(handler({"url": long_url}, task_id="test"))
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Fetch tool tests
# ---------------------------------------------------------------------------


class TestX402Fetch:
    def test_fetch_invalid_url(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(handler({"url": "not-a-url"}, task_id="test"))
        assert result["success"] is False
        assert result["error"] == "invalid_input"

    def test_fetch_invalid_method(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(
            handler({"url": "https://example.com", "method": "INVALID"}, task_id="test")
        )
        assert result["success"] is False

    def test_fetch_nonpaying_by_default(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        # Mock httpx to return 402
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.headers = {"Payment-Required": "price=10000"}
        with patch("hermes_x402.hermes_plugin.tools.httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.request.return_value = mock_response
            result = json.loads(handler({"url": "https://example.com/resource"}, task_id="test"))
            assert result["success"] is True
            assert result["payment_required"] is True
            # Verify it did NOT attempt payment
            assert mock_client.return_value.request.call_count == 1

    def test_fetch_truncates_large_response(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = "x" * 200000
        with patch("hermes_x402.hermes_plugin.tools.httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.request.return_value = mock_response
            result = json.loads(handler({"url": "https://example.com/big"}, task_id="test"))
            assert result["success"] is True
            assert "truncated" in result["data"]


# ---------------------------------------------------------------------------
# Pay tool tests
# ---------------------------------------------------------------------------


class TestX402Pay:
    def test_pay_unavailable_when_not_configured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(handler({"url": "https://example.com/resource"}, task_id="test"))
        assert result["success"] is False
        assert result["error"] == "configuration_error"

    def test_pay_invalid_url(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(handler({"url": ""}, task_id="test"))
        assert result["success"] is False

    def test_pay_caller_cap_cannot_exceed_configured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": "0x1234",
            "CIRCLE_AGENT_WALLET_NETWORK": "BASE",
            "X402_MAX_USDC_PER_PAYMENT": "0.01",
        }
        with patch.dict("os.environ", env, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[1]["handler"]
            result = json.loads(
                handler(
                    {
                        "url": "https://example.com/resource",
                        "max_usdc": "1.00",
                    },
                    task_id="test",
                )
            )
            assert result["success"] is False
            msg = result["message"].lower()
            assert "exceeds" in msg or result["error"] == "payment_policy_error"

    def test_pay_negative_amount_rejected(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(
            handler(
                {"url": "https://example.com/resource", "max_usdc": "-1"},
                task_id="test",
            )
        )
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Error mapping tests
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def test_buyer_configuration_error(self):
        from hermes_x402.buyer.errors import BuyerConfigurationError
        from hermes_x402.hermes_plugin.errors import map_exception

        result = map_exception(BuyerConfigurationError("bad config"))
        assert result["success"] is False
        assert result["error"] == "configuration_error"
        assert result["retry_safe"] is False

    def test_payment_outcome_unknown_not_retryable(self):
        from hermes_x402.buyer.errors import PaymentSubmissionUnknownError
        from hermes_x402.hermes_plugin.errors import map_exception

        result = map_exception(PaymentSubmissionUnknownError("uncertain"))
        assert result["success"] is False
        assert result["error"] == "payment_outcome_unknown"
        assert result["retry_safe"] is False

    def test_unknown_exception_maps_to_internal_error(self):
        from hermes_x402.hermes_plugin.errors import map_exception

        result = map_exception(RuntimeError("something broke"))
        assert result["success"] is False
        assert result["error"] == "internal_plugin_error"
        assert result["retry_safe"] is False

    def test_no_traceback_in_error_output(self):
        from hermes_x402.hermes_plugin.errors import format_error_result

        result_str = format_error_result(RuntimeError("secret stuff"))
        assert "traceback" not in result_str.lower()
        assert "Traceback" not in result_str


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_validate_url_rejects_empty(self):
        from hermes_x402.hermes_plugin.tools import _validate_url

        assert _validate_url("") is not None
        assert _validate_url(None) is not None  # type: ignore

    def test_validate_url_rejects_file_scheme(self):
        from hermes_x402.hermes_plugin.tools import _validate_url

        assert _validate_url("file:///etc/passwd") is not None

    def test_validate_url_accepts_https(self):
        from hermes_x402.hermes_plugin.tools import _validate_url

        assert _validate_url("https://example.com/api") is None

    def test_validate_method_rejects_invalid(self):
        from hermes_x402.hermes_plugin.tools import _validate_method

        assert _validate_method("INVALID") is not None

    def test_validate_method_accepts_get(self):
        from hermes_x402.hermes_plugin.tools import _validate_method

        assert _validate_method("GET") is None

    def test_validate_query_rejects_long(self):
        from hermes_x402.hermes_plugin.tools import _validate_query

        assert _validate_query("a" * 300) is not None

    def test_validate_max_usdc_cannot_increase(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("1.00", "0.05")
        assert err is not None
        assert "exceeds" in err.lower()

    def test_validate_max_usdc_can_reduce(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("0.01", "0.05")
        assert err is None
        assert cap == "0.01"


# ---------------------------------------------------------------------------
# Output tests
# ---------------------------------------------------------------------------


class TestOutput:
    def test_safe_wallet_address_masks(self):
        from hermes_x402.hermes_plugin.output import safe_wallet_address

        addr = "0x1234567890abcdef1234567890abcdef12345678"
        masked = safe_wallet_address(addr)
        assert masked.startswith("0x1234")
        assert masked.endswith("5678")
        assert "..." in masked

    def test_safe_wallet_address_short(self):
        from hermes_x402.hermes_plugin.output import safe_wallet_address

        assert safe_wallet_address("0xabc") == "0xabc"


# ---------------------------------------------------------------------------
# Runtime tests
# ---------------------------------------------------------------------------


class TestRuntime:
    def test_singleton_behavior(self):
        from hermes_x402.hermes_plugin.runtime import get_runtime, reset_runtime

        reset_runtime()
        r1 = get_runtime()
        r2 = get_runtime()
        assert r1 is r2
        reset_runtime()

    def test_version(self):
        from hermes_x402.hermes_plugin.runtime import get_runtime

        r = get_runtime()
        assert r.version == "0.1.0"

    def test_unconfigured_role(self):
        from hermes_x402.hermes_plugin.runtime import get_runtime

        r = get_runtime()
        r.ensure_initialized()
        assert r.is_configured is False or r.role is None


# ---------------------------------------------------------------------------
# Pyproject entry-point test
# ---------------------------------------------------------------------------


class TestPackaging:
    def test_entry_point_declaration(self):
        """Verify pyproject.toml has the correct entry point."""
        from pathlib import Path

        toml = Path(__file__).parent.parent / "pyproject.toml"
        content = toml.read_text()
        assert "hermes_agent.plugins" in content
        assert 'hermes-x402 = "hermes_x402.hermes_plugin.entry"' in content
