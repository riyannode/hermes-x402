"""Shared test fixtures for hermes-x402.

Mocks DNS validation to succeed by default for existing tests.
DNS validation is tested specifically in test_hardening.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_runtime_x402_env(monkeypatch):
    """Keep local operator env from changing deterministic unit tests."""
    for name in (
        "X402_ALLOW_CHAT_OTP",
        "X402_ALLOW_HTTP",
        "X402_BUYER_BACKEND",
        "X402_DAILY_BUDGET_USDC",
        "X402_DISCOVERY_HOST_ALLOWLIST",
        "X402_DISCOVERY_PROVIDERS",
        "X402_HOST_ALLOWLIST",
        "X402_MAX_USDC_PER_PAYMENT",
        "X402_NETWORK_POLICY",
        "X402_NETWORK_PREFERENCE",
        "X402_REQUIRE_APPROVAL_FOR_NEW_HOST",
        "X402_REQUIRE_GATEWAY_BATCHING",
        "X402_ROLE",
        "X402_SELLER_ADDRESS",
        "CIRCLE_AGENT_WALLET_ADDRESS",
        "CIRCLE_AGENT_WALLET_NETWORK",
        "CIRCLE_CLI_CWD",
        "CIRCLE_CLI_EXECUTABLE",
        "CIRCLE_DCW_BLOCKCHAIN",
        "CIRCLE_DCW_WALLET_ADDRESS",
        "CIRCLE_DCW_WALLET_ID",
        "CIRCLE_ENTITY_SECRET",
        "CIRCLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # Ensure every test has a deterministic public_base_url so gateway
    # construction does not depend on the operator's shell environment.
    monkeypatch.setenv("X402_PUBLIC_BASE_URL", "https://seller.example")


@pytest.fixture(autouse=True)
def _mock_dns_validation():
    """Mock DNS validation to succeed for all tests by default.

    The DNS validation is imported lazily inside tool handlers via:
        from hermes_x402.dns_validator import resolve_and_validate_destination

    So we mock it at the dns_validator module level.
    """
    with patch(
        "hermes_x402.dns_validator.resolve_and_validate_destination",
        new_callable=AsyncMock,
        return_value=("93.184.216.34",),
    ):
        yield
