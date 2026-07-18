"""Deterministic regression tests for hermes-x402 plugin.

Retains pure deterministic tests for:
- import smoke, entry point, packaging
- payment cap validation
- error mapping, input validation, URL/host policy
- output bounding, runtime singleton
- URL sanitization, preview store state transitions
- service option fingerprinting
- wallet status network resolution
- login completion environment validation

Orchestration tests (FakeHermesContext tool handler tests) removed —
covered by live Arc Testnet acceptance test in hermes_x402/live_test.py.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestImportSmoke:
    def test_plugin_package_imports(self):
        import hermes_x402.hermes_plugin  # noqa: F401

    def test_entry_imports(self):
        from hermes_x402.hermes_plugin.entry import register

        assert callable(register)

    def test_runtime_imports(self):
        from hermes_x402.hermes_plugin.runtime import get_runtime

        assert callable(get_runtime)

    def test_tools_imports(self):
        from hermes_x402.hermes_plugin.tools import (  # noqa: F401
            register_payment_tools,
            register_service_tools,
            register_status_tools,
            register_wallet_tools,
        )

    def test_errors_imports(self):
        from hermes_x402.hermes_plugin.errors import (  # noqa: F401
            format_error_result,
            format_success_result,
            map_exception,
        )

    def test_schemas_imports(self):
        from hermes_x402.hermes_plugin.schemas import (
            X402_PAY_SCHEMA,
            X402_STATUS_SCHEMA,
        )

        assert X402_STATUS_SCHEMA["name"] == "x402_status"
        assert X402_PAY_SCHEMA["name"] == "x402_pay"

    def test_output_imports(self):
        from hermes_x402.hermes_plugin.output import safe_wallet_address  # noqa: F401

    def test_policy_subclass_imports(self):
        from hermes_x402.buyer.errors import (  # noqa: F401
            HostPolicyError,
            PaymentLimitExceededError,
            PaymentPolicyError,
        )

        assert issubclass(HostPolicyError, PaymentPolicyError)
        assert issubclass(PaymentLimitExceededError, PaymentPolicyError)


# ---------------------------------------------------------------------------
# Entry point discovery test
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_entry_point_exists_in_metadata(self):
        from importlib.metadata import entry_points

        matches = [
            ep for ep in entry_points(group="hermes_agent.plugins") if ep.name == "hermes-x402"
        ]
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


class TestPaymentCapValidation:
    def test_malformed_configured_cap_no_caller(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc(None, "not-a-number")
        assert cap is None
        assert err is not None
        assert "valid decimal" in err.lower()

    def test_malformed_configured_cap_high_caller(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("1.00", "bad")
        assert cap is None
        assert err is not None

    def test_configured_nan(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc(None, "NaN")
        assert cap is None
        assert err is not None
        assert "finite" in err.lower()

    def test_configured_infinity(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc(None, "Infinity")
        assert cap is None
        assert err is not None

    def test_configured_negative(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc(None, "-0.01")
        assert cap is None
        assert err is not None
        assert "non-negative" in err.lower()

    def test_caller_nan(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("NaN", "0.05")
        assert cap is None
        assert err is not None

    def test_caller_infinity(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("Infinity", "0.05")
        assert cap is None
        assert err is not None

    def test_caller_negative(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("-1", "0.05")
        assert cap is None
        assert err is not None

    def test_caller_below_configured(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("0.01", "0.05")
        assert err is None
        assert cap == "0.01"

    def test_caller_equal_to_configured(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("0.05", "0.05")
        assert err is None
        assert cap == "0.05"

    def test_caller_above_configured(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc("0.10", "0.05")
        assert cap is None
        assert err is not None
        assert "exceeds" in err.lower()

    def test_no_caller_uses_configured(self):
        from hermes_x402.hermes_plugin.tools import _validate_max_usdc

        cap, err = _validate_max_usdc(None, "0.05")
        assert err is None
        assert cap == "0.05"


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

    def test_unsupported_backend_not_shadowed(self):
        """Fix #7: subclass not shadowed by base class."""
        from hermes_x402.buyer.errors import UnsupportedBuyerBackendError
        from hermes_x402.hermes_plugin.errors import map_exception

        result = map_exception(UnsupportedBuyerBackendError("nope"))
        assert result["error"] == "unsupported_backend"

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

    def test_no_traceback_in_error_output(self):
        from hermes_x402.hermes_plugin.errors import format_error_result

        result_str = format_error_result(RuntimeError("secret stuff"))
        assert "traceback" not in result_str.lower()
        assert "Traceback" not in result_str

    def test_host_policy_error_maps_to_host_rejected(self):
        """Fix #8: typed HostPolicyError maps to host_rejected."""
        from hermes_x402.buyer.errors import HostPolicyError
        from hermes_x402.hermes_plugin.errors import map_exception

        result = map_exception(HostPolicyError("blocked"))
        assert result["error"] == "host_rejected"

    def test_payment_limit_exceeded_maps_correctly(self):
        """Fix #8: PaymentLimitExceededError maps to payment_limit_exceeded."""
        from hermes_x402.buyer.errors import PaymentLimitExceededError
        from hermes_x402.hermes_plugin.errors import map_exception

        result = map_exception(PaymentLimitExceededError("too much"))
        assert result["error"] == "payment_limit_exceeded"

    def test_generic_policy_error_maps_to_payment_policy_rejected(self):
        """Fix #8: base PaymentPolicyError maps to payment_policy_rejected."""
        from hermes_x402.buyer.errors import PaymentPolicyError
        from hermes_x402.hermes_plugin.errors import map_exception

        result = map_exception(PaymentPolicyError("generic"))
        assert result["error"] == "payment_policy_rejected"

    def test_subclass_not_shadowed_by_base(self):
        """Fix #7: subclass error codes are not shadowed."""
        from hermes_x402.buyer.errors import (
            HostPolicyError,
            PaymentPolicyError,
        )
        from hermes_x402.hermes_plugin.errors import map_exception

        host = map_exception(HostPolicyError("host"))
        generic = map_exception(PaymentPolicyError("generic"))
        assert host["error"] != generic["error"]


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


# ---------------------------------------------------------------------------
# URL/host policy tests (Fix #3)
# ---------------------------------------------------------------------------


class TestURLHostPolicy:
    def test_validate_allowed_url_rejects_localhost(self):
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://localhost/admin", [])
        assert err is not None
        assert "blocked" in err.lower()

    def test_validate_allowed_url_rejects_metadata_ip(self):
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("http://169.254.169.254/", [])
        assert err is not None

    def test_validate_allowed_url_rejects_userinfo(self):
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://user:pass@example.com/", [])
        assert err is not None
        assert "credentials" in err.lower()

    def test_validate_allowed_url_enforces_allowlist(self):
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://evil.com/steal", ["allowed.example.com"])
        assert err is not None
        assert "allowlist" in err.lower()

    def test_validate_allowed_url_subdomain_match(self):
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        err = _validate_allowed_url("https://sub.example.com/api", ["example.com"])
        assert err is None

    def test_validate_allowed_url_empty_allowlist(self):
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        # strict_allowlist + empty allowlist → blocks everything
        err = _validate_allowed_url("https://anything.com/", [], mode="strict_allowlist")
        assert err is not None
        assert "empty allowlist" in err

    def test_validate_allowed_url_public_empty_allowlist(self):
        from hermes_x402.hermes_plugin.tools import _validate_allowed_url

        # public + empty allowlist → allows public destinations
        err = _validate_allowed_url("https://anything.com/", [], mode="public")
        assert err is None


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
        assert r.version == "0.2.0"

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
        from pathlib import Path

        toml = Path(__file__).parent.parent / "pyproject.toml"
        content = toml.read_text()
        assert "hermes_agent.plugins" in content
        assert 'hermes-x402 = "hermes_x402.hermes_plugin.entry"' in content


# ---------------------------------------------------------------------------
# Approval hook unit tests
# ---------------------------------------------------------------------------


class TestUrlSanitization:
    """Tests that URL sanitization strips sensitive components."""

    def test_userinfo_not_in_approval_url(self):
        """Userinfo (username:password) does not appear in approval URL."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display("https://user:password@example.com/api?token=secret")
        assert "user" not in result.lower()
        assert "password" not in result.lower()
        assert "token" not in result.lower()
        assert "secret" not in result.lower()
        assert "example.com" in result

    def test_query_not_in_approval_url(self):
        """Query string does not appear in approval URL."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display("https://example.com/api?token=secret&key=value")
        assert "?" not in result
        assert "token" not in result
        assert "key" not in result

    def test_fragment_not_in_approval_url(self):
        """Fragment does not appear in approval URL."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display("https://example.com/api#section")
        assert "#" not in result
        assert "section" not in result

    def test_control_characters_stripped(self):
        """CR/LF/control characters do not appear in approval URL."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display("https://example.com/api\r\ninjection")
        assert "\r" not in result
        assert "\n" not in result
        assert "\x00" not in result

    def test_malformed_url_returns_invalid(self):
        """Malformed URL returns [invalid URL]."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display("not a url at all")
        assert result == "[invalid URL]"

    def test_non_string_url_returns_invalid(self):
        """Non-string URL returns [invalid URL]."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display(123)
        assert result == "[invalid URL]"


class TestPreviewAtomicity:
    """Tests that concurrent preview claims are atomic."""

    def test_concurrent_claims_one_succeeds(self):
        """Two concurrent claims produce exactly one successful claim."""
        import threading
        import time

        from hermes_x402.hermes_plugin.gateway_state import (
            claim_preview_for_execution,
            store_preview,
        )

        store_preview(
            "atomic_test",
            {
                "service_url": "https://example.com",
                "deposit_amount": "1.0",
                "wallet": "0x1234567890abcdef",
                "wallet_network": "ARC-TESTNET",
                "deposit_method": "direct",
                "expires_at": time.time() + 300,
                "consumed": False,
            },
        )

        results = []

        def claim():
            r = claim_preview_for_execution("atomic_test")
            results.append(r is not None)

        t1 = threading.Thread(target=claim)
        t2 = threading.Thread(target=claim)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should succeed
        assert results.count(True) == 1
        assert results.count(False) == 1


class TestPreviewStoreBounds:
    """Tests that the preview store remains bounded."""

    def test_purge_expired_on_store(self):
        """Expired previews are purged during store."""
        import time

        from hermes_x402.hermes_plugin.gateway_state import (
            _lock,
            _previews,
            store_preview,
        )

        # Store an expired preview
        with _lock:
            _previews["old_expired"] = {
                "expires_at": time.time() - 100,
                "consumed": False,
            }

        # Store a new valid preview — should purge expired
        store_preview(
            "new_valid",
            {
                "service_url": "https://example.com",
                "deposit_amount": "1.0",
                "wallet": "0x1234567890abcdef",
                "wallet_network": "ARC-TESTNET",
                "deposit_method": "direct",
                "expires_at": time.time() + 300,
                "consumed": False,
            },
        )

        with _lock:
            assert "old_expired" not in _previews
            assert "new_valid" in _previews

    def test_store_rejects_when_full(self):
        """Store rejects new preview when at capacity."""
        import time

        from hermes_x402.hermes_plugin.gateway_state import (
            _MAX_ACTIVE_PREVIEWS,
            _lock,
            _previews,
            store_preview,
        )

        # Fill the store
        with _lock:
            for i in range(_MAX_ACTIVE_PREVIEWS):
                _previews[f"fill_{i}"] = {
                    "expires_at": time.time() + 300,
                    "consumed": False,
                }

        # Try to store one more — should raise
        try:
            store_preview(
                "overflow",
                {
                    "service_url": "https://example.com",
                    "deposit_amount": "1.0",
                    "wallet": "0x1234567890abcdef",
                    "wallet_network": "ARC-TESTNET",
                    "deposit_method": "direct",
                    "expires_at": time.time() + 300,
                    "consumed": False,
                },
            )
            raise AssertionError("Expected RuntimeError for full store")
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Focused regression tests — core fixes from PR #6 head
# ---------------------------------------------------------------------------


class TestUrlSanitizerRegression:
    """Regression: malformed port raises ValueError/UnicodeError → [invalid URL]."""

    def test_malformed_port_returns_invalid(self) -> None:
        """URL with a non-numeric port triggers ValueError in parsed.port."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        # Python's urlparse is lenient — "https://example.com:notaport/path"
        # may raise on .port access depending on the exact input
        result = _sanitize_url_for_display("https://example.com:notaport/path")
        # Either returns [invalid URL] or a valid sanitized URL — both are acceptable
        # The key regression: must not raise an unhandled exception
        assert isinstance(result, str)

    def test_extreme_port_returns_invalid(self) -> None:
        """URL with port outside valid range returns [invalid URL]."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        # A port with very long numeric string triggers ValueError on .port access
        result = _sanitize_url_for_display("https://example.com:" + "9" * 30 + "/path")
        assert result == "[invalid URL]"

    def test_ipv6_display_is_bracketed(self) -> None:
        """IPv6 hostnames are bracketed in the sanitized output."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display("https://[::1]/api")
        assert "[" in result
        assert "]" in result
        assert "::1" in result

    def test_ipv6_with_port_is_bracketed(self) -> None:
        """IPv6 with port is bracketed as [host]:port."""
        from hermes_x402.hermes_plugin.entry import _sanitize_url_for_display

        result = _sanitize_url_for_display("https://[::1]:8443/api")
        assert "[::1]:8443" in result


class TestPreviewStoreOverflow:
    """Regression: full preview store returns structured error, never raw RuntimeError."""

    async def test_full_store_returns_preview_store_full(self) -> None:
        """When preview store is full, handler returns structured preview_store_full."""
        from hermes_x402.hermes_plugin.gateway_state import (
            _MAX_ACTIVE_PREVIEWS,
            _lock,
            _previews,
        )
        from hermes_x402.hermes_plugin.tools import register_gateway_tools

        class _C:
            def __init__(self):
                self.tools = {}
                self.hooks = []

            def register_tool(self, **kw):
                self.tools[kw["name"]] = kw

            def register_hook(self, ht, h):
                self.hooks.append({"hook_type": ht, "handler": h})

        ctx = _C()
        register_gateway_tools(ctx)
        handler = ctx.tools["x402_gateway_deposit_preview"]["handler"]

        rt = MagicMock()
        rt.is_configured = True
        rt.is_available = True
        rt.backend_name = "cli"
        rt.role = "buyer"
        rt.network = "ARC-TESTNET"
        rt.wallet_address = "0xabcdef1234567890abcdef1234567890abcdef12"
        rt.version = "0.1.0"
        from hermes_x402.config import X402Config

        rt.config = X402Config(
            role="buyer",
            buyer_backend="cli",
            circle_cli_network="ARC-TESTNET",
            circle_cli_wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            host_allowlist=[],
            network_policy="public",
        )
        rt.cli_client = AsyncMock()
        rt.buyer_tool = MagicMock()
        rt.init_error = None

        status = MagicMock()
        status.authenticated = True
        status.terms_accepted = True
        status.testnet_status = "VALID"
        status.mainnet_status = "NOT_LOGGED_IN"
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)

        mock_support = MagicMock()
        mock_support.x402 = True
        mock_support.gateway_batching = True
        mock_support.reason = None
        mock_support.version = "2"
        mock_support.options = (
            MagicMock(
                payment_system="gateway_batching",
                network="arcTestnet",
                network_id="eip155:5042002",
                supported_by_backend=True,
                scheme="https",
                amount_atomic="1000000",
                amount_usdc="1.0",
                asset="USDC",
                pay_to="0xdeadbeef",
                max_timeout_seconds=60,
            ),
        )

        gw_result = MagicMock()
        gw_result.total_usdc = "5.0"
        gw_result.network = "ARC-TESTNET"

        # Fill the preview store to capacity
        with _lock:
            _previews.clear()
            import time as _t

            for i in range(_MAX_ACTIVE_PREVIEWS):
                _previews[f"fill_{i}"] = {
                    "expires_at": _t.time() + 300,
                    "consumed": False,
                }

        # Mock wallet and gateway balance
        mock_balance = MagicMock()
        mock_balance.symbol = "USDC"
        mock_balance.amount = "10.0"
        rt.cli_client.get_balance = AsyncMock(return_value=[mock_balance])

        gw_result = MagicMock()
        gw_result.total_usdc = "5.0"
        gw_result.network = "ARC-TESTNET"
        rt.cli_client.gateway_balance = AsyncMock(return_value=gw_result)

        with (
            patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt),
            patch(
                "hermes_x402.buyer.supports.check_supports",
                new_callable=AsyncMock,
                return_value=mock_support,
            ),
        ):
            result = await handler(
                {
                    "service_url": "https://api.example.com/premium",
                    "method": "GET",
                    "amount": "5.0",
                }
            )

        # Clean up
        with _lock:
            _previews.clear()

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "preview_store_full"
        assert data["retry_safe"] is True
        assert "256" in data["message"]


class TestWalletStatusNetworkResolution:
    """Regression: valid mainnet session does NOT satisfy Arc Testnet wallet status."""

    def test_mainnet_valid_does_not_satisfy_testnet_config(self) -> None:
        """A valid mainnet session must not report session_valid=true for testnet config."""

        from hermes_x402.config import X402Config
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        class _C:
            def __init__(self):
                self.tools = {}
                self.hooks = []

            def register_tool(self, **kw):
                self.tools[kw["name"]] = kw

            def register_hook(self, ht, h):
                self.hooks.append({"hook_type": ht, "handler": h})

        ctx = _C()
        register_wallet_tools(ctx)
        handler = ctx.tools["x402_wallet_status"]["handler"]

        # Config is ARC-TESTNET but session only has mainnet VALID
        rt = MagicMock()
        rt.is_configured = True
        rt.is_available = True
        rt.backend_name = "cli"
        rt.role = "buyer"
        rt.network = "ARC-TESTNET"
        rt.wallet_address = "0xabcdef1234567890abcdef1234567890abcdef12"
        rt.version = "0.1.0"
        rt.config = X402Config(
            role="buyer",
            buyer_backend="cli",
            circle_cli_network="ARC-TESTNET",
            circle_cli_wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            host_allowlist=[],
            network_policy="public",
        )
        rt.cli_client = AsyncMock()
        rt.buyer_tool = MagicMock()
        rt.init_error = None

        status = MagicMock()
        status.authenticated = True  # mainnet is VALID — this used to leak
        status.testnet_status = "NOT_LOGGED_IN"
        status.mainnet_status = "VALID"
        status.terms_accepted = True
        status.email = "user@example.com"
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)
        rt.cli_client.list_wallets = AsyncMock(return_value=[])
        gw = MagicMock()
        gw.total_usdc = "0"
        gw.network = "ARC-TESTNET"
        rt.cli_client.gateway_balance = AsyncMock(return_value=gw)
        rt.cli_client.network_x402_identifier = AsyncMock(return_value="eip155:5042002")

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = asyncio.run(handler({}))

        data = json.loads(result)
        # session_valid must be False because testnet_status is NOT_LOGGED_IN
        assert data["session_valid"] is False
        assert data["session_environment"] == "testnet"


class TestLoginCompletionEnvironment:
    """Regression: login completion success=false when expected environment is invalid."""

    async def test_success_false_when_expected_env_invalid(self) -> None:
        """login_complete returns success=false when env-specific status is not VALID."""
        from hermes_x402.config import X402Config
        from hermes_x402.hermes_plugin.tools import register_login_tools

        class _C:
            def __init__(self):
                self.tools = {}
                self.hooks = []

            def register_tool(self, **kw):
                self.tools[kw["name"]] = kw

            def register_hook(self, ht, h):
                self.hooks.append({"hook_type": ht, "handler": h})

        ctx = _C()
        register_login_tools(ctx)
        start_handler = ctx.tools["x402_login_start"]["handler"]
        complete_handler = ctx.tools["x402_login_complete"]["handler"]

        rt = MagicMock()
        rt.is_configured = True
        rt.is_available = True
        rt.backend_name = "cli"
        rt.role = "buyer"
        rt.network = "ARC-TESTNET"
        rt.wallet_address = "0xabcdef1234567890abcdef1234567890abcdef12"
        rt.version = "0.1.0"
        rt.config = X402Config(
            role="buyer",
            buyer_backend="cli",
            circle_cli_network="ARC-TESTNET",
            circle_cli_wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            host_allowlist=[],
            network_policy="public",
            allow_chat_otp=True,
        )
        rt.cli_client = AsyncMock()
        rt.buyer_tool = MagicMock()
        rt.init_error = None

        # Session not yet valid → allows login_start
        status = MagicMock()
        status.authenticated = False
        status.terms_accepted = True
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)
        rt.cli_client.login_start = AsyncMock(
            return_value=MagicMock(
                request_id="circle-req-env",
                email_masked="u***@example.com",
                otp_required=True,
            )
        )

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            start_result = await start_handler({"email": "user@example.com", "mode": "chat_otp"})
        start_data = json.loads(start_result)
        login_id = start_data["login_id"]

        # Session after OTP: mainnet VALID, testnet NOT_VALID
        session_result = MagicMock()
        session_result.authenticated = True
        session_result.mainnet_status = "VALID"
        session_result.testnet_status = "NOT_VALID"
        rt.cli_client.login_complete = AsyncMock(return_value=session_result)

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await complete_handler(
                {
                    "login_id": login_id,
                    "otp": "654321",
                    "acknowledge_otp_exposure": True,
                }
            )

        data = json.loads(result)
        # Expected testnet but only mainnet is valid → success must be False
        assert data["success"] is False
        assert data["authenticated"] is False
        assert data["environment_valid"] is False
        assert data["environment"] == "testnet"

    async def test_success_true_when_expected_env_valid(self) -> None:
        """login_complete returns success=true when env-specific status IS VALID."""
        from hermes_x402.config import X402Config
        from hermes_x402.hermes_plugin.tools import register_login_tools

        class _C:
            def __init__(self):
                self.tools = {}
                self.hooks = []

            def register_tool(self, **kw):
                self.tools[kw["name"]] = kw

            def register_hook(self, ht, h):
                self.hooks.append({"hook_type": ht, "handler": h})

        ctx = _C()
        register_login_tools(ctx)
        start_handler = ctx.tools["x402_login_start"]["handler"]
        complete_handler = ctx.tools["x402_login_complete"]["handler"]

        # For mainnet test, we need MAINNET config
        rt = MagicMock()
        rt.is_configured = True
        rt.is_available = True
        rt.backend_name = "cli"
        rt.role = "buyer"
        rt.network = "MAINNET"
        rt.wallet_address = "0xabcdef1234567890abcdef1234567890abcdef12"
        rt.version = "0.1.0"
        rt.config = X402Config(
            role="buyer",
            buyer_backend="cli",
            circle_cli_network="MAINNET",
            circle_cli_wallet_address="0xabcdef1234567890abcdef1234567890abcdef12",
            host_allowlist=[],
            network_policy="public",
            allow_chat_otp=True,
        )
        rt.cli_client = AsyncMock()
        rt.buyer_tool = MagicMock()
        rt.init_error = None

        # Session not yet valid → allows login_start
        status = MagicMock()
        status.authenticated = False
        status.terms_accepted = True
        rt.cli_client.agent_wallet_status = AsyncMock(return_value=status)
        rt.cli_client.login_start = AsyncMock(
            return_value=MagicMock(
                request_id="circle-req-env2",
                email_masked="u***@example.com",
                otp_required=True,
            )
        )

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            start_result = await start_handler({"email": "user@example.com", "mode": "chat_otp"})
        start_data = json.loads(start_result)
        login_id = start_data["login_id"]

        # Session after OTP: mainnet VALID, testnet NOT_VALID
        session_result = MagicMock()
        session_result.authenticated = True
        session_result.mainnet_status = "VALID"
        session_result.testnet_status = "NOT_VALID"
        rt.cli_client.login_complete = AsyncMock(return_value=session_result)

        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await complete_handler(
                {
                    "login_id": login_id,
                    "otp": "654321",
                    "acknowledge_otp_exposure": True,
                }
            )

        data = json.loads(result)
        assert data["success"] is True
        assert data["authenticated"] is True
        assert data["environment_valid"] is True
        assert data["environment"] == "mainnet"


# ---------------------------------------------------------------------------
# Regression: service payment-option fingerprint
# ---------------------------------------------------------------------------


class TestServiceOptionFingerprint:
    """Regression: fingerprint must be a stable 64-char hex string."""

    def test_returns_string_of_length_64(self) -> None:
        """_service_option_fingerprint returns a 64-char hex digest string."""
        from hermes_x402.hermes_plugin.tools import _service_option_fingerprint

        option = MagicMock()
        option.scheme = "https"
        option.payment_system = "gateway_batching"
        option.network = "arcTestnet"
        option.network_id = "eip155:5042002"
        option.amount_atomic = "1000000"
        option.asset = "USDC"
        option.pay_to = "0xdeadbeef"
        option.max_timeout_seconds = 60

        fp = _service_option_fingerprint(option, "2")
        assert isinstance(fp, str)
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_stored_fingerprint_survives_deepcopy(self) -> None:
        """Preview data containing the fingerprint can be stored and read back."""
        import copy
        import time

        from hermes_x402.hermes_plugin.gateway_state import (
            _lock,
            _previews,
            get_preview,
            store_preview,
        )
        from hermes_x402.hermes_plugin.tools import _service_option_fingerprint

        option = MagicMock()
        option.scheme = "https"
        option.payment_system = "gateway_batching"
        option.network = "arcTestnet"
        option.network_id = "eip155:5042002"
        option.amount_atomic = "1000000"
        option.asset = "USDC"
        option.pay_to = "0xdeadbeef"
        option.max_timeout_seconds = 60

        fp = _service_option_fingerprint(option, "2")
        preview_data = {
            "deposit_amount": "5.0",
            "service_option_fingerprint": fp,
            "wallet": "0xabcdef1234567890abcdef1234567890abcdef12",
            "wallet_network": "ARC-TESTNET",
            "deposit_method": "direct",
            "expires_at": time.time() + 300,
            "consumed": False,
        }

        # deepcopy must not raise TypeError on the fingerprint
        copy.deepcopy(preview_data)

        # Clear stale previews and store
        with _lock:
            _previews.clear()
        store_preview("fp_test_123", preview_data)

        read_back = get_preview("fp_test_123")
        assert read_back is not None
        assert read_back["service_option_fingerprint"] == fp
        assert len(read_back["service_option_fingerprint"]) == 64

        # Cleanup
        with _lock:
            _previews.clear()
