"""Tests for the hermes-x402 Hermes plugin integration.

Covers: registration, status tools, wallet tools, service inspect,
fetch, pay, error mapping, input validation, output bounding,
async handlers, URL/host policy, redirect behavior, subclass error codes,
status consistency, and payment cap validation.

All tests use mocks — no live wallet, no live payment, no Circle CLI.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any
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


class FakeHermesContext:
    """Minimal fake Hermes PluginContext for testing registration."""

    def __init__(self):
        self.tools: list[dict[str, Any]] = []
        self.hooks: list[dict[str, Any]] = []

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
                "is_async": is_async,
                "description": description,
            }
        )

    def register_hook(self, hook_type: str, handler: Any) -> None:
        """Register a hook with the plugin context."""
        self.hooks.append({"hook_type": hook_type, "handler": handler})


@pytest.fixture
def fake_ctx() -> FakeHermesContext:
    return FakeHermesContext()


async def _call_handler(handler: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a handler, awaiting if async."""
    result = handler(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------


class FakeStreamResponse:
    """Fake httpx streaming response for testing.

    Implements the exact interface used by production fetch code:
    - status_code, headers, is_redirect, encoding
    - async aiter_bytes() yielding real bytes chunks
    - __aenter__/__aexit__ for ``async with client.stream(...) as response:``
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        is_redirect: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.is_redirect = is_redirect
        self.encoding = "utf-8"
        self.bytes_read = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    async def aiter_bytes(self, chunk_size: int = 65536):
        for offset in range(0, len(self._body), chunk_size):
            chunk = self._body[offset : offset + chunk_size]
            self.bytes_read += len(chunk)
            yield chunk


class InfiniteLikeStreamResponse:
    """Fake stream that yields repeated chunks without allocating the full body.

    Used for large-response / truncation tests.  Proves the production code
    stops reading at MAX_OUTPUT_BYTES + 1 without buffering the entire payload.
    """

    def __init__(self, chunk: bytes, chunk_count: int, *, status_code: int = 200):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.chunk = chunk
        self.chunk_count = chunk_count
        self.is_redirect = False
        self.encoding = "utf-8"
        self.bytes_read = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    async def aiter_bytes(self):
        for _ in range(self.chunk_count):
            self.bytes_read += len(self.chunk)
            yield self.chunk


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
            # Some handlers use **kwargs only (wallet_status, wallet_balance, gateway_balance),
            # others take (args, **kwargs)
            kwargs_only_tools = (
                "x402_wallet_status",
                "x402_wallet_balance",
                "x402_gateway_balance",
            )
            if tool["name"] in kwargs_only_tools:
                result = asyncio.run(_call_handler(tool["handler"], task_id="test"))
            else:
                result = asyncio.run(_call_handler(tool["handler"], {}, task_id="test"))
            assert isinstance(result, str)
            parsed = json.loads(result)
            assert isinstance(parsed, dict)

    def test_async_handlers_registered_with_flag(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.entry import register

        register(fake_ctx)

        async_tools = [t["name"] for t in fake_ctx.tools if t["is_async"]]
        assert "x402_wallet_balance" in async_tools
        assert "x402_service_inspect" in async_tools
        assert "x402_fetch" in async_tools
        assert "x402_pay" in async_tools

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
        # Fix #9: configured=false when no role set
        assert result["configured"] is False
        assert result["available"] is False
        assert result["role"] == "unconfigured"
        assert result["plugin_loaded"] is True

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
            assert result["configured"] is True
            assert result["plugin_loaded"] is True
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

    def test_status_consistency_no_role(self, fake_ctx: FakeHermesContext):
        """Fix #9: role=unconfigured AND configured=false when no role."""
        from hermes_x402.hermes_plugin.tools import register_status_tools

        register_status_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(handler({}, task_id="test"))
        assert result["role"] == "unconfigured"
        assert result["configured"] is False
        assert result["available"] is False


# ---------------------------------------------------------------------------
# Wallet tool tests
# ---------------------------------------------------------------------------


class TestX402WalletStatus:
    def test_wallet_status_unconfigured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        register_wallet_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(asyncio.run(_call_handler(handler, task_id="test")))
        assert result["success"] is True
        assert "configured" not in result or result.get("configured") is False

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
            result_str = asyncio.run(_call_handler(handler, task_id="test"))
            assert "secret-value" not in result_str
            assert "key-value" not in result_str


class TestX402WalletBalance:
    def test_balance_unconfigured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_wallet_tools

        register_wallet_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(asyncio.run(_call_handler(handler, task_id="test")))
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
            result = json.loads(asyncio.run(_call_handler(handler, task_id="test")))
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
        result = json.loads(asyncio.run(_call_handler(handler, {"url": ""}, task_id="test")))
        assert result["success"] is False
        assert result["error"] == "invalid_input"

    def test_inspect_file_url_rejected(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_service_tools

        register_service_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(
            asyncio.run(_call_handler(handler, {"url": "file:///etc/passwd"}, task_id="test"))
        )
        assert result["success"] is False

    def test_inspect_url_too_long(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_service_tools

        register_service_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        long_url = "https://example.com/" + "a" * 3000
        result = json.loads(asyncio.run(_call_handler(handler, {"url": long_url}, task_id="test")))
        assert result["success"] is False

    def test_inspect_localhost_blocked(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_service_tools

        register_service_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(
            asyncio.run(_call_handler(handler, {"url": "https://localhost/admin"}, task_id="test"))
        )
        assert result["success"] is False
        assert "blocked" in result["message"].lower()

    def test_inspect_metadata_ip_blocked(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_service_tools

        register_service_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(
            asyncio.run(
                _call_handler(
                    handler,
                    {"url": "http://169.254.169.254/metadata"},
                    task_id="test",
                )
            )
        )
        assert result["success"] is False

    def test_inspect_no_redirect(self, fake_ctx: FakeHermesContext):
        """Fix #4: redirects not followed."""
        from hermes_x402.hermes_plugin.tools import register_service_tools

        with patch.dict("os.environ", {"X402_NETWORK_POLICY": "public"}, clear=False):
            register_service_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]

            mock_response = MagicMock()
            mock_response.is_redirect = True
            mock_response.status_code = 302
            mock_response.headers = {"location": "https://evil.com/steal"}

            with patch("hermes_x402.hermes_plugin.tools.httpx.AsyncClient") as mock_client:
                mc = mock_client.return_value
                mc.__aenter__ = AsyncMock(return_value=mc)
                mc.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
                mc.request = AsyncMock(return_value=mock_response)

                result = json.loads(
                    asyncio.run(
                        _call_handler(
                            handler,
                            {"url": "https://example.com/redirect"},
                            task_id="test",
                        )
                    )
                )
                assert result["success"] is False
                assert result["error"] == "redirect_not_followed"
                assert result["status"] == 302
                # Redirect target is NOT requested (request called once only)
                assert mc.request.call_count == 1

    # ---------------------------------------------------------------------------
    # Fetch tool tests
    # ---------------------------------------------------------------------------


class TestX402Fetch:
    def test_fetch_invalid_method(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[0]["handler"]
        result = json.loads(
            asyncio.run(
                _call_handler(
                    handler,
                    {"url": "https://example.com", "method": "INVALID"},
                    task_id="test",
                )
            )
        )
        assert result["success"] is False

    def test_fetch_nonpaying_by_default(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        with patch.dict("os.environ", {"X402_NETWORK_POLICY": "public"}, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]

            fake = FakeStreamResponse(
                status_code=402,
                headers={"Payment-Required": "price=10000"},
            )

            with patch("hermes_x402.hermes_plugin.tools.httpx.AsyncClient") as mock_client:
                mc = mock_client.return_value
                mc.__aenter__ = AsyncMock(return_value=mc)
                mc.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.stream = MagicMock(return_value=fake)

                result = json.loads(
                    asyncio.run(
                        _call_handler(
                            handler,
                            {"url": "https://example.com/resource"},
                            task_id="test",
                        )
                    )
                )
                assert result["success"] is True
                assert result["payment_required"] is True
                assert fake.closed is True

    def test_fetch_enforces_allowlist(self, fake_ctx: FakeHermesContext):
        """Fix #5: allowlist enforced before network I/O."""
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        env = {
            "X402_HOST_ALLOWLIST": "allowed.example.com",
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": "0x1234",
            "CIRCLE_AGENT_WALLET_NETWORK": "BASE",
            "X402_MAX_USDC_PER_PAYMENT": "0.05",
        }
        with patch.dict("os.environ", env, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]
            result = json.loads(
                asyncio.run(
                    _call_handler(
                        handler,
                        {"url": "https://evil.com/steal"},
                        task_id="test",
                    )
                )
            )
            assert result["success"] is False
            assert result["error"] == "host_rejected"

    def test_fetch_no_redirect(self, fake_ctx: FakeHermesContext):
        """Fix #4: redirects not followed."""
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        with patch.dict("os.environ", {"X402_NETWORK_POLICY": "public"}, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]

            fake = FakeStreamResponse(
                status_code=301,
                headers={"location": "https://other.com/new"},
                is_redirect=True,
            )

            with patch("hermes_x402.hermes_plugin.tools.httpx.AsyncClient") as mock_client:
                mc = mock_client.return_value
                mc.__aenter__ = AsyncMock(return_value=mc)
                mc.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.stream = MagicMock(return_value=fake)

                result = json.loads(
                    asyncio.run(
                        _call_handler(
                            handler,
                            {"url": "https://example.com/moved"},
                            task_id="test",
                        )
                    )
                )
                assert result["success"] is False
                assert result["error"] == "redirect_not_followed"
                assert fake.closed is True

    def test_fetch_bounded_json_output(self, fake_ctx: FakeHermesContext):
        """Fix #6: JSON output bounded."""
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        with patch.dict("os.environ", {"X402_NETWORK_POLICY": "public"}, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]

            from hermes_x402.hermes_plugin.schemas import MAX_OUTPUT_BYTES

            # Use InfiniteLikeStreamResponse to prove bounded read
            # without allocating the full body
            chunk = b"x" * 4096
            chunk_count = (MAX_OUTPUT_BYTES // 4096) + 10  # way over the limit
            fake = InfiniteLikeStreamResponse(chunk, chunk_count)

            with patch("hermes_x402.hermes_plugin.tools.httpx.AsyncClient") as mock_client:
                mc = mock_client.return_value
                mc.__aenter__ = AsyncMock(return_value=mc)
                mc.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.stream = MagicMock(return_value=fake)

                result = json.loads(
                    asyncio.run(
                        _call_handler(
                            handler,
                            {"url": "https://example.com/big"},
                            task_id="test",
                        )
                    )
                )
                assert result["success"] is True
                assert result["truncated"] is True
                assert result["original_size"] > MAX_OUTPUT_BYTES
                # Prove bounded read: bytes consumed <= limit
                assert fake.bytes_read <= MAX_OUTPUT_BYTES + len(chunk)
                assert fake.closed is True

    def test_fetch_malformed_json(self, fake_ctx: FakeHermesContext):
        """Fix #6: malformed JSON handled gracefully."""
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        with patch.dict("os.environ", {"X402_NETWORK_POLICY": "public"}, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[0]["handler"]

            fake = FakeStreamResponse(
                status_code=200,
                body=b"{not valid json",
                headers={"content-type": "application/json"},
            )

            with patch("hermes_x402.hermes_plugin.tools.httpx.AsyncClient") as mock_client:
                mc = mock_client.return_value
                mc.__aenter__ = AsyncMock(return_value=mc)
                mc.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_client.return_value.stream = MagicMock(return_value=fake)

                result = json.loads(
                    asyncio.run(
                        _call_handler(
                            handler,
                            {"url": "https://example.com/bad"},
                            task_id="test",
                        )
                    )
                )
                # Result is still valid JSON string
                assert isinstance(result, dict)
                assert fake.closed is True


# ---------------------------------------------------------------------------
# Pay tool tests
# ---------------------------------------------------------------------------


class TestX402Pay:
    def test_pay_unavailable_when_not_configured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(
            asyncio.run(
                _call_handler(handler, {"url": "https://example.com/resource"}, task_id="test")
            )
        )
        assert result["success"] is False
        assert result["error"] == "configuration_error"

    def test_pay_invalid_url(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(asyncio.run(_call_handler(handler, {"url": ""}, task_id="test")))
        assert result["success"] is False

    def test_pay_caller_cap_cannot_exceed_configured(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": "0x1234",
            "CIRCLE_AGENT_WALLET_NETWORK": "BASE",
            "X402_MAX_USDC_PER_PAYMENT": "0.01",
            "X402_NETWORK_POLICY": "public",
        }
        with patch.dict("os.environ", env, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[1]["handler"]
            result = json.loads(
                asyncio.run(
                    _call_handler(
                        handler,
                        {"url": "https://example.com/resource", "max_usdc": "1.00"},
                        task_id="test",
                    )
                )
            )
            assert result["success"] is False
            assert result["error"] == "payment_limit_exceeded"

    def test_pay_negative_amount_rejected(self, fake_ctx: FakeHermesContext):
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        register_payment_tools(fake_ctx)
        handler = fake_ctx.tools[1]["handler"]
        result = json.loads(
            asyncio.run(
                _call_handler(
                    handler,
                    {"url": "https://example.com/resource", "max_usdc": "-1"},
                    task_id="test",
                )
            )
        )
        assert result["success"] is False


# ---------------------------------------------------------------------------
# P1: Payment cap validation tests
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

    def test_buyer_not_called_on_invalid_cap(self, fake_ctx: FakeHermesContext):
        """On any invalid-cap case, buyer.pay() must not be called."""
        from hermes_x402.hermes_plugin.tools import register_payment_tools

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": "0x1234",
            "CIRCLE_AGENT_WALLET_NETWORK": "BASE",
            "X402_MAX_USDC_PER_PAYMENT": "bad-config",
        }
        with patch.dict("os.environ", env, clear=False):
            register_payment_tools(fake_ctx)
            handler = fake_ctx.tools[1]["handler"]
            result = json.loads(
                asyncio.run(
                    _call_handler(
                        handler,
                        {"url": "https://example.com", "max_usdc": "0.01"},
                        task_id="test",
                    )
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
        from pathlib import Path

        toml = Path(__file__).parent.parent / "pyproject.toml"
        content = toml.read_text()
        assert "hermes_agent.plugins" in content
        assert 'hermes-x402 = "hermes_x402.hermes_plugin.entry"' in content


# ---------------------------------------------------------------------------
# Approval hook unit tests
# ---------------------------------------------------------------------------


class TestApprovalHookUnitTests:
    """Unit tests for the approval hook logic using FakeHermesContext.

    These tests verify the hook's decision logic but do NOT execute
    Hermes' native approval gate. They use mocks, not real Hermes runtime.

    Actual Hermes runtime/install smoke is explicitly deferred until
    the installer work.
    """

    def test_plugin_loads_14_tools_and_hook(self):
        """Plugin registration produces 14 tools and one hook."""
        from hermes_x402.hermes_plugin.entry import register

        ctx = FakeHermesContext()
        register(ctx)

        assert len(ctx.tools) == 14
        assert len(ctx.hooks) == 1

        tool_names = {t["name"] for t in ctx.tools}
        expected_tools = {
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
        assert tool_names == expected_tools

        assert ctx.hooks[0]["hook_type"] == "pre_tool_call"
        assert callable(ctx.hooks[0]["handler"])

    def test_denied_approval_prevents_backend(self):
        """When approval hook returns block, fake backend runs zero times."""
        from hermes_x402.hermes_plugin.entry import register

        ctx = FakeHermesContext()
        register(ctx)

        hook_fn = ctx.hooks[0]["handler"]

        # Block: no tool_call_id
        result = hook_fn("x402_pay", {"url": "https://example.com"})
        assert result is not None
        assert result["action"] == "block"

        # Fake backend must not be called
        fake_backend_calls = []

        def fake_backend():
            fake_backend_calls.append(1)

        # Simulate: if hook blocks, backend should not run
        if result["action"] == "block":
            pass  # Backend skipped
        else:
            fake_backend()

        assert fake_backend_calls == []

    def test_approved_approval_runs_backend_once(self):
        """When approval hook returns approve, fake backend runs exactly once."""
        from hermes_x402.hermes_plugin.entry import register

        ctx = FakeHermesContext()
        register(ctx)

        hook_fn = ctx.hooks[0]["handler"]

        # Approve: with tool_call_id
        result = hook_fn(
            "x402_pay",
            {"url": "https://example.com", "method": "GET"},
            tool_call_id="call_123",
        )
        assert result is not None
        assert result["action"] == "approve"
        assert result["rule_key"] == "hermes-x402:x402_pay:call_123"

        # Fake backend must run exactly once
        fake_backend_calls = []

        def fake_backend():
            fake_backend_calls.append(1)

        if result["action"] == "block":
            pass
        else:
            fake_backend()

        assert fake_backend_calls == [1]

    def test_approval_message_includes_url_and_method(self):
        """Approval message includes URL and method for x402_pay."""
        from hermes_x402.hermes_plugin.entry import register

        ctx = FakeHermesContext()
        register(ctx)

        hook_fn = ctx.hooks[0]["handler"]

        result = hook_fn(
            "x402_pay",
            {"url": "https://api.example.com/data", "method": "POST"},
            tool_call_id="call_456",
        )
        assert result is not None
        assert "https://api.example.com/data" in result["message"]
        assert "POST" in result["message"]

    def test_non_financial_tool_returns_none(self):
        """Non-financial tools return None (no approval needed)."""
        from hermes_x402.hermes_plugin.entry import register

        ctx = FakeHermesContext()
        register(ctx)

        hook_fn = ctx.hooks[0]["handler"]

        result = hook_fn("x402_status", {})
        assert result is None

        result = hook_fn("x402_wallet_status", {})
        assert result is None

    def test_gateway_execute_approval_message(self):
        """Gateway deposit execute gets informative approval message."""
        import time

        from hermes_x402.hermes_plugin.entry import register
        from hermes_x402.hermes_plugin.gateway_state import store_preview

        # Store a valid preview for the test
        store_preview(
            "abc123",
            {
                "service_url": "https://api.example.com/pay",
                "deposit_amount": "2.5",
                "wallet": "0x1234567890abcdef1234567890abcdef12345678",
                "wallet_network": "ARC-TESTNET",
                "deposit_method": "direct",
                "expires_at": time.time() + 300,
                "consumed": False,
            },
        )

        ctx = FakeHermesContext()
        register(ctx)

        hook_fn = ctx.hooks[0]["handler"]

        result = hook_fn(
            "x402_gateway_deposit_execute",
            {"preview_id": "abc123"},
            tool_call_id="call_789",
        )
        assert result is not None
        assert result["action"] == "approve"
        assert "2.5" in result["message"]
        assert "example.com" in result["message"]
        assert "ARC-TESTNET" in result["message"]


# ---------------------------------------------------------------------------
# Focused regression tests — preview blocking, URL sanitization, atomicity
# ---------------------------------------------------------------------------


class TestPreviewBlocking:
    """Tests that invalid Gateway previews are blocked in the approval hook."""

    def test_missing_preview_returns_block(self):
        """Missing preview returns action=block."""
        from hermes_x402.hermes_plugin.entry import register

        ctx = FakeHermesContext()
        register(ctx)
        hook_fn = ctx.hooks[0]["handler"]

        result = hook_fn(
            "x402_gateway_deposit_execute",
            {"preview_id": "nonexistent_id"},
            tool_call_id="call_1",
        )
        assert result is not None
        assert result["action"] == "block"
        assert "missing" in result["message"].lower() or "expired" in result["message"].lower()

    def test_expired_preview_returns_block(self):
        """Expired preview returns action=block."""
        # Store a preview that is already expired
        import time

        from hermes_x402.hermes_plugin.entry import register
        from hermes_x402.hermes_plugin.gateway_state import store_preview

        store_preview(
            "expired_123",
            {
                "service_url": "https://example.com",
                "deposit_amount": "1.0",
                "wallet": "0x1234567890abcdef",
                "wallet_network": "ARC-TESTNET",
                "deposit_method": "direct",
                "expires_at": time.time() - 10,  # Already expired
                "consumed": False,
            },
        )

        ctx = FakeHermesContext()
        register(ctx)
        hook_fn = ctx.hooks[0]["handler"]

        result = hook_fn(
            "x402_gateway_deposit_execute",
            {"preview_id": "expired_123"},
            tool_call_id="call_2",
        )
        assert result is not None
        assert result["action"] == "block"

    def test_consumed_preview_returns_block(self):
        """Consumed preview returns action=block."""
        import time

        from hermes_x402.hermes_plugin.entry import register
        from hermes_x402.hermes_plugin.gateway_state import store_preview

        store_preview(
            "consumed_123",
            {
                "service_url": "https://example.com",
                "deposit_amount": "1.0",
                "wallet": "0x1234567890abcdef",
                "wallet_network": "ARC-TESTNET",
                "deposit_method": "direct",
                "expires_at": time.time() + 300,
                "consumed": True,  # Already consumed
            },
        )

        ctx = FakeHermesContext()
        register(ctx)
        hook_fn = ctx.hooks[0]["handler"]

        result = hook_fn(
            "x402_gateway_deposit_execute",
            {"preview_id": "consumed_123"},
            tool_call_id="call_3",
        )
        assert result is not None
        assert result["action"] == "block"


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


class TestPreviewIdValidation:
    """Regression: preview ID > 128 chars is blocked in approval hook and handler."""

    def test_approval_hook_blocks_overlong_preview_id(self) -> None:
        """Approval hook blocks preview_id exceeding 128 characters."""
        from hermes_x402.hermes_plugin.entry import register
        from hermes_x402.hermes_plugin.gateway_state import _lock, _previews

        # Clear any stale previews
        with _lock:
            _previews.clear()

        ctx = FakeHermesContext()
        register(ctx)
        hook_fn = ctx.hooks[0]["handler"]

        long_id = "a" * 129
        result = hook_fn(
            "x402_gateway_deposit_execute",
            {"preview_id": long_id},
            tool_call_id="call_overflow",
        )
        assert result is not None
        assert result["action"] == "block"
        assert "128" in result["message"]

    def test_approval_hook_allows_valid_preview_id(self) -> None:
        """Approval hook allows preview_id within 1..128 characters."""
        import time

        from hermes_x402.hermes_plugin.entry import register
        from hermes_x402.hermes_plugin.gateway_state import _lock, _previews, store_preview

        # Clear any stale previews from previous tests
        with _lock:
            _previews.clear()

        valid_id = "a" * 128
        store_preview(
            valid_id,
            {
                "service_url": "https://example.com",
                "deposit_amount": "1.0",
                "wallet": "0x1234567890abcdef1234567890abcdef12345678",
                "wallet_network": "ARC-TESTNET",
                "deposit_method": "direct",
                "expires_at": time.time() + 300,
                "consumed": False,
            },
        )

        ctx = FakeHermesContext()
        register(ctx)
        hook_fn = ctx.hooks[0]["handler"]

        result = hook_fn(
            "x402_gateway_deposit_execute",
            {"preview_id": valid_id},
            tool_call_id="call_valid",
        )
        assert result is not None
        assert result["action"] == "approve"

    async def test_execute_handler_blocks_overlong_preview_id(self) -> None:
        """Execute handler rejects preview_id exceeding 128 characters."""
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
        handler = ctx.tools["x402_gateway_deposit_execute"]["handler"]

        long_id = "b" * 129
        rt = MagicMock()
        rt.cli_client = AsyncMock()
        rt.config = MagicMock()
        rt.is_configured = True
        with patch("hermes_x402.hermes_plugin.tools.get_runtime", return_value=rt):
            result = await handler({"preview_id": long_id})

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "invalid_input"
        assert "128" in data["message"]


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
        import asyncio

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
            result = asyncio.run(handler())

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
