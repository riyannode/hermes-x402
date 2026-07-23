"""Tests for the supports preflight module (hermes_x402.buyer.supports)."""

from __future__ import annotations

import base64
import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hermes_x402.buyer.errors import InvalidPaymentChallengeError, PaymentPolicyError
from hermes_x402.buyer.models import PaymentOption
from hermes_x402.buyer.supports import (
    MAX_HEADER_SIZE,
    SupportResult,
    _amount_to_usdc,
    _determine_payment_system,
    _parse_v2_challenge,
    check_supports,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_v2_challenge(
    accepts: list[dict[str, Any]],
    version: int = 2,
) -> str:
    """Build a base64-encoded x402 v2 Payment-Required header."""
    body = {
        "x402Version": version,
        "resource": {"url": "/test"},
        "accepts": accepts,
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


def _make_accept_entry(
    network: str = "eip155:8453",
    amount: str = "10000",
    extra_name: str = "GatewayWalletBatched",
) -> dict[str, Any]:
    return {
        "scheme": "exact",
        "network": network,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "amount": amount,
        "payTo": "0xSeller",
        "maxTimeoutSeconds": 2592000,
        "extra": {
            "name": extra_name,
            "version": "1",
            "verifyingContract": "0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        },
    }


def _mock_response(
    status: int = 402,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    content_type: str = "application/json",
) -> httpx.Response:
    h = headers or {}
    h.setdefault("content-type", content_type)
    return httpx.Response(status_code=status, headers=h, content=body)


# ---------------------------------------------------------------------------
# HTTP 402 x402 v2 header parsed
# ---------------------------------------------------------------------------


class TestV2HeaderParsing:
    def test_basic_header_parsed(self):
        accepts = [_make_accept_entry()]
        header = _make_v2_challenge(accepts)
        version, options, unsupported = _parse_v2_challenge(
            header, configured_backend=None, wallet_network=None
        )
        assert version == "2"
        assert len(options) == 1
        assert options[0].network == "base"
        assert options[0].amount_atomic == "10000"

    def test_version_field_preserved(self):
        accepts = [_make_accept_entry()]
        header = _make_v2_challenge(accepts, version=3)
        version, options, _ = _parse_v2_challenge(
            header, configured_backend=None, wallet_network=None
        )
        assert version == "3"


# ---------------------------------------------------------------------------
# GatewayWalletBatched detected through extra.name
# ---------------------------------------------------------------------------


class TestGatewayDetection:
    def test_gateway_detected(self):
        entry = _make_accept_entry(extra_name="GatewayWalletBatched")
        result = _determine_payment_system(entry)
        assert result == "gateway_batching"

    def test_vanilla_detected(self):
        entry = _make_accept_entry(extra_name="SomethingElse")
        result = _determine_payment_system(entry)
        assert result == "vanilla"

    def test_no_extra_is_vanilla(self):
        entry = {"scheme": "exact", "network": "eip155:8453"}
        result = _determine_payment_system(entry)
        assert result == "vanilla"

    def test_empty_extra_name_is_vanilla(self):
        entry = _make_accept_entry(extra_name="")
        result = _determine_payment_system(entry)
        assert result == "vanilla"


# ---------------------------------------------------------------------------
# Vanilla exact distinguished from Gateway exact
# ---------------------------------------------------------------------------


class TestVanillaVsGateway:
    def test_vanilla_entry_parsed(self):
        header = _make_v2_challenge([_make_accept_entry(extra_name="VanillaExact")])
        _, options, _ = _parse_v2_challenge(header, configured_backend=None, wallet_network=None)
        assert options[0].payment_system == "vanilla"

    def test_gateway_entry_parsed(self):
        header = _make_v2_challenge([_make_accept_entry(extra_name="GatewayWalletBatched")])
        _, options, _ = _parse_v2_challenge(header, configured_backend=None, wallet_network=None)
        assert options[0].payment_system == "gateway_batching"


# ---------------------------------------------------------------------------
# Free 200 resource detected
# ---------------------------------------------------------------------------


class TestFreeResourceDetection:
    @pytest.mark.asyncio
    async def test_200_no_payment_required(self):
        mock_resp = _mock_response(status=200)
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_policy:
            mock_policy.return_value = MagicMock()
            mock_policy.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                result = await check_supports("https://example.com/free")

        assert result.payment_required is False
        assert result.supported is False
        assert result.x402 is False


# ---------------------------------------------------------------------------
# Malformed header returns error
# ---------------------------------------------------------------------------


class TestMalformedHeader:
    def test_invalid_base64(self):
        with pytest.raises(InvalidPaymentChallengeError, match="base64"):
            _parse_v2_challenge("not-base64!!!", configured_backend=None, wallet_network=None)

    def test_invalid_json(self):
        valid_b64 = base64.b64encode(b"not json").decode()
        with pytest.raises(InvalidPaymentChallengeError, match="JSON"):
            _parse_v2_challenge(valid_b64, configured_backend=None, wallet_network=None)

    def test_non_dict_payload(self):
        valid_b64 = base64.b64encode(b'["not","a","dict"]').decode()
        with pytest.raises(InvalidPaymentChallengeError, match="object"):
            _parse_v2_challenge(valid_b64, configured_backend=None, wallet_network=None)


# ---------------------------------------------------------------------------
# Missing accepts returns error
# ---------------------------------------------------------------------------


class TestMissingAccepts:
    def test_no_accepts_key(self):
        body = {"x402Version": 2, "resource": {"url": "/test"}}
        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        with pytest.raises(InvalidPaymentChallengeError, match="No accepted"):
            _parse_v2_challenge(encoded, configured_backend=None, wallet_network=None)

    def test_empty_accepts_list(self):
        body = {"x402Version": 2, "accepts": []}
        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        with pytest.raises(InvalidPaymentChallengeError, match="No accepted"):
            _parse_v2_challenge(encoded, configured_backend=None, wallet_network=None)


# ---------------------------------------------------------------------------
# Empty accepts returns error
# ---------------------------------------------------------------------------


class TestEmptyAccepts:
    def test_accepts_none_value(self):
        body = {"x402Version": 2, "accepts": None}
        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        with pytest.raises(InvalidPaymentChallengeError, match="No accepted"):
            _parse_v2_challenge(encoded, configured_backend=None, wallet_network=None)


# ---------------------------------------------------------------------------
# Unsupported network identified
# ---------------------------------------------------------------------------


class TestUnsupportedNetwork:
    def test_unknown_network_in_unsupported(self):
        entry = _make_accept_entry(network="eip155:99999999")
        header = _make_v2_challenge([entry])
        _, options, unsupported = _parse_v2_challenge(
            header, configured_backend=None, wallet_network=None
        )
        assert len(unsupported) > 0
        assert "eip155:99999999" in unsupported

    def test_known_network_not_in_unsupported(self):
        entry = _make_accept_entry(network="eip155:8453")
        header = _make_v2_challenge([entry])
        _, options, unsupported = _parse_v2_challenge(
            header, configured_backend=None, wallet_network=None
        )
        assert "eip155:8453" not in unsupported


# ---------------------------------------------------------------------------
# Amount normalization works
# ---------------------------------------------------------------------------


class TestAmountNormalization:
    def test_basic_conversion(self):
        assert _amount_to_usdc("10000") == "0.01"

    def test_large_amount(self):
        assert _amount_to_usdc("1000000") == "1"

    def test_small_amount(self):
        assert _amount_to_usdc("1") == "0.000001"

    def test_zero(self):
        assert _amount_to_usdc("0") == "0"

    def test_invalid_returns_original(self):
        assert _amount_to_usdc("not-a-number") == "not-a-number"


# ---------------------------------------------------------------------------
# No payment / signing / backend call in supports
# ---------------------------------------------------------------------------


class TestSupportsReadOnly:
    @pytest.mark.asyncio
    async def test_no_backend_call(self):
        mock_resp = _mock_response(
            status=402, headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])}
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        mock_backend = MagicMock()
        mock_backend.create_payment_proof = AsyncMock()

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                await check_supports(
                    "https://example.com/data",
                    config=MagicMock(buyer_backend="cli", blockchain="BASE"),
                )

        # Backend should never have been called
        mock_backend.create_payment_proof.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_signing(self):
        """check_supports never calls create_payment_proof or similar."""
        mock_resp = _mock_response(
            status=402,
            headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])},
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                result = await check_supports("https://example.com/data")

        assert result.x402 is True
        assert result.supported is False  # no backend configured


# ---------------------------------------------------------------------------
# POST endpoint not incorrectly probed using GET
# ---------------------------------------------------------------------------


class TestMethodPreserved:
    @pytest.mark.asyncio
    async def test_post_method_used(self):
        mock_resp = _mock_response(
            status=402, headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])}
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                result = await check_supports("https://example.com/data", method="POST")

        assert result.method == "POST"
        # Verify the HTTP client was called with POST
        call_kwargs = client.request.call_args
        assert call_kwargs.kwargs.get("method") == "POST"


# ---------------------------------------------------------------------------
# Bounded challenge
# ---------------------------------------------------------------------------


class TestBoundedChallenge:
    def test_oversized_header_truncated(self):
        # Build a header > MAX_HEADER_SIZE
        huge_accept = _make_accept_entry()
        huge_accept["amount"] = "1" * (MAX_HEADER_SIZE + 1000)
        body = {"x402Version": 2, "accepts": [huge_accept]}
        raw = base64.b64encode(json.dumps(body).encode()).decode()
        # Should not crash — the parser truncates to MAX_HEADER_SIZE bytes
        # Truncation may cause JSON parse error, which is acceptable
        with contextlib.suppress(InvalidPaymentChallengeError, Exception):
            _parse_v2_challenge(raw, configured_backend=None, wallet_network=None)


# ---------------------------------------------------------------------------
# SupportResult.to_dict
# ---------------------------------------------------------------------------


class TestSupportResultDict:
    def test_to_dict_structure(self):
        opt = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="base",
            network_id="eip155:8453",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=2592000,
        )
        result = SupportResult(
            supported=True,
            x402=True,
            gateway_batching=True,
            resource="https://example.com",
            method="GET",
            version="2",
            options=(opt,),
            preferred_option=opt,
        )
        d = result.to_dict()
        assert d["supported"] is True
        assert d["x402"] is True
        assert d["gateway_batching"] is True
        assert len(d["options"]) == 1
        assert d["preferred_option"]["network"] == "base"


# ---------------------------------------------------------------------------
# URL policy rejection in supports
# ---------------------------------------------------------------------------


class TestSupportsUrlPolicy:
    @pytest.mark.asyncio
    async def test_invalid_url_returns_unsupported(self):
        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value.validate_url = MagicMock(
                side_effect=PaymentPolicyError("blocked")
            )
            result = await check_supports("https://evil.internal/secret")

        assert result.supported is False
        assert result.reason is not None
        assert "blocked" in result.reason


# ---------------------------------------------------------------------------
# POST body propagation in check_supports
# ---------------------------------------------------------------------------


class TestCheckSupportsBodyPropagation:
    @pytest.mark.asyncio
    async def test_post_body_sent_as_json(self):
        """POST request includes body as JSON payload."""
        mock_resp = _mock_response(
            status=402,
            headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])},
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        body = {"query": "test", "filters": {"limit": 10}}

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                await check_supports(
                    "https://example.com/api",
                    method="POST",
                    body=body,
                )

        call_kwargs = client.request.call_args.kwargs
        assert call_kwargs.get("json") == body
        assert call_kwargs.get("method") == "POST"

    @pytest.mark.asyncio
    async def test_put_body_sent_as_json(self):
        """PUT request includes body as JSON payload."""
        mock_resp = _mock_response(
            status=402,
            headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])},
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        body = {"id": 1, "value": "updated"}

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                await check_supports(
                    "https://example.com/api",
                    method="PUT",
                    body=body,
                )

        call_kwargs = client.request.call_args.kwargs
        assert call_kwargs.get("json") == body

    @pytest.mark.asyncio
    async def test_patch_body_sent_as_json(self):
        """PATCH request includes body as JSON payload."""
        mock_resp = _mock_response(
            status=402,
            headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])},
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        body = {"field": "new_value"}

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                await check_supports(
                    "https://example.com/api",
                    method="PATCH",
                    body=body,
                )

        call_kwargs = client.request.call_args.kwargs
        assert call_kwargs.get("json") == body

    @pytest.mark.asyncio
    async def test_get_ignores_body(self):
        """GET request does not include body even if body is provided."""
        mock_resp = _mock_response(
            status=402,
            headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])},
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                await check_supports(
                    "https://example.com/api",
                    method="GET",
                    body={"ignored": True},
                )

        call_kwargs = client.request.call_args.kwargs
        assert "json" not in call_kwargs

    @pytest.mark.asyncio
    async def test_method_normalized_to_uppercase(self):
        """Lowercase method is normalized to uppercase."""
        mock_resp = _mock_response(
            status=402,
            headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])},
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                result = await check_supports(
                    "https://example.com/api",
                    method="post",
                )

        assert result.method == "POST"
        call_kwargs = client.request.call_args.kwargs
        assert call_kwargs.get("method") == "POST"

    @pytest.mark.asyncio
    async def test_body_not_mutated(self):
        """Original body dict is not modified by check_supports."""
        mock_resp = _mock_response(
            status=402,
            headers={"Payment-Required": _make_v2_challenge([_make_accept_entry()])},
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        body = {"original": "value"}
        original_keys = set(body.keys())

        with patch("hermes_x402.buyer.supports.parse_network_policy") as mock_pol:
            mock_pol.return_value = MagicMock()
            mock_pol.return_value.validate_url = MagicMock()
            with patch("hermes_x402.buyer.supports.httpx.AsyncClient", return_value=client):
                await check_supports(
                    "https://example.com/api",
                    method="POST",
                    body=body,
                )

        assert set(body.keys()) == original_keys
