"""Regression and architecture tests for hermes_x402."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hermes_x402 import (
    BuyerConfigurationError,
    CircleDcwBuyerBackend,
    InvalidPaymentChallengeError,
    PaymentPolicy,
    PaymentPolicyError,
    X402BuyerService,
    X402Config,
    X402HermesAgent,
    create_buyer_tool,
)
from hermes_x402.buyer import X402BuyerTool
from hermes_x402.buyer.models import PaymentProof
from hermes_x402.config import ARC_TESTNET
from hermes_x402.context import X402ContextBridge, get_payment_context, set_payment_context
from hermes_x402.middleware import X402SellerMiddleware


@dataclass
class FakeBackend:
    calls: int = 0
    fail: Exception | None = None

    @property
    def name(self) -> str:
        return "fake"

    @property
    def wallet_address(self) -> str:
        return "0xBuyer"

    async def create_payment_proof(self, **_: Any) -> PaymentProof:
        self.calls += 1
        if self.fail:
            raise self.fail
        return PaymentProof(
            backend=self.name,
            header_name="Payment-Signature",
            header_value="generated-proof",
            payer=self.wallet_address,
            amount="10000",
            network="eip155:5042002",
        )


def challenge(amount: str = "10000") -> dict[str, Any]:
    return {
        "x402Version": 2,
        "resource": {"url": "/premium"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:5042002",
                "amount": amount,
                "payTo": "0xSeller",
                "maxTimeoutSeconds": 604900,
                "extra": {
                    "name": "GatewayWalletBatched",
                    "version": "1",
                    "verifyingContract": ARC_TESTNET["gateway_wallet"],
                },
            }
        ],
    }


def encoded_challenge(amount: str = "10000") -> str:
    return base64.b64encode(json.dumps(challenge(amount)).encode()).decode()


class TestConfig:
    def test_legacy_from_env_remains_usable(self, monkeypatch):
        # Live Hermes environments may export the explicit v2 buyer role/CLI
        # variables. This regression exercises the legacy DCW-only env shape, so
        # isolate it from process-level configuration.
        for key in (
            "X402_ROLE",
            "X402_BUYER_BACKEND",
            "CIRCLE_AGENT_WALLET_ADDRESS",
            "CIRCLE_AGENT_WALLET_NETWORK",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("X402_SELLER_ADDRESS", "0xSeller")
        monkeypatch.setenv("CIRCLE_DCW_WALLET_ID", "wallet-123")
        monkeypatch.setenv("CIRCLE_DCW_WALLET_ADDRESS", "0xBuyer")
        monkeypatch.setenv("CIRCLE_ENTITY_SECRET", "secret-abc")
        config = X402Config.from_env()
        assert config.wallet_id == "wallet-123"
        assert config.role is None

    @pytest.mark.parametrize("role", ["seller", "buyer", "dual"])
    def test_roles_validate_expected_requirements(self, role):
        kwargs: dict[str, Any] = {"role": role}
        if role in {"seller", "dual"}:
            kwargs["seller_address"] = "0xSeller"
        if role in {"buyer", "dual"}:
            kwargs.update(
                buyer_backend="dcw",
                wallet_id="id",
                wallet_address="0xBuyer",
                entity_secret="0" * 64,
            )
        X402Config(**kwargs).validate()

    def test_seller_rejects_buyer_configuration(self):
        with pytest.raises(BuyerConfigurationError):
            X402Config(role="seller", seller_address="0xSeller", buyer_backend="dcw").validate()

    def test_buyer_requires_explicit_backend(self):
        with pytest.raises(BuyerConfigurationError):
            X402Config(
                role="buyer", wallet_id="id", wallet_address="0xBuyer", entity_secret="0" * 64
            ).validate()

    def test_cli_requires_explicit_selection(self):
        with pytest.raises(BuyerConfigurationError, match="circle_cli_wallet_address"):
            X402Config(role="buyer", buyer_backend="cli").validate()

    def test_invalid_runtime_role_is_rejected(self):
        with pytest.raises(BuyerConfigurationError, match="Unsupported x402 role"):
            X402Config(
                role="unexpected",  # type: ignore[arg-type]
                buyer_backend="dcw",
                wallet_id="id",
                wallet_address="0xBuyer",
                entity_secret="0" * 64,
            ).validate()

    def test_legacy_agent_from_config_warns_and_constructs_dual_agent(self):
        config = X402Config(
            seller_address="0xSeller",
            wallet_id="id",
            wallet_address="0xBuyer",
            entity_secret="0" * 64,
        )
        with pytest.warns(DeprecationWarning, match="legacy"):
            agent = X402HermesAgent.from_config(config)
        assert agent.seller.seller_address == "0xSeller"
        assert agent.buyer.wallet_address == "0xBuyer"

    def test_explicit_dual_dcw_agent_from_config_succeeds(self):
        agent = X402HermesAgent.from_config(
            X402Config(
                role="dual",
                buyer_backend="dcw",
                seller_address="0xSeller",
                wallet_id="id",
                wallet_address="0xBuyer",
                entity_secret="0" * 64,
            )
        )
        assert agent.buyer.wallet_address == "0xBuyer"

    @pytest.mark.parametrize("role", ["seller", "buyer"])
    def test_agent_from_config_rejects_non_dual_explicit_roles(self, role: str):
        kwargs: dict[str, Any] = {"role": role, "seller_address": "0xSeller"}
        if role == "buyer":
            kwargs.update(
                buyer_backend="dcw",
                wallet_id="id",
                wallet_address="0xBuyer",
                entity_secret="0" * 64,
            )
        with pytest.raises(BuyerConfigurationError, match="requires role='dual'"):
            X402HermesAgent.from_config(X402Config(**kwargs))

    def test_unknown_environment_role_is_rejected(self, monkeypatch):
        monkeypatch.setenv("X402_ROLE", "unexpected")
        with pytest.raises(BuyerConfigurationError, match="Unsupported x402 role"):
            X402Config.from_env()


class TestContextAndSeller:
    def test_context_round_trip(self):
        set_payment_context("0xPayer", "10000", "eip155:5042002", "0xtx")
        assert get_payment_context().payer == "0xPayer"  # type: ignore[union-attr]
        X402ContextBridge.clear()
        assert get_payment_context() is None

    def test_seller_wire_format_unchanged(self):
        middleware = X402SellerMiddleware(seller_address="0xSeller")
        response = middleware._build_402_response("10000", "/api/test")
        assert response["status"] == 402
        assert response["body"]["x402Version"] == 2
        assert response["body"]["accepts"][0]["extra"]["name"] == "GatewayWalletBatched"
        assert middleware._price_to_amount("$0.01") == "10000"


class StubAsyncClient:
    def __init__(self, responses: list[httpx.Response]):
        self.responses = responses
        self.requests: list[httpx.Request] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: Any):
        return None

    async def request(self, *, method: str, url: str, json: Any, headers: dict[str, str]):
        self.requests.append(httpx.Request(method, url, json=json, headers=headers))
        return self.responses.pop(0)


class TestBuyerService:
    @staticmethod
    def service(backend: FakeBackend, **policy: Any) -> X402BuyerService:
        return X402BuyerService(backend=backend, policy=PaymentPolicy(**policy))

    @pytest.mark.asyncio
    async def test_non_402_never_calls_backend(self):
        backend = FakeBackend()
        client = StubAsyncClient([httpx.Response(200, json={"ok": True})])
        with patch("hermes_x402.buyer.service.httpx.AsyncClient", return_value=client):
            result = await self.service(backend).pay("https://api.example.com")
        assert backend.calls == 0
        assert result.payment_status == "not_submitted"
        assert result.data == {"ok": True}

    @pytest.mark.asyncio
    async def test_402_calls_backend_once_adds_proof_and_copies_headers(self):
        backend = FakeBackend()
        original = {"X-Caller": "yes", "Payment-Signature": "caller-proof"}
        client = StubAsyncClient(
            [
                httpx.Response(402, headers={"Payment-Required": encoded_challenge()}),
                httpx.Response(200, json={"paid": True}),
            ]
        )
        with patch("hermes_x402.buyer.service.httpx.AsyncClient", return_value=client):
            result = await self.service(backend).pay("https://api.example.com", headers=original)
        assert backend.calls == 1
        assert original["Payment-Signature"] == "caller-proof"
        assert "payment-signature" not in {key.lower() for key in client.requests[0].headers}
        assert client.requests[1].headers["Payment-Signature"] == "generated-proof"
        assert result.payment_status == "resource_succeeded"

    @pytest.mark.asyncio
    async def test_host_and_amount_are_rejected_before_backend(self):
        backend = FakeBackend()
        with pytest.raises(PaymentPolicyError):
            await self.service(backend, host_allowlist=("allowed.example",)).pay(
                "https://evil.example"
            )
        assert backend.calls == 0
        client = StubAsyncClient(
            [httpx.Response(402, headers={"Payment-Required": encoded_challenge("10001")})]
        )
        with (
            patch("hermes_x402.buyer.service.httpx.AsyncClient", return_value=client),
            pytest.raises(PaymentPolicyError),
        ):
            await self.service(backend, max_usdc="0.01").pay("https://allowed.example")
        assert backend.calls == 0

    @pytest.mark.asyncio
    async def test_malformed_or_empty_challenge_is_rejected(self):
        backend = FakeBackend()
        for header in ("not-base64", base64.b64encode(b"{}").decode()):
            client = StubAsyncClient([httpx.Response(402, headers={"Payment-Required": header})])
            with (
                patch("hermes_x402.buyer.service.httpx.AsyncClient", return_value=client),
                pytest.raises(InvalidPaymentChallengeError),
            ):
                await self.service(backend).pay("https://api.example.com")
        assert backend.calls == 0


class TestCircleDcwBackend:
    def backend(self, secret: str = "a" * 64) -> CircleDcwBuyerBackend:
        return CircleDcwBuyerBackend(
            wallet_id="wallet-id", wallet_address="0xBuyer", entity_secret=secret
        )

    def test_contract_identity_and_secret_safe_repr(self):
        backend = self.backend()
        assert backend.name == "circle_dcw"
        assert backend.wallet_address == "0xBuyer"
        assert "a" * 64 not in repr(backend)

    def test_entity_public_key_is_cached_and_ciphertext_is_fresh(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
        pem = key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        response = MagicMock()
        response.json.return_value = {"data": {"publicKey": pem}}
        response.raise_for_status.return_value = None
        backend = self.backend()
        with patch("hermes_x402.backends.circle_dcw.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            first = backend._fresh_entity_secret_ciphertext()
            second = backend._fresh_entity_secret_ciphertext()
        assert first != second
        assert client_cls.return_value.__enter__.return_value.get.call_count == 1

    @pytest.mark.asyncio
    async def test_dcw_payload_and_signature_normalization_are_compatible(self):
        backend = self.backend()
        backend._fresh_entity_secret_ciphertext = MagicMock(return_value="fresh")  # type: ignore[method-assign]
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"signature": "abc"}}
        captured: dict[str, Any] = {}

        async def post(*args: Any, **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return response

        client = MagicMock()
        client.post = post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("hermes_x402.backends.circle_dcw.httpx.AsyncClient", return_value=client):
            proof = await backend.create_payment_proof(
                url="https://ignored", method="GET", body=None, payment_required=challenge()
            )
        decoded = json.loads(base64.b64decode(proof.header_value))
        assert proof.header_name == "Payment-Signature"
        assert decoded["payload"]["signature"] == "0xabc"
        assert decoded["payload"]["authorization"]["from"] == "0xBuyer"
        payload = captured["json"]
        assert payload["walletId"] == "wallet-id"
        assert payload["blockchain"] == "ARC-TESTNET"
        assert payload["entitySecretCiphertext"] == "fresh"
        typed = json.loads(payload["data"])
        assert typed["primaryType"] == "TransferWithAuthorization"
        assert typed["domain"]["chainId"] == 5042002


class TestPublicApiAndDualRole:
    def test_legacy_and_backend_api_and_ambiguity(self):
        with pytest.warns(DeprecationWarning):
            legacy = create_buyer_tool(
                wallet_id="id", wallet_address="0xBuyer", entity_secret="a" * 64
            )
        assert legacy.wallet_address == "0xBuyer"
        assert legacy.wallet_id == "id"
        assert legacy.blockchain == "ARC-TESTNET"
        assert legacy.chain == "arcTestnet"
        assert legacy.max_usdc is None
        from hermes_x402.buyer import CircleDcwBuyerBackend as BuyerPackageDcwBackend

        assert BuyerPackageDcwBackend is CircleDcwBuyerBackend
        backend = CircleDcwBuyerBackend(
            wallet_id="id", wallet_address="0xBuyer", entity_secret="a" * 64
        )
        assert create_buyer_tool(backend=backend).backend is backend
        with pytest.raises(BuyerConfigurationError):
            create_buyer_tool(
                wallet_id="id", wallet_address="0xBuyer", entity_secret="a" * 64, backend=backend
            )
        with pytest.raises(BuyerConfigurationError):
            create_buyer_tool()

    @pytest.mark.asyncio
    async def test_legacy_max_usdc_assignment_rebuilds_policy_and_enforces_cap(self):
        backend = FakeBackend()
        tool = X402BuyerTool(
            backend=backend,
            policy=PaymentPolicy(
                host_allowlist=("api.example.com",),
                allow_http=False,
                daily_budget_usdc="1.00",
            ),
        )

        tool.max_usdc = "0.01"

        assert tool.max_usdc == "0.01"
        assert tool.policy.max_usdc == "0.01"
        assert tool.service.policy.max_usdc == "0.01"
        assert tool.policy.host_allowlist == ("api.example.com",)
        assert tool.policy.allow_http is False
        assert tool.policy.daily_budget_usdc == "1.00"
        assert tool.service.backend is backend

        client = StubAsyncClient(
            [httpx.Response(402, headers={"Payment-Required": encoded_challenge("10001")})]
        )
        with (
            patch("hermes_x402.buyer.service.httpx.AsyncClient", return_value=client),
            pytest.raises(PaymentPolicyError),
        ):
            await tool.pay("https://api.example.com")
        assert backend.calls == 0

        tool.max_usdc = None
        assert tool.max_usdc is None
        assert tool.policy.max_usdc is None
        assert tool.service.policy.max_usdc is None

    def test_dual_role_keeps_wallets_separate_and_uses_backend(self):
        backend = CircleDcwBuyerBackend(
            wallet_id="id", wallet_address="0xBuyer", entity_secret="a" * 64
        )
        agent = X402HermesAgent(seller_address="0xSeller", buyer_backend=backend)
        assert agent.seller.seller_address == "0xSeller"
        assert agent.buyer.wallet_address == "0xBuyer"
        assert "a" * 64 not in repr(agent.buyer.backend)
        with pytest.raises(BuyerConfigurationError):
            X402HermesAgent(seller_address="0xBuyer", buyer_backend=backend)
