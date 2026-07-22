"""Tests for the seller gateway decorator (hermes_x402.seller_gateway)."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from hermes_x402.networks import get_network
from hermes_x402.seller_gateway import (
    BUYER_MAX_TIMEOUT_SECONDS,
    SERVER_MIN_TIMEOUT_SECONDS,
    X402Gateway,
    _parse_price,
    create_aiohttp_gateway,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VALID_SELLER = "0x" + "ab" * 20
_VALID_SELLER_2 = "0x" + "cd" * 20


def _nc(key: str = "arcTestnet"):
    """Resolve a network key to a NetworkConfig."""
    from hermes_x402.networks import get_network

    return get_network(key)


def _make_gateway(**kwargs) -> X402Gateway:
    """Create a gateway with sensible defaults."""
    defaults = {
        "seller_address": _VALID_SELLER,
        "networks": ["arcTestnet"],
        "facilitator_url": "https://gateway-api-testnet.circle.com",
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
            networks=["arcTestnet", "arcTestnet", "arcTestnet"],
        )
        assert len(gateway._networks) == 3
        assert gateway._networks[0].key == "arcTestnet"
        assert gateway._networks[1].key == "arcTestnet"
        assert gateway._networks[2].key == "arcTestnet"

    def test_default_network_is_arcTestnet(self):
        gateway = _make_gateway()
        assert len(gateway._networks) == 1
        assert gateway._networks[0].key == "arcTestnet"


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
                networks=["arcTestnet"],
                facilitator_url="https://example.com",
                default_description="test",
            )

    def test_too_short_rejected(self):
        with pytest.raises(ValueError, match="Invalid seller address"):
            X402Gateway(
                seller_address="0x1234",
                networks=["arcTestnet"],
                facilitator_url="https://example.com",
                default_description="test",
            )

    def test_valid_address_accepted(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
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
            networks=["arcTestnet"],
            description="Premium endpoint",
        )
        assert callable(decorator)

    def test_keyword_form_with_multiple_networks(self):
        gateway = _make_gateway()
        decorator = gateway.require(
            price="$0.05",
            networks=["arcTestnet", "arcTestnet"],
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
                networks=["arcTestnet"],
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
            networks=["arcTestnet"],
        )
        # arcTestnet's facilitator_url
        assert gateway._facilitator_url == "https://gateway-api-testnet.circle.com"

    def test_custom_facilitator_url(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
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
        reqs = gateway._build_settle_requirements("10000", "eip155:5042002", gateway._networks)
        assert reqs["scheme"] == "exact"
        assert reqs["network"] == "eip155:5042002"
        assert reqs["amount"] == "10000"
        assert reqs["payTo"] == _VALID_SELLER
        assert reqs["extra"]["name"] == "GatewayWalletBatched"

    def test_unknown_network_raises(self):
        gateway = _make_gateway()
        with pytest.raises(ValueError, match="not in accepted networks"):
            gateway._build_settle_requirements("10000", "nonexistent", gateway._networks)

    def test_registry_key_not_accepted(self):
        """Registry keys like 'arcTestnet' must not be accepted — only CAIP-2."""
        gateway = _make_gateway()
        with pytest.raises(ValueError, match="not in accepted networks"):
            gateway._build_settle_requirements("10000", "arcTestnet", gateway._networks)


# ---------------------------------------------------------------------------
# Seller authorization hardening — asset/payTo validation
# ---------------------------------------------------------------------------


class TestSellerAuthValidation:
    """Missing or wrong asset/payTo must be rejected before facilitator."""

    def _make_auth_header(
        self,
        *,
        asset: str | None = None,
        pay_to: str | None = None,
        **overrides,
    ):
        """Build a complete payment header with configurable accepted requirement fields."""
        net = get_network("arcTestnet")
        value = str(overrides.get("value", "10000"))
        auth = {
            "from": overrides.get("from", "0x" + "cd" * 20),
            "to": net.gateway_wallet,
            "value": value,
            "validAfter": "0",
            "validBefore": "9999999999",
            "nonce": "0x" + "01" * 32,
        }
        accepted = {
            "scheme": "exact",
            "network": net.caip2,
            "amount": value,
            "maxTimeoutSeconds": 2592000,
            "extra": {
                "name": "GatewayWalletBatched",
                "version": "1",
                "verifyingContract": net.gateway_wallet,
            },
        }
        if asset is not None:
            accepted["asset"] = asset
        if pay_to is not None:
            accepted["payTo"] = pay_to
        payload = {
            "x402Version": 2,
            "payload": {"authorization": auth, "signature": "0xsig"},
            "accepted": accepted,
        }
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
        net = get_network("arcTestnet")
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
        net = get_network("arcTestnet")
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


# ---------------------------------------------------------------------------
# Circle CLI x402 v2 payload compatibility / sanitized diagnostics
# ---------------------------------------------------------------------------


class TestCircleCliPayloadCompatibility:
    def _make_cli_header(self, *, max_timeout: int = BUYER_MAX_TIMEOUT_SECONDS) -> str:
        net = get_network("arcTestnet")
        payload = {
            "x402Version": 2,
            "payload": {
                "authorization": {
                    "from": "0x" + "cd" * 20,
                    "to": net.gateway_wallet,
                    "value": "3000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x" + "01" * 32,
                },
                "signature": "0x" + "02" * 65,
            },
            "accepted": {
                "scheme": "exact",
                "network": net.caip2,
                "asset": net.usdc_address,
                "amount": "3000",
                "payTo": _VALID_SELLER,
                "maxTimeoutSeconds": max_timeout,
                "extra": {
                    "name": "GatewayWalletBatched",
                    "version": "1",
                    "verifyingContract": net.gateway_wallet,
                },
            },
            "resource": {
                "url": "https://seller.local/premium",
                "description": "Paid resource",
                "mimeType": "application/json",
            },
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    @pytest.mark.asyncio
    async def test_circle_cli_30_day_timeout_contract_settles_with_server_minimum(self, caplog):
        """Circle CLI accepted timeout can exceed server-owned settle requirement."""
        caplog.set_level("DEBUG", logger="hermes_x402.seller_gateway")
        gw = _make_gateway()
        mock_request = AsyncMock()
        mock_request.path = "/premium"
        mock_request.headers = {"Payment-Signature": self._make_cli_header()}

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            resp = await gw._handle_request(
                mock_request,
                AsyncMock(return_value=web.json_response({"ok": True})),
                "$0.003",
                None,
                None,
                "Paid resource",
            )

        assert mock_settle.await_count == 1
        requirements = mock_settle.call_args.args[1]
        assert requirements["maxTimeoutSeconds"] == SERVER_MIN_TIMEOUT_SECONDS
        assert resp.status != 402
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "x402 seller timeout compatibility" in messages
        assert f"'buyer_maxTimeoutSeconds': {BUYER_MAX_TIMEOUT_SECONDS}" in messages
        assert f"'server_maxTimeoutSeconds': {SERVER_MIN_TIMEOUT_SECONDS}" in messages
        assert "Payment-Signature" not in messages
        assert "authorization" not in messages
        assert "signature" not in messages
        assert "nonce" not in messages

    @pytest.mark.asyncio
    async def test_canonical_server_timeout_published_and_settled(self):
        """Seller-owned requirements remain Circle's documented 604900 seconds."""
        gw = _make_gateway()
        networks = [get_network("arcTestnet")]
        challenge = gw._build_402_body("3000", "/premium", "Paid resource", networks)
        assert challenge["accepts"][0]["maxTimeoutSeconds"] == SERVER_MIN_TIMEOUT_SECONDS

        requirements = gw._build_settle_requirements("3000", "eip155:5042002", networks)
        assert requirements["maxTimeoutSeconds"] == SERVER_MIN_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_selected_requirement_mismatch_logged_without_payment_material(self, caplog):
        caplog.set_level("DEBUG", logger="hermes_x402.seller_gateway")
        gw = _make_gateway()
        mock_request = AsyncMock()
        mock_request.path = "/premium"
        mock_request.headers = {"Payment-Signature": self._make_cli_header(max_timeout=60)}

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(
                mock_request,
                AsyncMock(),
                "$0.003",
                None,
                None,
                "Paid resource",
            )

        mock_settle.assert_not_called()
        assert resp.status == 402
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "stage=selected requirement mismatch" in messages
        assert "maxTimeoutSeconds below server minimum" in messages
        assert "Payment-Signature" not in messages
        assert "authorization" not in messages
        assert "signature" not in messages
        assert "nonce" not in messages

    @pytest.mark.asyncio
    async def test_non_integer_timeout_rejected_before_facilitator(self):
        gw = _make_gateway()
        mock_request = AsyncMock()
        mock_request.path = "/premium"
        mock_request.headers = {"Payment-Signature": self._make_cli_header(max_timeout="2592000")}  # type: ignore[arg-type]

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(
                mock_request,
                AsyncMock(),
                "$0.003",
                None,
                None,
                "Paid resource",
            )

        mock_settle.assert_not_called()
        assert resp.status == 402

    @pytest.mark.asyncio
    async def test_timeout_above_defensive_max_rejected_before_facilitator(self):
        gw = _make_gateway()
        mock_request = AsyncMock()
        mock_request.path = "/premium"
        mock_request.headers = {
            "Payment-Signature": self._make_cli_header(max_timeout=BUYER_MAX_TIMEOUT_SECONDS + 1)
        }

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(
                mock_request,
                AsyncMock(),
                "$0.003",
                None,
                None,
                "Paid resource",
            )

        mock_settle.assert_not_called()
        assert resp.status == 402
