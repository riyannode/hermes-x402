"""Tests for hermes_x402 package."""

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from hermes_x402.config import ARC_TESTNET, X402Config
from hermes_x402.context import X402ContextBridge, get_payment_context, set_payment_context


# ── Config Tests ─────────────────────────────────────────────────────────────


class TestX402Config:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("X402_SELLER_ADDRESS", "0xSeller")
        monkeypatch.setenv("CIRCLE_DCW_WALLET_ID", "wallet-123")
        monkeypatch.setenv("CIRCLE_DCW_WALLET_ADDRESS", "0xBuyer")
        monkeypatch.setenv("CIRCLE_ENTITY_SECRET", "secret-abc")

        config = X402Config.from_env()
        assert config.seller_address == "0xSeller"
        assert config.wallet_id == "wallet-123"
        assert config.wallet_address == "0xBuyer"
        assert config.entity_secret == "secret-abc"

    def test_get_chain_config(self):
        config = X402Config(chain="arcTestnet")
        cc = config.get_chain_config()
        assert cc["chain_id"] == 5042002
        assert cc["is_testnet"] is True

    def test_get_facilitator_url_override(self):
        config = X402Config(facilitator_url="https://custom.api.com")
        assert config.get_facilitator_url() == "https://custom.api.com"

    def test_get_facilitator_url_default(self):
        config = X402Config(chain="arcTestnet")
        assert config.get_facilitator_url() == "https://gateway-api-testnet.circle.com"

    def test_unknown_chain_raises(self):
        config = X402Config(chain="unknown")
        with pytest.raises(ValueError, match="Unknown chain"):
            config.get_chain_config()


# ── Context Tests ────────────────────────────────────────────────────────────


class TestContext:
    def test_set_and_get(self):
        ctx = set_payment_context(
            payer="0xPayer",
            amount="10000",
            network="eip155:5042002",
            transaction="0xtx",
        )
        assert ctx.payer == "0xPayer"
        assert ctx.amount == "10000"

        current = get_payment_context()
        assert current is not None
        assert current.payer == "0xPayer"
        assert current.transaction == "0xtx"

    def test_clear(self):
        set_payment_context(payer="0xPayer", amount="10000", network="eip155:5042002")
        X402ContextBridge.clear()
        assert get_payment_context() is None

    def test_default_is_none(self):
        X402ContextBridge.clear()
        assert get_payment_context() is None


# ── Middleware Tests ──────────────────────────────────────────────────────────


class TestSellerMiddleware:
    @pytest.fixture
    def middleware(self):
        from hermes_x402.middleware import X402SellerMiddleware

        return X402SellerMiddleware(
            seller_address="0xSeller",
            chain="arcTestnet",
        )

    def test_price_to_amount(self, middleware):
        assert middleware._price_to_amount("$0.01") == "10000"
        assert middleware._price_to_amount("$0.001") == "1000"
        assert middleware._price_to_amount("$1.00") == "1000000"

    def test_build_402_response(self, middleware):
        resp = middleware._build_402_response("10000", "/api/test")
        assert resp["status"] == 402
        assert "Payment-Required" in resp["headers"]
        assert resp["body"]["x402Version"] == 2
        assert len(resp["body"]["accepts"]) == 1
        assert resp["body"]["accepts"][0]["amount"] == "10000"
        assert resp["body"]["accepts"][0]["payTo"] == "0xSeller"

    def test_build_requirements(self, middleware):
        req = middleware._build_requirements("10000", "eip155:5042002")
        assert req["scheme"] == "exact"
        assert req["network"] == "eip155:5042002"
        assert req["amount"] == "10000"
        assert req["payTo"] == "0xSeller"
        assert req["extra"]["name"] == "GatewayWalletBatched"


# ── Buyer Tests ──────────────────────────────────────────────────────────────


class TestBuyerTool:
    @pytest.fixture
    def buyer(self):
        from hermes_x402.buyer import X402BuyerTool

        return X402BuyerTool(
            wallet_id="wallet-123",
            wallet_address="0xBuyer",
            entity_secret="test-secret",
            chain="arcTestnet",
        )

    def test_check_host_allowlist_empty(self, buyer):
        assert buyer._check_host("https://any-host.com") is True

    def test_check_host_allowlist_match(self, buyer):
        buyer.host_allowlist = ["example.com"]
        assert buyer._check_host("https://example.com/api") is True
        assert buyer._check_host("https://sub.example.com/api") is True
        assert buyer._check_host("https://evil.com/api") is False

    def test_check_host_allowlist_multiple(self, buyer):
        buyer.host_allowlist = ["example.com", "api.test.com"]
        assert buyer._check_host("https://example.com") is True
        assert buyer._check_host("https://api.test.com") is True
        assert buyer._check_host("https://other.com") is False


# ── Agent Tests ──────────────────────────────────────────────────────────────


class TestDualRoleAgent:
    def test_same_wallet_raises(self):
        from hermes_x402.agent import X402HermesAgent

        with pytest.raises(ValueError, match="different addresses"):
            X402HermesAgent(
                seller_address="0xSame",
                buyer_wallet_address="0xSame",
                buyer_wallet_id="w1",
                buyer_entity_secret="s1",
            )

    def test_different_wallets_ok(self):
        from hermes_x402.agent import X402HermesAgent

        agent = X402HermesAgent(
            seller_address="0xSeller",
            buyer_wallet_address="0xBuyer",
            buyer_wallet_id="w1",
            buyer_entity_secret="s1",
        )
        assert agent.seller.seller_address == "0xSeller"
        assert agent.buyer.wallet_address == "0xBuyer"
