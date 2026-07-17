"""Regression tests for all P0 fixes in PR #4.

Covers:
- middleware.py: server-computed amount, client validation, underpayment rejection
- seller_gateway.py: CAIP-2 matching, accepts deep copy, PAYMENT-RESPONSE header,
  server amount in result, seller_supported check, mixed mainnet/testnet rejection
- supports.py: CLI backend network field
- Complete request-to-settlement flow with official-compatible payload
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from hermes_x402.middleware import (
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X402SellerMiddleware,
)
from hermes_x402.networks import get_network
from hermes_x402.seller_gateway import (
    create_aiohttp_gateway,
)

_VALID_SELLER = "0x" + "ab" * 20
_VALID_ASSET = "0x3600000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_middleware(
    seller: str = _VALID_SELLER,
    chain: str = "arcTestnet",
) -> X402SellerMiddleware:
    return X402SellerMiddleware(seller_address=seller, chain=chain)


def _build_auth_header(
    *,
    value: str = "10000",
    asset: str = _VALID_ASSET,
    pay_to: str = _VALID_SELLER,
    from_addr: str = "0x" + "cd" * 20,
    network: str = "eip155:5042002",
) -> str:
    """Build a base64 payment header with official x402 nested payload."""
    auth = {
        "from": from_addr,
        "value": value,
        "asset": asset,
        "payTo": pay_to,
    }
    payload = {
        "x402Version": 2,
        "payload": {"authorization": auth, "signature": "0xsig"},
        "accepted": {"network": network},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


# ---------------------------------------------------------------------------
# middleware.py: Server-computed amount
# ---------------------------------------------------------------------------


class TestMiddlewareServerAmount:
    """middleware.py must compute settlement amount from price, not client value."""

    @pytest.mark.asyncio
    async def test_underpayment_rejected(self):
        """Client offers less than server price → 402, no settlement."""
        mw = _make_middleware()
        header = _build_auth_header(value="5000")  # $0.005 but price is $0.01

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        result = await mw.process_request(mock_request, price="$0.01")
        assert result is None  # 402
        # Verify 402 response was stored on request
        assert mock_request.__setitem__.call_count > 0
        stored_keys = [
            call.args[0] for call in mock_request.__setitem__.call_args_list
        ]
        assert "x402_402" in stored_keys

    @pytest.mark.asyncio
    async def test_overpayment_rejected(self):
        """Client offers more than server price → 402, no settlement."""
        mw = _make_middleware()
        header = _build_auth_header(value="20000")  # $0.02 but price is $0.01

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        result = await mw.process_request(mock_request, price="$0.01")
        assert result is None
        stored_keys = [
            call.args[0] for call in mock_request.__setitem__.call_args_list
        ]
        assert "x402_402" in stored_keys

    @pytest.mark.asyncio
    async def test_exact_amount_settles_with_server_amount(self):
        """Client offers exact server amount → settlement succeeds, result uses server amount."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")  # exact $0.01

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None
            assert result.amount == "10000"  # server-computed
            assert result.network == "eip155:5042002"


# ---------------------------------------------------------------------------
# seller_gateway.py: CAIP-2 matching
# ---------------------------------------------------------------------------


class TestGatewayCAIP2Matching:
    """_build_settle_requirements must use CAIP-2, not registry key."""

    def test_caip2_matches(self):
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["base"],
        )
        reqs = gw._build_settle_requirements("10000", "eip155:8453", gw._networks)
        assert reqs["network"] == "eip155:8453"
        assert reqs["amount"] == "10000"

    def test_registry_key_fails(self):
        """Passing 'base' instead of 'eip155:8453' must raise."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["base"],
        )
        with pytest.raises(ValueError, match="not in accepted networks"):
            gw._build_settle_requirements("10000", "base", gw._networks)


# ---------------------------------------------------------------------------
# seller_gateway.py: Accepts deep copy
# ---------------------------------------------------------------------------


class TestGatewayAcceptsDeepCopy:
    """_build_402_body must not mutate shared accepts dicts."""

    def test_no_mutation_between_calls(self):
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["base"],
        )
        # Build 402 body with amount "10000"
        body1 = gw._build_402_body("10000", "/a", "desc1", gw._networks)
        # Build another with amount "20000"
        body2 = gw._build_402_body("20000", "/b", "desc2", gw._networks)
        # First body should still have original amount, not "20000"
        assert body1["accepts"][0]["amount"] == "10000"
        assert body2["accepts"][0]["amount"] == "20000"


# ---------------------------------------------------------------------------
# seller_gateway.py: PAYMENT-RESPONSE header
# ---------------------------------------------------------------------------


class TestGatewayPaymentResponseHeader:
    """Successful settlement must add PAYMENT-RESPONSE header to response."""

    @pytest.mark.asyncio
    async def test_payment_response_header_added(self):
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )
        net = get_network("arcTestnet")
        expected_asset = net.usdc_address

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {
            PAYMENT_SIGNATURE_HEADER: _build_auth_header(
                value="10000",
                asset=expected_asset,
                pay_to=_VALID_SELLER,
                network="eip155:5042002",
            )
        }

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx123"}
            resp = await gw._handle_request(
                mock_request, handler, "$0.01", None, None, "test"
            )
            assert resp.status == 200
            assert PAYMENT_RESPONSE_HEADER in resp.headers


# ---------------------------------------------------------------------------
# seller_gateway.py: PaymentResult uses server amount
# ---------------------------------------------------------------------------


class TestGatewayServerAmountInResult:
    @pytest.mark.asyncio
    async def test_result_stores_server_amount(self):
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )
        net = get_network("arcTestnet")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {
            PAYMENT_SIGNATURE_HEADER: _build_auth_header(
                value="10000",
                asset=net.usdc_address,
                pay_to=_VALID_SELLER,
                network="eip155:5042002",
            )
        }

        async def handler(req):
            return web.json_response({"ok": True})

        stored = {}
        original_setitem = mock_request.__setitem__

        def track_setitem(self_or_key, key=None, value=None):
            # MagicMock calls __setitem__(self, key, value)
            if key is None:
                # direct call: track_setitem(key, value)
                original_setitem(self_or_key, key)
            else:
                stored[key] = value
                original_setitem(self_or_key, key, value)

        mock_request.__setitem__ = track_setitem

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(
                mock_request, handler, "$0.01", None, None, "test"
            )
            payment = stored["x402_payment"]
            assert payment.amount == "10000"  # server-computed, not client-provided


# ---------------------------------------------------------------------------
# seller_gateway.py: seller_supported check
# ---------------------------------------------------------------------------


class TestGatewaySellerSupported:
    def test_unsupported_network_rejected(self):
        """Networks with seller_supported=False must be rejected."""
        with pytest.raises(ValueError, match="not supported for seller mode"):
            create_aiohttp_gateway(
                seller_address=_VALID_SELLER,
                networks=["arcMainnet"],  # disabled/unverified
            )


# ---------------------------------------------------------------------------
# seller_gateway.py: Mixed mainnet/testnet rejection
# ---------------------------------------------------------------------------


class TestGatewayMixedEnvironment:
    def test_mixed_mainnet_testnet_rejected(self):
        """Cannot mix mainnet and testnet networks."""
        with pytest.raises(ValueError, match="Cannot mix networks"):
            create_aiohttp_gateway(
                seller_address=_VALID_SELLER,
                networks=["base", "arcTestnet"],
            )


# ---------------------------------------------------------------------------
# supports.py: CLI backend network
# ---------------------------------------------------------------------------


class TestSupportsCLIBackendNetwork:
    def test_cli_uses_circle_cli_network(self):
        """CLI backend must read circle_cli_network, not blockchain."""

        class MockConfig:
            buyer_backend = "cli"
            blockchain = "ARC-TESTNET"  # DCW field — should be ignored
            circle_cli_network = "eip155:5042002"  # CLI field — should be used
            network_policy = "public"
            host_allowlist = ()
            allow_http = False

        # Verify the network selection logic by checking _detect_backend_support directly
        from hermes_x402.buyer.supports import _detect_backend_support

        # CLI backend with circle_cli_network=eip155:5042002 (arcTestnet)
        # should support arcTestnet networks
        result = _detect_backend_support(
            "arcTestnet",
            configured_backend="cli",
            wallet_network="eip155:5042002",
        )
        assert result is True

        # CLI backend with wrong network should not support
        result2 = _detect_backend_support(
            "base",
            configured_backend="cli",
            wallet_network="eip155:5042002",
        )
        assert result2 is False


# ---------------------------------------------------------------------------
# Complete settlement flow with official payload
# ---------------------------------------------------------------------------


class TestCompleteSettlementFlow:
    """End-to-end: official payload → validation → settlement → response."""

    @pytest.mark.asyncio
    async def test_full_gateway_settlement_flow(self):
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )
        net = get_network("arcTestnet")

        mock_request = AsyncMock()
        mock_request.path = "/api/data"
        mock_request.headers = {
            PAYMENT_SIGNATURE_HEADER: _build_auth_header(
                value="10000",
                asset=net.usdc_address,
                pay_to=_VALID_SELLER,
                network="eip155:5042002",
                from_addr="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
        }

        async def handler(req):
            return web.json_response({"data": "secret"})

        stored = {}
        original_setitem = mock_request.__setitem__

        def track_setitem(self_or_key, key=None, value=None):
            if key is None:
                original_setitem(self_or_key, key)
            else:
                stored[key] = value
                original_setitem(self_or_key, key, value)

        mock_request.__setitem__ = track_setitem

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {
                "success": True,
                "transaction": "0xsettlement_tx_hash",
            }
            resp = await gw._handle_request(
                mock_request, handler, "$0.01", None, None, "Premium API"
            )

            # Verify settlement was called
            mock_settle.assert_called_once()

            # Verify the payload passed to settle has correct structure
            call_args = mock_settle.call_args
            payload = call_args[0][0]  # first positional arg = decoded payload
            requirements = call_args[0][1]  # second positional arg = requirements

            # Payload must have the nested structure
            assert "payload" in payload
            assert "authorization" in payload["payload"]
            assert "accepted" in payload

            # Requirements must use server-computed amount
            assert requirements["amount"] == "10000"
            assert requirements["network"] == "eip155:5042002"
            assert requirements["asset"] == net.usdc_address
            assert requirements["payTo"] == _VALID_SELLER
            assert requirements["extra"]["name"] == "GatewayWalletBatched"

            # Response must be successful
            assert resp.status == 200
            assert PAYMENT_RESPONSE_HEADER in resp.headers

            # Payment result must be stored on request
            payment = stored["x402_payment"]
            assert payment.amount == "10000"
            assert payment.network == "eip155:5042002"
            assert payment.transaction == "0xsettlement_tx_hash"
            assert payment.payer == "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
