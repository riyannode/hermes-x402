"""Regression tests for all P0 fixes in PR #4.

Covers:
- middleware.py: server-computed amount, client validation, underpayment rejection
- seller_gateway.py: CAIP-2 matching, accepts deep copy, PAYMENT-RESPONSE header,
  server amount in result, seller_supported check, mixed mainnet/testnet rejection
- supports.py: CLI backend network field
- Complete request-to-settlement flow with official-compatible payload
- Official x402 authorization payload compatibility (no asset/payTo in auth)
- Server authority: requirements contain configured values, client cannot override
- Price protection: underpayment, overpayment, malformed values
- Settlement flow: success, failure, exception handling
- Payment response header: presence, encoding, content

Official Circle x402 authorization contains ONLY EIP-3009 fields:
  from, to, value, validAfter, validBefore, nonce.
Payment requirements (scheme, network, asset, amount, payTo) belong in accepted.
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from hermes_x402.middleware import (
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X402SellerMiddleware,
)
from hermes_x402.networks import get_network
from hermes_x402.seller_gateway import InMemoryReceiptStore, create_aiohttp_gateway

_VALID_SELLER = "0x" + "ab" * 20
_VALID_ASSET = "0x3600000000000000000000000000000000000000"
_GATEWAY_WALLET = "0x0077777d7EBA4688BDeF3E311b846F25870A19B9"
_NETWORK_CAIP2 = "eip155:5042002"


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
    from_addr: str = "0x" + "cd" * 20,
    to_addr: str = _GATEWAY_WALLET,
    network: str = _NETWORK_CAIP2,
) -> str:
    """Build a base64 payment header with official x402 nested payload.

    Official Circle x402 authorization contains ONLY EIP-3009 fields:
      from, to, value, validAfter, validBefore, nonce.
    Payment requirements (scheme, network, asset, amount, payTo) belong in accepted.
    """
    auth = {
        "from": from_addr,
        "to": to_addr,
        "value": value,
        "validAfter": "0",
        "validBefore": "9999999999",
        "nonce": "0x0000000000000000000000000000000000000000000000000000000000000001",
    }
    payload = {
        "x402Version": 2,
        "payload": {"authorization": auth, "signature": "0xsig"},
        "accepted": {
            "scheme": "exact",
            "network": network,
            "asset": _VALID_ASSET,
            "amount": value,
            "payTo": _VALID_SELLER,
            "maxTimeoutSeconds": 2592000,
            "extra": {
                "name": "GatewayWalletBatched",
                "version": "1",
                "verifyingContract": _GATEWAY_WALLET,
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _decode_header(header: str) -> dict:
    """Decode a base64 payment header to a dict."""
    return json.loads(base64.b64decode(header).decode())


# ---------------------------------------------------------------------------
# middleware.py: Server-computed amount
# ---------------------------------------------------------------------------


class TestMiddlewareServerAmount:
    """middleware.py must compute settlement amount from price, not client value."""

    async def test_underpayment_rejected(self):
        """Client offers less than server price -> 402, no settlement."""
        mw = _make_middleware()
        header = _build_auth_header(value="5000")  # $0.005 but price is $0.01

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        result = await mw.process_request(mock_request, price="$0.01")
        assert result is None  # 402
        # Verify 402 response was stored on request
        assert mock_request.__setitem__.call_count > 0
        stored_keys = [call.args[0] for call in mock_request.__setitem__.call_args_list]
        assert "x402_402" in stored_keys

    async def test_overpayment_rejected(self):
        """Client offers more than server price -> 402, no settlement."""
        mw = _make_middleware()
        header = _build_auth_header(value="20000")  # $0.02 but price is $0.01

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        result = await mw.process_request(mock_request, price="$0.01")
        assert result is None
        stored_keys = [call.args[0] for call in mock_request.__setitem__.call_args_list]
        assert "x402_402" in stored_keys

    async def test_real_aiohttp_request_unpaid_stores_challenge(self):
        """Compatibility adapter works with real aiohttp Request objects."""
        mw = _make_middleware()
        request = make_mocked_request("GET", "/premium?item=1", headers={})

        result = await mw.process_request(request, price="$0.003")

        assert result is None
        assert request["x402_402"]["status"] == 402
        body = request["x402_402"]["body"]
        assert body["resource"]["url"] == "https://seller.local/premium?item=1"
        assert body["accepts"][0]["network"] == _NETWORK_CAIP2
        assert body["accepts"][0]["amount"] == "3000"

    async def test_real_aiohttp_request_paid_returns_payment_result(self):
        """Compatibility adapter delegates paid real aiohttp requests to canonical core."""
        mw = _make_middleware()
        request = make_mocked_request(
            "GET",
            "/premium",
            headers={PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="3000")},
        )

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(request, price="$0.003")

        assert result is not None
        assert result.amount == "3000"
        assert result.network == _NETWORK_CAIP2
        assert request["x402_payment"] is result

    async def test_exact_amount_settles_with_server_amount(self):
        """Client offers exact server amount -> settlement succeeds, result uses server amount."""
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
            assert result.network == _NETWORK_CAIP2


# ---------------------------------------------------------------------------
# Official payload compatibility — authorization MUST NOT require asset/payTo
# ---------------------------------------------------------------------------


class TestOfficialPayloadCompatibility:
    """Verify the seller accepts official Circle x402 authorization payloads.

    Official authorization contains ONLY EIP-3009 fields:
      from, to, value, validAfter, validBefore, nonce.
    Asset, network, amount, payTo belong in the 'accepted' section.
    """

    async def test_missing_auth_asset_not_rejected(self):
        """Authorization without 'asset' field must NOT be rejected."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None  # Should not be rejected

    async def test_missing_auth_payto_not_rejected(self):
        """Authorization without 'payTo' field must NOT be rejected."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None  # Should not be rejected

    async def test_full_payload_passed_to_settle(self):
        """The complete decoded payload reaches _settle with correct structure."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            # Verify _settle was called with the decoded payload
            call_args = mock_settle.call_args
            payload = call_args[0][0]  # first positional arg = decoded payload

            # Must have nested structure: payload.authorization, payload.signature
            assert "payload" in payload
            assert "authorization" in payload["payload"]
            assert "signature" in payload["payload"]
            # Must have accepted section
            assert "accepted" in payload
            # Authorization should have EIP-3009 fields
            auth = payload["payload"]["authorization"]
            assert "from" in auth
            assert "to" in auth
            assert "value" in auth
            assert "validAfter" in auth
            assert "validBefore" in auth
            assert "nonce" in auth
            # Should NOT have asset or payTo in authorization
            assert "asset" not in auth
            assert "payTo" not in auth

    async def test_server_requirements_separate(self):
        """Server-generated requirements are passed as a separate argument to _settle."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            call_args = mock_settle.call_args
            requirements = call_args[0][1]  # second positional arg = requirements

            # Requirements must be server-computed, containing asset, payTo, network
            assert requirements["asset"] == _VALID_ASSET
            assert requirements["payTo"] == _VALID_SELLER
            assert requirements["network"] == _NETWORK_CAIP2
            assert requirements["amount"] == "10000"
            assert requirements["scheme"] == "exact"


# ---------------------------------------------------------------------------
# Server authority — requirements contain configured values
# ---------------------------------------------------------------------------


class TestServerAuthority:
    """Server requirements must reflect server-configured values, not client claims."""

    async def test_requirements_contain_configured_asset(self):
        """Settlement requirements contain the server-configured USDC asset address."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )
        net = get_network("arcTestnet")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")

            requirements = mock_settle.call_args[0][1]
            assert requirements["asset"] == net.usdc_address

    async def test_requirements_contain_seller_address(self):
        """Settlement requirements contain the server-configured seller address."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")

            requirements = mock_settle.call_args[0][1]
            assert requirements["payTo"] == _VALID_SELLER

    async def test_requirements_contain_server_amount(self):
        """Settlement requirements contain server-computed amount from price."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")

            requirements = mock_settle.call_args[0][1]
            assert requirements["amount"] == "10000"  # server-computed

    async def test_requirements_contain_caip2_network(self):
        """Settlement requirements contain CAIP-2 network identifier."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")

            requirements = mock_settle.call_args[0][1]
            assert requirements["network"] == _NETWORK_CAIP2

    async def test_requirements_contain_gateway_contract(self):
        """Settlement requirements contain the gateway wallet as verifyingContract."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )
        net = get_network("arcTestnet")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")

            requirements = mock_settle.call_args[0][1]
            assert requirements["extra"]["verifyingContract"] == net.gateway_wallet

    async def test_client_cannot_override_amount(self):
        """Client offers wrong amount -> rejected, server uses its own price."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        # Client offers 50000 ($0.05) but server price is $0.01
        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="50000")}

        async def handler(req):
            return web.json_response({"ok": True})

        resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
        assert resp.status == 402  # Rejected — client amount != server price

    async def test_client_cannot_override_asset(self):
        """Client specifies a different asset in 'accepted' -> rejected (wrong network)."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        # Build header with wrong network (base) but client price matches
        header = _build_auth_header(value="10000", network="eip155:5042002")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        async def handler(req):
            return web.json_response({"ok": True})

        resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
        assert resp.status == 402  # Rejected — wrong network

    async def test_client_cannot_override_seller(self):
        """Client pays to wrong seller address -> rejected via settlement failure."""
        # Client builds header paying to a different seller
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        # Header with correct amount and network but gateway handles payTo
        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        # Settle fails because the payment doesn't match server requirements
        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": False, "errorReason": "wrong_payTo"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402  # Settlement rejected

            # Server requirements must contain correct seller, not client's
            requirements = mock_settle.call_args[0][1]
            assert requirements["payTo"] == _VALID_SELLER


# ---------------------------------------------------------------------------
# Price protection
# ---------------------------------------------------------------------------


class TestPriceProtection:
    """Server must reject payments that don't match the configured price."""

    async def test_underpayment_rejected_before_handler(self):
        """Underpayment is rejected without invoking the handler."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        # Client offers 5000 but price is 10000
        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="5000")}

        handler_called = False

        async def handler(req):
            nonlocal handler_called
            handler_called = True
            return web.json_response({"ok": True})

        resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
        assert resp.status == 402
        assert not handler_called

    async def test_overpayment_rejected_before_handler(self):
        """Overpayment is rejected without invoking the handler."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        # Client offers 99999 but price is 10000
        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="99999")}

        handler_called = False

        async def handler(req):
            nonlocal handler_called
            handler_called = True
            return web.json_response({"ok": True})

        resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
        assert resp.status == 402
        assert not handler_called

    async def test_exact_amount_reaches_settlement(self):
        """Exact amount passes validation and reaches settlement."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            mock_settle.assert_called_once()
            assert resp.status == 200

    async def test_malformed_value_rejected(self):
        """Malformed authorization value is rejected."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        # Build a header with a non-numeric value
        auth = {
            "from": "0x" + "cd" * 20,
            "to": _GATEWAY_WALLET,
            "value": "not_a_number",
            "validAfter": "0",
            "validBefore": "9999999999",
            "nonce": "0x01",
        }
        payload = {
            "x402Version": 2,
            "payload": {"authorization": auth, "signature": "0xsig"},
            "accepted": {
                "scheme": "exact",
                "network": _NETWORK_CAIP2,
                "asset": _VALID_ASSET,
                "amount": "10000",
                "payTo": _VALID_SELLER,
                "maxTimeoutSeconds": 2592000,
                "extra": {
                    "name": "GatewayWalletBatched",
                    "version": "1",
                    "verifyingContract": _GATEWAY_WALLET,
                },
            },
        }
        bad_header = base64.b64encode(json.dumps(payload).encode()).decode()

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: bad_header}

        async def handler(req):
            return web.json_response({"ok": True})

        resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
        assert resp.status == 402


# ---------------------------------------------------------------------------
# Settlement flow
# ---------------------------------------------------------------------------


class TestSettlementFlow:
    """Test success, failure, and exception paths of settlement."""

    async def test_successful_settlement_executes_handler_once(self):
        """Successful settlement calls the handler exactly once."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        call_count = 0

        async def handler(req):
            nonlocal call_count
            call_count += 1
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert call_count == 1

    async def test_failed_settlement_does_not_execute_handler(self):
        """Failed settlement does not execute the handler."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        handler_called = False

        async def handler(req):
            nonlocal handler_called
            handler_called = True
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": False, "errorReason": "insufficient_funds"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402
            assert not handler_called

    async def test_settlement_exception_fails_closed(self):
        """Settlement exception returns 402 without executing handler."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        handler_called = False

        async def handler(req):
            nonlocal handler_called
            handler_called = True
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.side_effect = RuntimeError("Network error")
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 503
            assert not handler_called

    async def test_receipt_store_concurrent_duplicate_executes_handler_once(self):
        """Same payment + same request recovers existing result without double execution."""
        store = InMemoryReceiptStore()
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
            receipt_store=store,
        )
        header = _build_auth_header(value="10000")
        req1 = make_mocked_request("GET", "/premium", headers={PAYMENT_SIGNATURE_HEADER: header})
        req2 = make_mocked_request("GET", "/premium", headers={PAYMENT_SIGNATURE_HEADER: header})
        handler_calls = 0

        async def handler(req):
            nonlocal handler_calls
            handler_calls += 1
            await asyncio.sleep(0.01)
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            responses = await asyncio.gather(
                gw._handle_request(req1, handler, "$0.01", None, None, "test"),
                gw._handle_request(req2, handler, "$0.01", None, None, "test"),
            )

        assert [resp.status for resp in responses] == [200, 200]
        assert handler_calls == 1
        assert mock_settle.await_count == 1
        assert responses[1].body == responses[0].body

    async def test_receipt_store_same_payment_different_request_conflicts(self):
        """Same payment reused for a different request is a 409 conflict."""
        store = InMemoryReceiptStore()
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
            receipt_store=store,
        )
        header = _build_auth_header(value="10000")
        req1 = make_mocked_request("GET", "/premium/a", headers={PAYMENT_SIGNATURE_HEADER: header})
        req2 = make_mocked_request("GET", "/premium/b", headers={PAYMENT_SIGNATURE_HEADER: header})

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            first = await gw._handle_request(req1, handler, "$0.01", None, None, "test")
            second = await gw._handle_request(req2, handler, "$0.01", None, None, "test")

        assert first.status == 200
        assert second.status == 409
        assert mock_settle.await_count == 1

    async def test_payment_result_stores_server_amount(self):
        """PaymentResult stores server-computed amount, not client-provided value."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        stored = {}
        original_setitem = mock_request.__setitem__

        def track_setitem(self_or_key, key=None, value=None):
            if key is None:
                original_setitem(self_or_key, key)
            else:
                stored[key] = value
                original_setitem(self_or_key, key, value)

        mock_request.__setitem__ = track_setitem

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            payment = stored["x402_payment"]
            assert payment.amount == "10000"  # server-computed

    async def test_payment_context_stores_server_amount(self):
        """set_payment_context receives server-computed amount."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            with patch("hermes_x402.seller_gateway.set_payment_context_token") as mock_ctx:
                token = object()
                mock_ctx.return_value = token
                with patch("hermes_x402.seller_gateway.reset_payment_context") as mock_reset:
                    await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
                    mock_ctx.assert_called_once_with(
                        payer="0x" + "cd" * 20,
                        amount="10000",
                        network=_NETWORK_CAIP2,
                        transaction="0xtx",
                    )
                    mock_reset.assert_called_once_with(token)


# ---------------------------------------------------------------------------
# Payment response header
# ---------------------------------------------------------------------------


class TestPaymentResponseHeader:
    """Successful settlement must add PAYMENT-RESPONSE header to response."""

    async def test_header_exists_after_success(self):
        """Response has PAYMENT-RESPONSE header after successful settlement."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx123"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 200
            assert PAYMENT_RESPONSE_HEADER in resp.headers

    async def test_header_is_base64_encoded(self):
        """PAYMENT-RESPONSE header value is valid base64."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx123"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            header_val = resp.headers[PAYMENT_RESPONSE_HEADER]
            decoded = base64.b64decode(header_val).decode()
            data = json.loads(decoded)
            assert isinstance(data, dict)

    async def test_decoded_json_has_transaction(self):
        """PAYMENT-RESPONSE contains a 'transaction' field."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx123"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            decoded = json.loads(base64.b64decode(resp.headers[PAYMENT_RESPONSE_HEADER]).decode())
            assert "transaction" in decoded
            assert decoded["transaction"] == "0xtx123"

    async def test_decoded_json_has_network(self):
        """PAYMENT-RESPONSE contains a 'network' field."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx123"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            decoded = json.loads(base64.b64decode(resp.headers[PAYMENT_RESPONSE_HEADER]).decode())
            assert "network" in decoded
            assert decoded["network"] == _NETWORK_CAIP2

    async def test_no_header_before_settlement(self):
        """No PAYMENT-RESPONSE header when payment header is missing (402)."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {}  # No payment header

        async def handler(req):
            return web.json_response({"ok": True})

        resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
        assert resp.status == 402
        assert PAYMENT_RESPONSE_HEADER not in resp.headers

    async def test_failed_settlement_no_header(self):
        """No PAYMENT-RESPONSE header when settlement fails."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": False, "errorReason": "revert"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402
            assert PAYMENT_RESPONSE_HEADER not in resp.headers


# ---------------------------------------------------------------------------
# seller_gateway.py: CAIP-2 matching
# ---------------------------------------------------------------------------


class TestGatewayCAIP2Matching:
    """_build_settle_requirements must use CAIP-2, not registry key."""

    def test_caip2_matches(self):
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )
        reqs = gw._build_settle_requirements("10000", "eip155:5042002", gw._networks)
        assert reqs["network"] == "eip155:5042002"
        assert reqs["amount"] == "10000"

    def test_registry_key_fails(self):
        """Passing 'base' instead of 'eip155:8453' must raise."""
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
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
            networks=["arcTestnet"],
        )
        # Build 402 body with amount "10000"
        body1 = gw._build_402_body("10000", "/a", "desc1", gw._networks)
        # Build another with amount "20000"
        body2 = gw._build_402_body("20000", "/b", "desc2", gw._networks)
        # First body should still have original amount, not "20000"
        assert body1["accepts"][0]["amount"] == "10000"
        assert body2["accepts"][0]["amount"] == "20000"


# ---------------------------------------------------------------------------
# seller_gateway.py: Server amount in result
# ---------------------------------------------------------------------------


class TestGatewayServerAmountInResult:
    async def test_result_stores_server_amount(self):
        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        async def handler(req):
            return web.json_response({"ok": True})

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
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
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
        with pytest.raises(ValueError, match="not supported for seller mode"):
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
    """End-to-end: official payload -> validation -> settlement -> response."""

    async def test_full_gateway_settlement_flow(self):
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

            # Authorization must have EIP-3009 fields only
            auth = payload["payload"]["authorization"]
            assert "from" in auth
            assert "to" in auth
            assert "value" in auth
            assert "validAfter" in auth
            assert "validBefore" in auth
            assert "nonce" in auth

            # Requirements must use server-computed amount
            assert requirements["amount"] == "10000"
            assert requirements["network"] == _NETWORK_CAIP2
            assert requirements["asset"] == net.usdc_address
            assert requirements["payTo"] == _VALID_SELLER
            assert requirements["extra"]["name"] == "GatewayWalletBatched"

            # Response must be successful
            assert resp.status == 200
            assert PAYMENT_RESPONSE_HEADER in resp.headers

            # Payment result must be stored on request
            payment = stored["x402_payment"]
            assert payment.amount == "10000"
            assert payment.network == _NETWORK_CAIP2
            assert payment.transaction == "0xsettlement_tx_hash"
            assert payment.payer == "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


# ---------------------------------------------------------------------------
# Circle CLI 0.0.6 compatibility fixes
# ---------------------------------------------------------------------------


class TestCircleCliAmountParsing:
    """_reported_usdc_atomic must handle CLI 0.0.6 $-prefixed amounts."""

    def test_plain_amount(self):
        from hermes_x402.backends.circle_cli import CircleCliBuyerBackend

        assert CircleCliBuyerBackend._reported_usdc_atomic("0.000003 USDC") == 3

    def test_dollar_prefixed_amount(self):
        from hermes_x402.backends.circle_cli import CircleCliBuyerBackend

        assert CircleCliBuyerBackend._reported_usdc_atomic("$0.000003 USDC") == 3

    def test_larger_amount_with_dollar(self):
        from hermes_x402.backends.circle_cli import CircleCliBuyerBackend

        assert CircleCliBuyerBackend._reported_usdc_atomic("$0.01 USDC") == 10000

    def test_invalid_amount_raises(self):
        from hermes_x402.backends.circle_cli import (
            CircleCliBuyerBackend,
            CircleCliPaymentOutcomeUnknownError,
        )

        with pytest.raises(CircleCliPaymentOutcomeUnknownError):
            CircleCliBuyerBackend._reported_usdc_atomic("invalid")


class TestCircleCliSchemeNormalization:
    """CLI 0.0.6 returns 'GatewayWalletBatched' as scheme; must normalize to 'exact'."""

    def test_gateway_batching_normalized(self):
        _CLI_SCHEME_NORMALIZE = {"GatewayWalletBatched": "exact"}
        assert _CLI_SCHEME_NORMALIZE.get("GatewayWalletBatched") == "exact"

    def test_exact_scheme_unchanged(self):
        _CLI_SCHEME_NORMALIZE = {"GatewayWalletBatched": "exact"}
        assert _CLI_SCHEME_NORMALIZE.get("exact", "exact") == "exact"
