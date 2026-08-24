"""Regression tests for four-key CLI buyer persistence across restarts."""

from __future__ import annotations

import re

import pytest

from hermes_x402.config import X402Config
from hermes_x402.hermes_plugin.formatters import format_configure
from hermes_x402.hermes_plugin.runtime import X402Runtime
from hermes_x402.hermes_plugin.slash_command import (
    _handle_configure_apply,
    _handle_configure_preview,
    _preview_store,
)

VALID_WALLET = "0xabababababababababababababababababababab"
PERSISTED_KEYS = {
    "X402_ROLE",
    "X402_BUYER_BACKEND",
    "CIRCLE_AGENT_WALLET_ADDRESS",
    "CIRCLE_AGENT_WALLET_NETWORK",
}
OPTIONAL_KEYS = {
    "X402_MAX_USDC_PER_PAYMENT",
    "X402_NETWORK_POLICY",
    "X402_HOST_ALLOWLIST",
    "X402_REQUIRE_GATEWAY_BATCHING",
    "X402_ALLOW_HTTP",
    "X402_ALLOW_CHAT_OTP",
}


@pytest.fixture(autouse=True)
def _clear_previews():
    _preview_store.clear()
    yield
    _preview_store.clear()


def _write_buyer_env(tmp_path, **overrides):
    values = {
        "X402_ROLE": "buyer",
        "X402_BUYER_BACKEND": "cli",
        "CIRCLE_AGENT_WALLET_ADDRESS": VALID_WALLET,
        "CIRCLE_AGENT_WALLET_NETWORK": "ARC-TESTNET",
    }
    values.update(overrides)
    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    )


def _configure_apply(tmp_path, *, max_usdc=None):
    import os

    os.environ["HERMES_HOME"] = str(tmp_path)
    args = ["buyer", "cli", VALID_WALLET, "ARC-TESTNET"]
    if max_usdc is not None:
        args.append(max_usdc)
    preview = _handle_configure_preview(args)
    preview_id = re.search(r"Preview ID: `([^`]+)`", preview)
    assert preview_id is not None, preview
    result = _handle_configure_apply([preview_id.group(1)])
    assert "Configuration Applied" in result


def test_persisted_four_keys_load_into_fresh_config(tmp_path, monkeypatch):
    _write_buyer_env(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = X402Config.from_env()

    assert config.role == "buyer"
    assert config.buyer_backend == "cli"
    assert config.circle_cli_wallet_address == VALID_WALLET
    assert config.circle_cli_network == "ARC-TESTNET"


def test_explicit_process_environment_wins_over_persisted_value(tmp_path, monkeypatch):
    _write_buyer_env(tmp_path, CIRCLE_AGENT_WALLET_NETWORK="ARC-TESTNET")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CIRCLE_AGENT_WALLET_NETWORK", "SOME_OTHER_VALUE")

    config = X402Config.from_env()

    assert config.circle_cli_network == "SOME_OTHER_VALUE"


def test_other_config_values_keep_existing_defaults(tmp_path, monkeypatch):
    _write_buyer_env(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = X402Config.from_env()

    assert config.max_usdc_per_payment == "5"
    assert config.network_policy == "public"
    assert config.host_allowlist == []
    assert config.require_gateway_batching is True
    assert config.allow_http is False
    assert config.allow_chat_otp is False
    assert config.require_approval_for_new_host is False


def test_configure_apply_persists_only_four_buyer_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    _configure_apply(tmp_path)

    written_keys = {
        line.split("=", 1)[0]
        for line in (tmp_path / ".env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert written_keys == PERSISTED_KEYS
    assert not written_keys & OPTIONAL_KEYS


def test_configure_apply_preserves_unrelated_env_content(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("# keep this\nPROVIDER_SETTING=unchanged\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    _configure_apply(tmp_path)

    content = env_path.read_text()
    assert "# keep this" in content
    assert "PROVIDER_SETTING=unchanged" in content


def test_configure_apply_does_not_delete_historical_optional_keys(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("X402_NETWORK_POLICY=strict_allowlist\nX402_ALLOW_HTTP=false\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    _configure_apply(tmp_path)

    content = env_path.read_text()
    assert "X402_NETWORK_POLICY=strict_allowlist" in content
    assert "X402_ALLOW_HTTP=false" in content


def test_fresh_runtime_reloads_persisted_cli_buyer(tmp_path, monkeypatch):
    _write_buyer_env(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    runtime = X402Runtime()
    runtime.ensure_initialized()

    assert runtime.role == "buyer"
    assert runtime.backend_name == "cli"
    assert runtime.network == "ARC-TESTNET"
    assert runtime.wallet_address == VALID_WALLET
    assert runtime.is_configured is True
    assert runtime.is_available is True


def test_configure_apply_writes_no_auth_or_payment_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    _configure_apply(tmp_path, max_usdc="5")

    content = (tmp_path / ".env").read_text()
    for secret_name in (
        "CIRCLE_ENTITY_SECRET",
        "CIRCLE_API_KEY",
        "OTP",
        "PAYMENT-SIGNATURE",
        "PRIVATE_KEY",
        "SEED_PHRASE",
        "SESSION_TOKEN",
    ):
        assert secret_name not in content


def test_configure_status_uses_default_max_without_persisting_it():
    managed = {
        "X402_ROLE": "buyer",
        "X402_BUYER_BACKEND": "cli",
        "CIRCLE_AGENT_WALLET_ADDRESS": VALID_WALLET,
        "CIRCLE_AGENT_WALLET_NETWORK": "ARC-TESTNET",
    }

    result = format_configure(managed, {"available": True, "version": "1.0.0"})

    assert "Configured: Yes" in result
    assert "Max payment: 5 USDC" in result
    assert "Missing:" not in result
