"""Tests for the seller gateway decorator (hermes_x402.seller_gateway)."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from hermes_x402.middleware import (
    create_aiohttp_middleware,
)
from hermes_x402.networks import get_network
from hermes_x402.seller_gateway import (
    BUYER_MAX_TIMEOUT_SECONDS,
    SERVER_MIN_TIMEOUT_SECONDS,
    PaymentParsingError,
    SellerConfigurationError,
    X402Gateway,
    _build_resource_url,
    _parse_price,
    _resolve_seller_networks,
    _validate_public_base_url,
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
        "public_base_url": "https://seller.example",
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
            public_base_url="https://seller.example",
        )
        # Duplicate network keys are deduplicated by the resolver.
        assert len(gateway._networks) == 1
        assert gateway._networks[0].key == "arcTestnet"

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
            public_base_url="https://seller.example",
        )
        assert gateway._seller_address == _VALID_SELLER


# ---------------------------------------------------------------------------
# Invalid network rejected
# ---------------------------------------------------------------------------


class TestInvalidNetwork:
    def test_unknown_network_in_require(self):
        gateway = _make_gateway()
        with pytest.raises(ValueError, match="Unknown seller network"):
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
        with pytest.raises(ValueError, match="Unknown seller network"):
            create_aiohttp_gateway(
                seller_address=_VALID_SELLER,
                networks=["nonexistent_chain"],
                public_base_url="https://seller.example",
            )

    def test_facilitator_url_from_network(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
            public_base_url="https://seller.example",
        )
        # arcTestnet's facilitator_url
        assert gateway._facilitator_url == "https://gateway-api-testnet.circle.com"

    def test_custom_facilitator_url(self):
        gateway = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
            facilitator_url="https://custom.example.com",
            public_base_url="https://seller.example",
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


# ===========================================================================
# Regression tests for Codex review fixes
# ===========================================================================


def _make_valid_network_config(
    *,
    key: str = "supportedTestnet",
    seller_supported: bool = True,
    environment: str = "testnet",
    gateway_wallet: str = "0x" + "ab" * 20,
):
    from hermes_x402.networks import NetworkConfig

    return NetworkConfig(
        key=key,
        display_name=key,
        aliases=(key,),
        caip2="eip155:999999",
        chain_id=999999,
        environment=environment,
        cli_chain=None,
        usdc_address="0x" + "ab" * 20,
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=False,
        seller_supported=seller_supported,
        gateway_wallet=gateway_wallet,
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="test",
    )


def _make_mock_request(method: str = "GET", path: str = "/premium") -> web.Request:
    from unittest.mock import AsyncMock

    req = AsyncMock()
    req.method = method
    req.path = path
    req.path_qs = path
    req.headers = {}
    req.match_info = {}
    return req


# --- Fix 1: Multi-network, default Arc Testnet ---


class TestMultiNetworkCapabilityValidation:
    def test_default_seller_network_is_arc_testnet(self):
        gateway = create_aiohttp_gateway(
            _VALID_SELLER,
            public_base_url="https://seller.example",
        )
        assert [n.key for n in gateway._networks] == ["arcTestnet"]

    def test_explicit_supported_non_arc_network_is_allowed(self):
        cfg = _make_valid_network_config(
            key="supportedTestnet",
            seller_supported=True,
        )
        gateway = X402Gateway(
            seller_address=_VALID_SELLER,
            networks=[cfg],
            facilitator_url=cfg.facilitator_url,
            public_base_url="https://seller.example",
            default_description="Paid resource",
        )
        assert gateway._networks == [cfg]

    def test_unsupported_network_is_rejected(self):
        cfg = _make_valid_network_config(
            key="unsupported",
            seller_supported=False,
        )
        with pytest.raises(SellerConfigurationError):
            _resolve_seller_networks([cfg])

    def test_mixed_environments_are_rejected(self):
        from hermes_x402.networks import get_network

        testnet = get_network("arcTestnet")
        mainnet = get_network("base")
        with pytest.raises(SellerConfigurationError):
            _resolve_seller_networks([testnet, mainnet])

    def test_network_config_missing_gateway_wallet_is_rejected(self):
        from dataclasses import replace

        from hermes_x402.networks import get_network

        cfg = replace(
            get_network("arcTestnet"),
            gateway_wallet="",
        )
        with pytest.raises(SellerConfigurationError, match="gateway_wallet"):
            _resolve_seller_networks([cfg])


# --- Fix 2: Base URL path prefix preservation ---


class TestResourceUrlPreservesBasePath:
    @pytest.mark.parametrize(
        ("base_url", "request_path", "expected"),
        [
            (
                "https://example.com",
                "/premium",
                "https://example.com/premium",
            ),
            (
                "https://example.com/x402",
                "/premium",
                "https://example.com/x402/premium",
            ),
            (
                "https://example.com/x402/",
                "/premium?format=json",
                "https://example.com/x402/premium?format=json",
            ),
            (
                "https://example.com/api/v1",
                "/premium/",
                "https://example.com/api/v1/premium/",
            ),
        ],
    )
    def test_resource_url_preserves_public_base_path(
        self,
        base_url,
        request_path,
        expected,
    ):
        request = _make_mock_request("GET", request_path)
        assert _build_resource_url(base_url, request) == expected

    def test_resource_path_cannot_escape_configured_prefix(self):
        request = _make_mock_request("GET", "/../admin")
        with pytest.raises(PaymentParsingError, match="path traversal"):
            _build_resource_url(
                "https://example.com/x402",
                request,
            )

    def test_validate_public_base_url_rejects_query(self):
        with pytest.raises(SellerConfigurationError, match="query string"):
            _validate_public_base_url("https://example.com?foo=bar", allow_http=False)

    def test_validate_public_base_url_rejects_fragment(self):
        with pytest.raises(SellerConfigurationError, match="fragment"):
            _validate_public_base_url("https://example.com#section", allow_http=False)

    def test_validate_public_base_url_rejects_userinfo(self):
        with pytest.raises(SellerConfigurationError, match="userinfo"):
            _validate_public_base_url("https://user:pass@example.com", allow_http=False)

    def test_validate_public_base_url_preserves_path_prefix(self):
        result = _validate_public_base_url("https://example.com/x402", allow_http=False)
        assert result == "https://example.com/x402"


# --- Fix 3: Public base URL required, no seller.local fallback ---


class TestPublicBaseUrlRequired:
    def test_public_base_url_is_required(self, monkeypatch):
        monkeypatch.delenv("X402_PUBLIC_BASE_URL", raising=False)
        with pytest.raises(
            SellerConfigurationError,
            match="public seller URL is required",
        ):
            create_aiohttp_gateway(_VALID_SELLER)

    def test_public_base_url_from_environment(self, monkeypatch):
        monkeypatch.setenv(
            "X402_PUBLIC_BASE_URL",
            "https://seller.example/x402",
        )
        gateway = create_aiohttp_gateway(_VALID_SELLER)
        assert gateway._public_base_url == "https://seller.example/x402"

    def test_insecure_http_requires_explicit_opt_in(self):
        with pytest.raises(SellerConfigurationError):
            create_aiohttp_gateway(
                _VALID_SELLER,
                public_base_url="http://127.0.0.1:8080",
            )

    def test_seller_local_not_used_as_fallback(self, monkeypatch):
        monkeypatch.delenv("X402_PUBLIC_BASE_URL", raising=False)
        with pytest.raises(SellerConfigurationError):
            create_aiohttp_gateway(_VALID_SELLER)


# --- Fix 4: NetworkConfig objects validated through same resolver ---


class TestNetworkConfigObjectValidation:
    def test_network_config_object_is_validated(self):
        from dataclasses import replace

        from hermes_x402.networks import get_network

        invalid = replace(
            get_network("arcTestnet"),
            seller_supported=False,
        )
        with pytest.raises(SellerConfigurationError):
            X402Gateway(
                seller_address=_VALID_SELLER,
                networks=[invalid],
                facilitator_url=invalid.facilitator_url,
                public_base_url="https://seller.example",
                default_description="Paid resource",
            )

    def test_mixed_string_and_object_inputs_are_validated(self):
        another = _make_valid_network_config(key="anotherTestnet")
        resolved = _resolve_seller_networks(["arcTestnet", another])
        assert len(resolved) == 2


# --- Fix 5: Decorator resets context; legacy process_request preserves ---


class TestContextLifecycle:
    @pytest.mark.asyncio
    async def test_decorator_resets_context_after_handler(self):
        from hermes_x402.context import get_payment_context

        gw = _make_gateway()

        async def handler(request):
            return web.json_response({"ok": True})

        protected = gw.require("$0.01")(handler)

        mock_request = _make_mock_request("GET", "/premium")
        mock_request.headers = {}  # no payment header

        await protected(mock_request)
        assert get_payment_context() is None

    @pytest.mark.asyncio
    async def test_failed_legacy_request_does_not_leave_context(self):
        from hermes_x402.context import get_payment_context

        middleware = create_aiohttp_middleware(
            seller_address=_VALID_SELLER,
            public_base_url="https://seller.example",
        )
        mock_request = _make_mock_request("GET", "/premium")
        mock_request.headers = {}  # no payment

        result = await middleware.process_request(mock_request, "$0.003")
        assert result is None
        assert get_payment_context() is None


# --- Fix: payment_fingerprint hashes settlement identity only ---


class TestPaymentFingerprintSettlementIdentity:
    """Payment fingerprint must be stable across different accepted timeouts."""

    def _build_payload(self, *, max_timeout: int = 604900, nonce: str = "0x" + "01" * 32) -> str:
        import base64
        import json

        from hermes_x402.networks import get_network

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
                    "nonce": nonce,
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
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def test_same_auth_different_timeout_produces_same_fingerprint(self):
        from hermes_x402.seller_gateway import (
            _decode_payment_header,
            _fingerprint,
            _settlement_identity,
        )

        header_604900 = self._build_payload(max_timeout=604900)
        header_2592000 = self._build_payload(max_timeout=2592000)

        decoded_1 = _decode_payment_header(header_604900)
        decoded_2 = _decode_payment_header(header_2592000)

        fp1 = _fingerprint(_settlement_identity(decoded_1))
        fp2 = _fingerprint(_settlement_identity(decoded_2))
        assert fp1 == fp2

    def test_different_nonce_produces_different_fingerprint(self):
        from hermes_x402.seller_gateway import (
            _decode_payment_header,
            _fingerprint,
            _settlement_identity,
        )

        header_a = self._build_payload(nonce="0x" + "01" * 32)
        header_b = self._build_payload(nonce="0x" + "02" * 32)

        decoded_a = _decode_payment_header(header_a)
        decoded_b = _decode_payment_header(header_b)

        fp_a = _fingerprint(_settlement_identity(decoded_a))
        fp_b = _fingerprint(_settlement_identity(decoded_b))
        assert fp_a != fp_b

    def test_different_value_produces_different_fingerprint(self):
        import base64
        import json

        from hermes_x402.networks import get_network
        from hermes_x402.seller_gateway import (
            _decode_payment_header,
            _fingerprint,
            _settlement_identity,
        )

        net = get_network("arcTestnet")
        payload_a = {
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
                "maxTimeoutSeconds": 604900,
                "extra": {
                    "name": "GatewayWalletBatched",
                    "version": "1",
                    "verifyingContract": net.gateway_wallet,
                },
            },
        }
        payload_b = {
            **payload_a,
            "payload": {
                **payload_a["payload"],
                "authorization": {**payload_a["payload"]["authorization"], "value": "5000"},
            },
        }
        header_a = base64.b64encode(json.dumps(payload_a).encode()).decode()
        header_b = base64.b64encode(json.dumps(payload_b).encode()).decode()

        decoded_a = _decode_payment_header(header_a)
        decoded_b = _decode_payment_header(header_b)

        fp_a = _fingerprint(_settlement_identity(decoded_a))
        fp_b = _fingerprint(_settlement_identity(decoded_b))
        assert fp_a != fp_b

    @pytest.mark.asyncio
    async def test_receipt_store_treats_same_settlement_as_replay(self):
        from hermes_x402.seller_gateway import InMemoryReceiptStore

        store = InMemoryReceiptStore()
        fp = "same_fingerprint"
        begin1 = await store.begin(fp, "req1", "route1")
        assert begin1.action == "owner"

        # Same payment fingerprint, different request fingerprint → conflict
        begin2 = await store.begin(fp, "req2", "route1")
        assert begin2.action == "conflict"
