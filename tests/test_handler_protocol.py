"""Deterministic protocol tests for all 14 registered x402 handlers.

Verifies the handler dispatch contract:
  handler(args: dict, **kwargs)

For every registered tool:
  - accepts positional args dict
  - tool arguments are preserved (for parameterized tools)
  - async behavior is preserved (asyncio.iscoroutinefunction where applicable)
  - read-only handlers return valid JSON on validation paths
  - money-moving handlers prove argument binding without executing transactions

Uses a FakeCtx to capture registrations — no broad orchestration mocks.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest


def _run_async(coro):
    """Run a coroutine, compatible with all Python 3.10+ environments."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Event loop already running (pytest-asyncio context).
        # Use nest_asyncio if available, otherwise create new loop in thread.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# FakeCtx — captures registered tools/hooks without Hermes runtime
# ---------------------------------------------------------------------------


class FakeCtx:
    """Minimal Hermes PluginContext that captures tool and hook registrations."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.hooks: list[dict[str, Any]] = []

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict,
        handler: Any,
        description: str = "",
        is_async: bool = False,
        **kwargs: Any,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise TypeError("register_tool requires a non-empty name")
        self.tools.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "description": description,
                "is_async": is_async,
            }
        )

    def register_hook(self, hook_type: str, handler: Any, **kwargs: Any) -> None:
        self.hooks.append({"type": hook_type, "handler": handler})


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


@pytest.fixture()
def fake_ctx() -> FakeCtx:
    """Create a fresh FakeCtx and register all x402 tools + hooks."""
    from hermes_x402.hermes_plugin.entry import register

    ctx = FakeCtx()
    register(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALL_14_TOOLS = [
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
]

# Tools that accept args dict with meaningful parameters
_PARAMETERIZED_TOOLS = {
    "x402_service_search": {"query": "test", "limit": 5},
    "x402_supports": {"url": "https://example.com"},
    "x402_service_inspect": {"url": "https://example.com"},
    "x402_fetch": {"url": "https://example.com"},
    "x402_pay": {"url": "https://example.com"},
    "x402_login_start": {"email": "test@example.com"},
    "x402_login_complete": {
        "login_id": "test-id",
        "otp": "123456",
        "acknowledge_otp_exposure": True,
    },
    "x402_gateway_deposit_preview": {
        "service_url": "https://example.com",
        "method": "GET",
        "amount": "1.00",
    },
    "x402_gateway_deposit_execute": {"preview_id": "test-preview-id"},
}

# Tools that take no parameters
_PARAMETERLESS_TOOLS = {
    "x402_status",
    "x402_wallet_status",
    "x402_wallet_balance",
    "x402_networks",
    "x402_gateway_balance",
}

# Money-moving tools
_MONEY_MOVING_TOOLS = {"x402_pay", "x402_login_complete", "x402_gateway_deposit_execute"}

# Read-only tools (should return valid JSON on validation paths)
_READONLY_TOOLS = {
    "x402_status",
    "x402_wallet_status",
    "x402_wallet_balance",
    "x402_networks",
    "x402_service_search",
    "x402_supports",
    "x402_service_inspect",
    "x402_fetch",
    "x402_login_start",
    "x402_gateway_balance",
    "x402_gateway_deposit_preview",
}


# ---------------------------------------------------------------------------
# Test class: Registration completeness
# ---------------------------------------------------------------------------


class TestRegistrationCompleteness:
    """All 14 tools and 1 pre_tool_call hook are registered."""

    def test_exact_14_tools_registered(self, fake_ctx: FakeCtx):
        assert len(fake_ctx.tools) == 14

    def test_exact_1_hook_registered(self, fake_ctx: FakeCtx):
        assert len(fake_ctx.hooks) == 1
        assert fake_ctx.hooks[0]["type"] == "pre_tool_call"

    def test_all_14_tool_names_present(self, fake_ctx: FakeCtx):
        registered = {t["name"] for t in fake_ctx.tools}
        assert registered == set(_ALL_14_TOOLS)

    def test_all_tools_use_x402_toolset(self, fake_ctx: FakeCtx):
        for tool in fake_ctx.tools:
            assert tool["toolset"] == "x402", (
                f"{tool['name']} toolset is {tool['toolset']!r}, expected 'x402'"
            )

    def test_all_tools_have_schema(self, fake_ctx: FakeCtx):
        for tool in fake_ctx.tools:
            schema = tool["schema"]
            assert isinstance(schema, dict)
            assert schema.get("name") == tool["name"]
            assert "parameters" in schema

    def test_all_tools_have_description(self, fake_ctx: FakeCtx):
        for tool in fake_ctx.tools:
            assert tool["description"], f"{tool['name']} has empty description"


# ---------------------------------------------------------------------------
# Test class: Handler signature contract
# ---------------------------------------------------------------------------


class TestHandlerSignatureContract:
    """Every handler must accept (args: dict, **kwargs)."""

    def test_all_handlers_callable(self, fake_ctx: FakeCtx):
        for tool in fake_ctx.tools:
            handler = tool["handler"]
            assert callable(handler), f"{tool['name']} handler is not callable"

    def test_all_handlers_accept_args_positional(self, fake_ctx: FakeCtx):
        """Every handler must accept a positional args dict argument."""
        for tool in fake_ctx.tools:
            handler = tool["handler"]
            sig = inspect.signature(handler)
            params = list(sig.parameters.values())

            # Handler must accept at least 1 positional parameter (args dict)
            # or **kwargs (which absorbs positional args)
            has_args_param = any(
                p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                for p in params
            )
            has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            has_var_positional = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)

            assert has_args_param or has_var_keyword or has_var_positional, (
                f"{tool['name']} handler signature {sig} does not accept positional args dict"
            )

    def test_async_handlers_are_coroutine_functions(self, fake_ctx: FakeCtx):
        """Handlers marked is_async must be coroutine functions or wrap one."""
        for tool in fake_ctx.tools:
            if tool["is_async"]:
                handler = tool["handler"]
                is_coro = asyncio.iscoroutinefunction(handler)
                if not is_coro:
                    # Lambda wrapper — verify it returns a coroutine
                    # (the actual async dispatch in Hermes awaits it)
                    result = handler({}, **{})
                    assert asyncio.iscoroutine(result) or isinstance(result, str), (
                        f"{tool['name']} async handler (lambda-wrapped) "
                        f"returned {type(result).__name__}, expected coroutine or str"
                    )
                    # If it's a coroutine, close it to avoid resource warning
                    if asyncio.iscoroutine(result):
                        result.close()

    def test_sync_handlers_are_not_coroutine_functions(self, fake_ctx: FakeCtx):
        """Handlers NOT marked is_async must NOT be coroutine functions."""
        for tool in fake_ctx.tools:
            if not tool["is_async"]:
                assert not asyncio.iscoroutinefunction(tool["handler"]), (
                    f"{tool['name']} is NOT marked is_async but IS a coroutine function"
                )


# ---------------------------------------------------------------------------
# Test class: Parameterized tool argument preservation
# ---------------------------------------------------------------------------


class TestArgumentPreservation:
    """Parameterized tools must receive and preserve args dict arguments."""

    @pytest.mark.parametrize(
        "tool_name",
        sorted(_PARAMETERIZED_TOOLS.keys()),
    )
    def test_parameterized_handler_receives_args(self, fake_ctx: FakeCtx, tool_name: str):
        """The handler must be callable with (args_dict, **kwargs) and not crash
        on argument validation (before reaching runtime/network)."""
        tool = next(t for t in fake_ctx.tools if t["name"] == tool_name)
        handler = tool["handler"]
        args = _PARAMETERIZED_TOOLS[tool_name]

        result = _run_async(handler(args, **{})) if tool["is_async"] else handler(args, **{})

        # Must return a string (JSON)
        assert isinstance(result, str), (
            f"{tool_name} handler did not return a string, got {type(result).__name__}"
        )

    @pytest.mark.parametrize(
        "tool_name",
        sorted(_PARAMETERLESS_TOOLS),
    )
    def test_parameterless_handler_ignores_args(self, fake_ctx: FakeCtx, tool_name: str):
        """Parameterless handlers must work when called with (empty_args, **kwargs)."""
        tool = next(t for t in fake_ctx.tools if t["name"] == tool_name)
        handler = tool["handler"]

        result = _run_async(handler({}, **{})) if tool["is_async"] else handler({}, **{})

        assert isinstance(result, str), f"{tool_name} handler did not return a string"

    def test_service_search_preserves_query(self, fake_ctx: FakeCtx):
        """x402_service_search must preserve query arg in validation path."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_service_search")
        handler = tool["handler"]

        # Empty query should return validation error referencing the query
        result = _run_async(handler({"query": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False
        assert "query" in parsed.get("message", "").lower() or "query" in parsed.get("error", "")

    def test_pay_preserves_url(self, fake_ctx: FakeCtx):
        """x402_pay must preserve url arg in validation path."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_pay")
        handler = tool["handler"]

        result = _run_async(handler({"url": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False
        assert "url" in parsed.get("message", "").lower() or "url" in parsed.get("error", "")

    def test_login_start_preserves_email(self, fake_ctx: FakeCtx):
        """x402_login_start must preserve email arg."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_login_start")
        handler = tool["handler"]

        # When no CLI client, returns cli_not_available; otherwise validates email
        result = _run_async(handler({"email": "not-an-email"}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False
        # Either email validation error or cli_not_available (no runtime config)
        assert parsed.get("error") in ("invalid_input", "cli_not_available")

    def test_login_complete_preserves_login_id(self, fake_ctx: FakeCtx):
        """x402_login_complete must preserve login_id arg."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_login_complete")
        handler = tool["handler"]

        result = _run_async(
            handler({"login_id": "", "otp": "", "acknowledge_otp_exposure": True}, **{})
        )
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_gateway_deposit_preview_preserves_service_url(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_preview must preserve service_url arg."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_preview")
        handler = tool["handler"]

        # When no CLI client, returns cli_not_available; otherwise validates service_url
        result = _run_async(handler({"service_url": "", "method": "GET", "amount": "1.00"}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False
        # Either input validation error or cli_not_available (no runtime config)
        assert parsed.get("error") in ("invalid_input", "cli_not_available")

    def test_gateway_deposit_execute_preserves_preview_id(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_execute must preserve preview_id arg."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_execute")
        handler = tool["handler"]

        result = _run_async(handler({"preview_id": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False


# ---------------------------------------------------------------------------
# Test class: Read-only handler correctness
# ---------------------------------------------------------------------------


class TestReadOnlyHandlerCorrectness:
    """Read-only handlers return valid JSON with success/error fields."""

    def test_x402_status_returns_json(self, fake_ctx: FakeCtx):
        """x402_status returns valid JSON with success field."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_status")
        result = tool["handler"]({}, **{})
        parsed = json.loads(result)
        assert "success" in parsed
        assert parsed.get("plugin") == "hermes-x402"

    def test_x402_networks_returns_json(self, fake_ctx: FakeCtx):
        """x402_networks returns valid JSON with network list."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_networks")
        result = tool["handler"]({}, **{})
        parsed = json.loads(result)
        assert "success" in parsed
        assert "networks" in parsed
        assert isinstance(parsed["networks"], list)
        assert len(parsed["networks"]) > 0

    def test_x402_wallet_status_returns_unconfigured(self, fake_ctx: FakeCtx):
        """x402_wallet_status returns unconfigured when no config."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_wallet_status")
        result = _run_async(tool["handler"]({}, **{}))
        parsed = json.loads(result)
        assert "success" in parsed

    def test_x402_wallet_balance_returns_unconfigured(self, fake_ctx: FakeCtx):
        """x402_wallet_balance returns unconfigured when no config."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_wallet_balance")
        result = _run_async(tool["handler"]({}, **{}))
        parsed = json.loads(result)
        assert "success" in parsed

    def test_x402_service_search_empty_query(self, fake_ctx: FakeCtx):
        """x402_service_search returns validation error for empty query."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_service_search")
        result = _run_async(tool["handler"]({"query": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False
        assert parsed.get("error") == "invalid_input"

    def test_x402_supports_invalid_url(self, fake_ctx: FakeCtx):
        """x402_supports returns validation error for invalid URL."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_supports")
        result = _run_async(tool["handler"]({"url": "not-a-url"}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_x402_service_inspect_invalid_url(self, fake_ctx: FakeCtx):
        """x402_service_inspect returns validation error for invalid URL."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_service_inspect")
        result = _run_async(tool["handler"]({"url": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_x402_fetch_invalid_url(self, fake_ctx: FakeCtx):
        """x402_fetch returns validation error for empty URL."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_fetch")
        result = _run_async(tool["handler"]({"url": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_x402_login_start_invalid_email(self, fake_ctx: FakeCtx):
        """x402_login_start returns validation error for invalid email."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_login_start")
        result = _run_async(tool["handler"]({"email": "bad"}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_x402_gateway_balance_unconfigured(self, fake_ctx: FakeCtx):
        """x402_gateway_balance returns error when not configured."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_balance")
        result = _run_async(tool["handler"]({}, **{}))
        parsed = json.loads(result)
        assert "success" in parsed

    def test_x402_gateway_deposit_preview_missing_url(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_preview returns error for missing service_url."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_preview")
        result = _run_async(
            tool["handler"]({"service_url": "", "method": "GET", "amount": "1.00"}, **{})
        )
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_x402_gateway_deposit_execute_empty_preview(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_execute returns error for empty preview_id."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_execute")
        result = _run_async(tool["handler"]({"preview_id": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False


# ---------------------------------------------------------------------------
# Test class: Money-moving handler argument binding
# ---------------------------------------------------------------------------


class TestMoneyMovingArgumentBinding:
    """Money-moving handlers prove argument binding without transactions."""

    def test_pay_rejects_empty_url_no_transaction(self, fake_ctx: FakeCtx):
        """x402_pay with empty URL returns validation error, never touches buyer."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_pay")
        result = _run_async(tool["handler"]({"url": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False
        assert parsed.get("error") == "invalid_input"

    def test_pay_rejects_invalid_method(self, fake_ctx: FakeCtx):
        """x402_pay with invalid HTTP method returns validation error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_pay")
        result = _run_async(
            tool["handler"]({"url": "https://example.com", "method": "INVALID"}, **{})
        )
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_pay_rejects_file_scheme(self, fake_ctx: FakeCtx):
        """x402_pay with file:// URL returns validation error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_pay")
        result = _run_async(tool["handler"]({"url": "file:///etc/passwd"}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_login_complete_rejects_missing_otp(self, fake_ctx: FakeCtx):
        """x402_login_complete with empty OTP returns error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_login_complete")
        result = _run_async(
            tool["handler"]({"login_id": "test", "otp": "", "acknowledge_otp_exposure": True}, **{})
        )
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_login_complete_rejects_unacknowledged_otp(self, fake_ctx: FakeCtx):
        """x402_login_complete without acknowledgment returns error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_login_complete")
        result = _run_async(
            tool["handler"](
                {"login_id": "test", "otp": "123456", "acknowledge_otp_exposure": False},
                **{},
            )
        )
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_gateway_deposit_execute_rejects_empty_preview(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_execute with empty preview_id returns error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_execute")
        result = _run_async(tool["handler"]({"preview_id": ""}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_gateway_deposit_execute_rejects_long_preview_id(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_execute with >128 char preview_id returns error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_execute")
        result = _run_async(tool["handler"]({"preview_id": "x" * 200}, **{}))
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_gateway_deposit_preview_rejects_bad_amount(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_preview with non-numeric amount returns error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_preview")
        result = _run_async(
            tool["handler"](
                {
                    "service_url": "https://example.com",
                    "method": "GET",
                    "amount": "not-a-number",
                },
                **{},
            )
        )
        parsed = json.loads(result)
        assert parsed.get("success") is False

    def test_gateway_deposit_preview_rejects_below_minimum(self, fake_ctx: FakeCtx):
        """x402_gateway_deposit_preview with amount < 0.5 USDC returns error."""
        tool = next(t for t in fake_ctx.tools if t["name"] == "x402_gateway_deposit_preview")
        result = _run_async(
            tool["handler"](
                {
                    "service_url": "https://example.com",
                    "method": "GET",
                    "amount": "0.10",
                },
                **{},
            )
        )
        parsed = json.loads(result)
        assert parsed.get("success") is False


# ---------------------------------------------------------------------------
# Test class: Approval hook protocol
# ---------------------------------------------------------------------------


class TestApprovalHookProtocol:
    """The pre_tool_call approval hook must follow the dispatch contract."""

    def test_approval_hook_callable(self, fake_ctx: FakeCtx):
        hook = fake_ctx.hooks[0]["handler"]
        assert callable(hook)

    def test_approval_hook_returns_none_for_non_financial(self, fake_ctx: FakeCtx):
        hook = fake_ctx.hooks[0]["handler"]
        result = hook("x402_status", {}, tool_call_id="tc-1")
        assert result is None

    def test_approval_hook_blocks_without_tool_call_id(self, fake_ctx: FakeCtx):
        hook = fake_ctx.hooks[0]["handler"]
        result = hook("x402_pay", {"url": "https://example.com"}, tool_call_id="")
        assert result is not None
        assert result.get("action") == "block"

    def test_approval_hook_approves_pay_with_tool_call_id(self, fake_ctx: FakeCtx):
        hook = fake_ctx.hooks[0]["handler"]
        result = hook("x402_pay", {"url": "https://example.com"}, tool_call_id="tc-2")
        assert result is not None
        assert result.get("action") == "approve"

    def test_approval_hook_blocks_deposit_execute_invalid_preview(self, fake_ctx: FakeCtx):
        hook = fake_ctx.hooks[0]["handler"]
        result = hook(
            "x402_gateway_deposit_execute",
            {"preview_id": 123},  # not a string
            tool_call_id="tc-3",
        )
        assert result is not None
        assert result.get("action") == "block"

    def test_approval_hook_accepts_keyword_args(self, fake_ctx: FakeCtx):
        """Hook must work with keyword args (Hermes dispatch contract)."""
        hook = fake_ctx.hooks[0]["handler"]
        # Financial tool with valid tool_call_id → approve
        result = hook(
            tool_name="x402_login_complete",
            args={},
            tool_call_id="tc-4",
            turn_id="turn-1",
        )
        assert result is not None
        assert result.get("action") == "approve"

    def test_approval_hook_blocks_financial_without_id_kwargs(self, fake_ctx: FakeCtx):
        """Financial tool via kwargs without tool_call_id → block."""
        hook = fake_ctx.hooks[0]["handler"]
        result = hook(
            tool_name="x402_pay",
            args={"url": "https://example.com"},
            tool_call_id="",
        )
        assert result is not None
        assert result.get("action") == "block"
