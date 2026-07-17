"""Tests for updated config with new PR #4 env vars (hermes_x402.config)."""

from __future__ import annotations

import pytest

from hermes_x402.buyer.errors import BuyerConfigurationError
from hermes_x402.config import X402Config

# ---------------------------------------------------------------------------
# X402Config defaults
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_default_network_policy(self):
        config = X402Config()
        assert config.network_policy == "strict_allowlist"

    def test_default_discovery_providers(self):
        config = X402Config()
        assert config.discovery_providers == ("circle_marketplace",)

    def test_default_network_preference(self):
        config = X402Config()
        assert config.network_preference == ("base",)

    def test_default_require_gateway_batching(self):
        config = X402Config()
        assert config.require_gateway_batching is True

    def test_default_require_approval(self):
        config = X402Config()
        assert config.require_approval_for_new_host is False

    def test_default_daily_budget(self):
        config = X402Config()
        assert config.daily_budget_usdc is None

    def test_default_allow_http(self):
        config = X402Config()
        assert config.allow_http is False


# ---------------------------------------------------------------------------
# from_env: X402_NETWORK_POLICY
# ---------------------------------------------------------------------------


class TestNetworkPolicyFromEnv:
    def test_strict(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "strict_allowlist")
        config = X402Config.from_env()
        assert config.network_policy == "strict_allowlist"

    def test_public(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "public")
        config = X402Config.from_env()
        assert config.network_policy == "public"

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "bogus")
        config = X402Config.from_env()
        assert config.network_policy == "strict_allowlist"


# ---------------------------------------------------------------------------
# from_env: X402_DISCOVERY_PROVIDERS
# ---------------------------------------------------------------------------


class TestDiscoveryProvidersFromEnv:
    def test_single_provider(self, monkeypatch):
        monkeypatch.setenv("X402_DISCOVERY_PROVIDERS", "circle_marketplace")
        config = X402Config.from_env()
        assert config.discovery_providers == ("circle_marketplace",)

    def test_multiple_providers(self, monkeypatch):
        monkeypatch.setenv("X402_DISCOVERY_PROVIDERS", "a, b, c")
        config = X402Config.from_env()
        assert config.discovery_providers == ("a", "b", "c")

    def test_default_when_absent(self, monkeypatch):
        monkeypatch.delenv("X402_DISCOVERY_PROVIDERS", raising=False)
        config = X402Config.from_env()
        assert config.discovery_providers == ("circle_marketplace",)


# ---------------------------------------------------------------------------
# from_env: X402_NETWORK_PREFERENCE
# ---------------------------------------------------------------------------


class TestNetworkPreferenceFromEnv:
    def test_single_preference(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_PREFERENCE", "base")
        config = X402Config.from_env()
        assert config.network_preference == ("base",)

    def test_multiple_preferences(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_PREFERENCE", "base, polygon, ethereum")
        config = X402Config.from_env()
        assert config.network_preference == ("base", "polygon", "ethereum")

    def test_default_when_absent(self, monkeypatch):
        monkeypatch.delenv("X402_NETWORK_PREFERENCE", raising=False)
        config = X402Config.from_env()
        assert config.network_preference == ("base",)


# ---------------------------------------------------------------------------
# from_env: X402_REQUIRE_GATEWAY_BATCHING
# ---------------------------------------------------------------------------


class TestRequireGatewayBatchingFromEnv:
    def test_true(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_GATEWAY_BATCHING", "true")
        config = X402Config.from_env()
        assert config.require_gateway_batching is True

    def test_false(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_GATEWAY_BATCHING", "false")
        config = X402Config.from_env()
        assert config.require_gateway_batching is False

    def test_one(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_GATEWAY_BATCHING", "1")
        config = X402Config.from_env()
        assert config.require_gateway_batching is True

    def test_default_when_absent(self, monkeypatch):
        monkeypatch.delenv("X402_REQUIRE_GATEWAY_BATCHING", raising=False)
        config = X402Config.from_env()
        assert config.require_gateway_batching is True


# ---------------------------------------------------------------------------
# from_env: X402_REQUIRE_APPROVAL_FOR_NEW_HOST
# ---------------------------------------------------------------------------


class TestRequireApprovalFromEnv:
    def test_true(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "true")
        config = X402Config.from_env()
        assert config.require_approval_for_new_host is True

    def test_false(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "false")
        config = X402Config.from_env()
        assert config.require_approval_for_new_host is False

    def test_default_when_absent(self, monkeypatch):
        monkeypatch.delenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", raising=False)
        config = X402Config.from_env()
        assert config.require_approval_for_new_host is False


# ---------------------------------------------------------------------------
# from_env: X402_ALLOW_HTTP
# ---------------------------------------------------------------------------


class TestAllowHttpFromEnv:
    def test_true(self, monkeypatch):
        monkeypatch.setenv("X402_ALLOW_HTTP", "true")
        config = X402Config.from_env()
        assert config.allow_http is True

    def test_false(self, monkeypatch):
        monkeypatch.setenv("X402_ALLOW_HTTP", "false")
        config = X402Config.from_env()
        assert config.allow_http is False

    def test_one(self, monkeypatch):
        monkeypatch.setenv("X402_ALLOW_HTTP", "1")
        config = X402Config.from_env()
        assert config.allow_http is True

    def test_default_when_absent(self, monkeypatch):
        monkeypatch.delenv("X402_ALLOW_HTTP", raising=False)
        config = X402Config.from_env()
        assert config.allow_http is False


# ---------------------------------------------------------------------------
# from_env: X402_DISCOVERY_HOST_ALLOWLIST
# ---------------------------------------------------------------------------


class TestDiscoveryHostAllowlistFromEnv:
    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("X402_DISCOVERY_HOST_ALLOWLIST", "a.com, b.com")
        config = X402Config.from_env()
        assert config.discovery_host_allowlist == ("a.com", "b.com")

    def test_default_empty(self, monkeypatch):
        monkeypatch.delenv("X402_DISCOVERY_HOST_ALLOWLIST", raising=False)
        config = X402Config.from_env()
        assert config.discovery_host_allowlist == ()


# ---------------------------------------------------------------------------
# Daily budget validation
# ---------------------------------------------------------------------------


class TestDailyBudget:
    def test_valid_budget(self):
        config = X402Config(daily_budget_usdc="10.50")
        result = config.validate_daily_budget()
        assert result == "10.50"

    def test_zero_budget(self):
        config = X402Config(daily_budget_usdc="0")
        result = config.validate_daily_budget()
        assert result == "0"

    def test_none_budget(self):
        config = X402Config(daily_budget_usdc=None)
        result = config.validate_daily_budget()
        assert result is None

    def test_negative_budget_rejected(self):
        config = X402Config(daily_budget_usdc="-1")
        with pytest.raises(BuyerConfigurationError, match="non-negative"):
            config.validate_daily_budget()

    def test_nan_budget_rejected(self):
        config = X402Config(daily_budget_usdc="NaN")
        with pytest.raises(BuyerConfigurationError, match="Invalid"):
            config.validate_daily_budget()

    def test_infinity_budget_rejected(self):
        config = X402Config(daily_budget_usdc="Infinity")
        with pytest.raises(BuyerConfigurationError, match="Invalid"):
            config.validate_daily_budget()

    def test_invalid_string_rejected(self):
        config = X402Config(daily_budget_usdc="not-a-number")
        with pytest.raises(BuyerConfigurationError, match="Invalid"):
            config.validate_daily_budget()

    def test_decimal_budget(self):
        config = X402Config(daily_budget_usdc="0.01")
        result = config.validate_daily_budget()
        assert result == "0.01"


# ---------------------------------------------------------------------------
# Legacy config still works
# ---------------------------------------------------------------------------


class TestLegacyConfig:
    def test_from_env_no_role(self, monkeypatch):
        monkeypatch.delenv("X402_ROLE", raising=False)
        monkeypatch.delenv("X402_BUYER_BACKEND", raising=False)
        monkeypatch.delenv("X402_NETWORK_POLICY", raising=False)
        monkeypatch.delenv("X402_DISCOVERY_PROVIDERS", raising=False)
        monkeypatch.delenv("X402_NETWORK_PREFERENCE", raising=False)
        monkeypatch.delenv("X402_REQUIRE_GATEWAY_BATCHING", raising=False)
        monkeypatch.delenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", raising=False)
        monkeypatch.delenv("X402_ALLOW_HTTP", raising=False)
        monkeypatch.delenv("X402_DISCOVERY_HOST_ALLOWLIST", raising=False)
        monkeypatch.delenv("X402_HOST_ALLOWLIST", raising=False)
        config = X402Config.from_env()
        assert config.role is None
        # PR #4 fields have sensible defaults
        assert config.network_policy == "strict_allowlist"
        assert config.allow_http is False


# ---------------------------------------------------------------------------
# Combined PR #4 env vars
# ---------------------------------------------------------------------------


class TestCombinedEnvVars:
    def test_all_pr4_vars_together(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "public")
        monkeypatch.setenv("X402_DISCOVERY_PROVIDERS", "a, b")
        monkeypatch.setenv("X402_NETWORK_PREFERENCE", "polygon, base")
        monkeypatch.setenv("X402_REQUIRE_GATEWAY_BATCHING", "false")
        monkeypatch.setenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "true")
        monkeypatch.setenv("X402_ALLOW_HTTP", "true")
        monkeypatch.setenv("X402_DISCOVERY_HOST_ALLOWLIST", "host1.com, host2.com")
        monkeypatch.delenv("X402_ROLE", raising=False)
        monkeypatch.delenv("X402_BUYER_BACKEND", raising=False)
        monkeypatch.delenv("X402_HOST_ALLOWLIST", raising=False)

        config = X402Config.from_env()
        assert config.network_policy == "public"
        assert config.discovery_providers == ("a", "b")
        assert config.network_preference == ("polygon", "base")
        assert config.require_gateway_batching is False
        assert config.require_approval_for_new_host is True
        assert config.allow_http is True
        assert config.discovery_host_allowlist == ("host1.com", "host2.com")
