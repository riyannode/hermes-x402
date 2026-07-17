"""Tests for the seller gateway decorator (hermes_x402.seller_gateway)."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from hermes_x402.networks import get_network
from hermes_x402.seller_gateway import (
    X402Gateway,
    _parse_price,
    create_aiohttp_gateway,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VALID_SELLER = "0x" + "ab" * 20
_VALID_SELLER_2 = "0x" + "cd" * 20


def _nc(key: str = "base"):
    """Resolve a network key to a NetworkConfig."""
    from hermes_x402.networks import get_network

    return get_network(key)


def _make_gateway(**kwargs) -> X402Gateway:
    """Create a gateway with sensible defaults."""
    defaults = {
        "seller_address": _VALID_SELLER,
        "networks": ["base"],
        "facilitator_url": "https://gateway-api.circle.com",
        "default_description": "Test resource",
    }
    defaults.update(kwargs)
    return create_aiohttp_gateway(**defaults)


# ---------------------------------------------------------------------------
# gateway.require('$0.01') works as decorator
# ---------------------------------------------------------------------------


class TestRequireDecorator:
    def test_require_returns_callable(self):
        gateway = _make_gateway()
        decorator = gateway.require("$0.01")
        assert callable(decorator)

    def test_require_wraps_handler(self):
        gateway = _make_gateway()

        @gateway.require("$0.01")
        async def handler(request):
            pass

        # The wrapped handler should be a middleware
        assert hasattr(handler, "__wrapped_handler__") or callable(handler)


# ---------------------------------------------------------------------------
# Exact atomic amount correct (10000 for $0.01)
# ---------------------------------------------------------------------------


class TestPriceParsing:
    def test_001_usd(self):
        assert _parse_price("$0.01") == "10000"

    def test_1_usd(self):
        assert _parse_price("$1.00") == "1000000"

    def test_0_001_usd(self):
        assert _parse_price("$0.001") == "1000"

    def test_with_dollar_sign(self):
        assert _parse_price("$5.50") == "5500000"

    def test_without_dollar_sign(self):
        assert _parse_price("5.50") == "5500000"

    def test_decimal_input(self):
        assert _parse_price(Decimal("0.01")) == "10000"

    def test_integer_input(self):
        assert _parse_price("1") == "1000000"


# ---------------------------------------------------------------------------
# Decimal edge cases
# ---------------------------------------------------------------------------


class TestDecimalEdgeCases:
    def test_negative_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            _parse_price("-0.01")

    def test_nan_rejected(self):
        with pytest.raises(ValueError, match="NaN"):
            _parse_price("NaN")

    def test_infinity_rejected(self):
        with pytest.raises(ValueError, match="Infinity"):
            _parse_price("Infinity")

    def test_zero_rejected(self):
        with pytest.raises(ValueError, match="greater than zero"):
            _parse_price("$0")

    def test_excess_precision_rejected(self):
        with pytest.raises(ValueError, match="excess precision"):
            _parse_price("$0.0000001")  # 7 decimals

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_price("")

    def test_callable_rejected(self):
        with pytest.raises(TypeError, match="callable"):
            _parse_price(lambda: "$0.01")


# ---------------------------------------------------------------------------
# Multi-network accepts generated
# ---------------------------------------------------------------------------


class TestMultiNetworkAccepts:
    def test_multi_network_gateway(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["base", "polygon", "ethereum"],
        )
        assert len(gateway._networks) == 3
        assert gateway._networks[0].key == "base"
        assert gateway._networks[1].key == "polygon"
        assert gateway._networks[2].key == "ethereum"

    def test_default_network_is_base(self):
        gateway = _make_gateway()
        assert len(gateway._networks) == 1
        assert gateway._networks[0].key == "base"


# ---------------------------------------------------------------------------
# GatewayWalletBatched metadata present
# ---------------------------------------------------------------------------


class TestGatewayWalletBatched:
    def test_accepts_contain_extra(self):
        gateway = _make_gateway()
        accepts = gateway._build_accepts(gateway._networks, "10000")
        assert len(accepts) == 1
        entry = accepts[0]
        assert entry["extra"]["name"] == "GatewayWalletBatched"
        assert entry["extra"]["version"] == "1"
        assert "verifyingContract" in entry["extra"]

    def test_402_body_has_correct_version(self):
        gateway = _make_gateway()
        body = gateway._build_402_body("10000", "/test", "desc", gateway._networks)
        assert body["x402Version"] == 2
        assert body["accepts"][0]["extra"]["name"] == "GatewayWalletBatched"


# ---------------------------------------------------------------------------
# Invalid seller address rejected
# ---------------------------------------------------------------------------


class TestSellerAddressValidation:
    def test_invalid_address_rejected(self):
        with pytest.raises(ValueError, match="Invalid seller address"):
            X402Gateway(
                seller_address="not-an-address",
                networks=["base"],
                facilitator_url="https://example.com",
                default_description="test",
            )

    def test_too_short_rejected(self):
        with pytest.raises(ValueError, match="Invalid seller address"):
            X402Gateway(
                seller_address="0x1234",
                networks=["base"],
                facilitator_url="https://example.com",
                default_description="test",
            )

    def test_valid_address_accepted(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["base"],
        )
        assert gateway._seller_address == _VALID_SELLER


# ---------------------------------------------------------------------------
# Invalid network rejected
# ---------------------------------------------------------------------------


class TestInvalidNetwork:
    def test_unknown_network_in_require(self):
        gateway = _make_gateway()
        with pytest.raises(ValueError, match="Unknown network"):
            gateway.require(price="$0.01", networks=["nonexistent_chain"])


# ---------------------------------------------------------------------------
# No header injection
# ---------------------------------------------------------------------------


class TestNoHeaderInjection:
    def test_accepts_no_control_characters(self):
        gateway = _make_gateway()
        accepts = gateway._build_accepts(gateway._networks, "10000")
        for entry in accepts:
            for _key, val in entry.items():
                if isinstance(val, str):
                    assert "\n" not in val
                    assert "\r" not in val
                    assert "\x00" not in val


# ---------------------------------------------------------------------------
# Keyword form: @gateway.require(price=..., networks=...)
# ---------------------------------------------------------------------------


class TestKeywordForm:
    def test_keyword_form_works(self):
        gateway = _make_gateway()
        decorator = gateway.require(
            price="$0.01",
            networks=["base"],
            description="Premium endpoint",
        )
        assert callable(decorator)

    def test_keyword_form_with_multiple_networks(self):
        gateway = _make_gateway()
        decorator = gateway.require(
            price="$0.05",
            networks=["base", "polygon"],
        )
        assert callable(decorator)


# ---------------------------------------------------------------------------
# create_aiohttp_gateway validation
# ---------------------------------------------------------------------------


class TestCreateGateway:
    def test_invalid_seller_address_rejected(self):
        with pytest.raises(ValueError, match="Invalid seller address"):
            create_aiohttp_gateway(
                seller_address="bad",
                networks=["base"],
            )

    def test_unknown_network_rejected(self):
        with pytest.raises(ValueError, match="Unknown network"):
            create_aiohttp_gateway(
                seller_address=_VALID_SELLER,
                networks=["nonexistent_chain"],
            )

    def test_facilitator_url_from_network(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["base"],
        )
        # base's facilitator_url
        assert gateway._facilitator_url == "https://gateway-api.circle.com"

    def test_custom_facilitator_url(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["base"],
            facilitator_url="https://custom.example.com",
        )
        assert gateway._facilitator_url == "https://custom.example.com"


# ---------------------------------------------------------------------------
# Build settle requirements
# ---------------------------------------------------------------------------


class TestSettleRequirements:
    def test_requirements_structure(self):
        gateway = _make_gateway()
        # _build_settle_requirements expects CAIP-2 network identifiers
        reqs = gateway._build_settle_requirements(
            "10000", "eip155:8453", gateway._networks
        )
        assert reqs["scheme"] == "exact"
        assert reqs["network"] == "eip155:8453"
        assert reqs["amount"] == "10000"
        assert reqs["payTo"] == _VALID_SELLER
        assert reqs["extra"]["name"] == "GatewayWalletBatched"

    def test_unknown_network_raises(self):
        gateway = _make_gateway()
        with pytest.raises(ValueError, match="not in accepted networks"):
            gateway._build_settle_requirements("10000", "nonexistent", gateway._networks)

    def test_registry_key_not_accepted(self):
        """Registry keys like 'base' must not be accepted — only CAIP-2."""
        gateway = _make_gateway()
        with pytest.raises(ValueError, match="not in accepted networks"):
            gateway._build_settle_requirements("10000", "base", gateway._networks)


# ---------------------------------------------------------------------------
# Seller authorization hardening — asset/payTo validation
# ---------------------------------------------------------------------------


class TestSellerAuthValidation:
    """Missing or wrong asset/payTo must be rejected before facilitator."""

    def _make_auth_header(self, *, asset: str = "", pay_to: str = "", **overrides):
        """Build a base64 payment header with specified authorization fields."""
        auth = {
            "from": overrides.get("from", "0x" + "cd" * 20),
            "value": overrides.get("value", "1000"),
        }
        if asset:
            auth["asset"] = asset
        if pay_to:
            auth["payTo"] = pay_to
        payload = {"payload": {"authorization": auth, "signature": "sig"}}
        return base64.b64encode(json.dumps(payload).encode()).decode()

    @pytest.mark.asyncio
    async def test_missing_asset_rejected_before_facilitator(self):
        """Authorization without asset → 402, facilitator not called."""
        gw = _make_gateway()
        seller_addr = "0x" + "ab" * 20
        gw._seller_address = seller_addr

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {"Payment-Signature": self._make_auth_header(pay_to=seller_addr)}

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(mock_request, AsyncMock(), "$0.01", None, None, "desc")
            mock_settle.assert_not_called()
            assert resp.status == 402

    @pytest.mark.asyncio
    async def test_wrong_asset_rejected_before_facilitator(self):
        """Authorization with wrong asset → 402, facilitator not called."""
        gw = _make_gateway()
        seller_addr = "0x" + "ab" * 20
        gw._seller_address = seller_addr

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {
            "Payment-Signature": self._make_auth_header(asset="0x" + "ff" * 20, pay_to=seller_addr)
        }

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(mock_request, AsyncMock(), "$0.01", None, None, "desc")
            mock_settle.assert_not_called()
            assert resp.status == 402

    @pytest.mark.asyncio
    async def test_missing_pay_to_rejected_before_facilitator(self):
        """Authorization without payTo → 402, facilitator not called."""
        gw = _make_gateway()
        net = get_network("base")
        expected_asset = net.usdc_address

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {"Payment-Signature": self._make_auth_header(asset=expected_asset)}

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(mock_request, AsyncMock(), "$0.01", None, None, "desc")
            mock_settle.assert_not_called()
            assert resp.status == 402

    @pytest.mark.asyncio
    async def test_wrong_pay_to_rejected_before_facilitator(self):
        """Authorization with wrong payTo → 402, facilitator not called."""
        gw = _make_gateway()
        seller_addr = "0x" + "ab" * 20
        gw._seller_address = seller_addr
        net = get_network("base")
        expected_asset = net.usdc_address

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {
            "Payment-Signature": self._make_auth_header(
                asset=expected_asset, pay_to="0x" + "ff" * 20
            )
        }

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(mock_request, AsyncMock(), "$0.01", None, None, "desc")
            mock_settle.assert_not_called()
            assert resp.status == 402
