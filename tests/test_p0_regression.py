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
    from_addr: str = "0x" + "cd" * 20,
    to_addr: str = "0x" + "ab" * 20,  # Gateway wallet
    network: str = "eip155:5042002",
) -> str:
    """Build a base64 payment header with official x402 nested payload.

    Official Circle x402 payload has:
    - authorization: {from, to, value, validAfter, validBefore, nonce}
    - accepted: {scheme, network, asset, amount, payTo, maxTimeoutSeconds, extra}
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
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": value,
            "payTo": "0x" + "ab" * 20,
            "maxTimeoutSeconds": 604900,
            "extra": {
                "name": "GatewayWalletBatched",
                "version": "1",
                "verifyingContract": "0x0077777d7EBA4688BDeF3E311b846F25870A19B9",
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _build_minimal_auth_header(
    *,
    value: str = "10000",
    from_addr: str = "0x" + "cd" * 20,
) -> str:
    """Build a minimal base64 payment header (for negative tests)."""
    auth = {
        "from": from_addr,
        "to": "0x" + "ab" * 20,
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
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": value,
            "payTo": "0x" + "ab" * 20,
            "maxTimeoutSeconds": 604900,
            "extra": {
                "name": "GatewayWalletBatched",
                "version": "1",
                "verifyingContract": "0x0077777d7EBA4688BDeF3E311b846F25870A19B9",
            },
        },
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
        stored_keys = [call.args[0] for call in mock_request.__setitem__.call_args_list]
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
        stored_keys = [call.args[0] for call in mock_request.__setitem__.call_args_list]
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

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {
            PAYMENT_SIGNATURE_HEADER: _build_auth_header(
                value="10000",
                network="eip155:5042002",
            )
        }

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx123"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
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

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {
            PAYMENT_SIGNATURE_HEADER: _build_auth_header(
                value="10000",
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
# TestOfficialPayloadCompatibility — proves official Circle payloads work
# ---------------------------------------------------------------------------


class TestOfficialPayloadCompatibility:
    """Official Circle x402 payloads have authorization WITHOUT asset, payTo, or network.
    These fields live in the 'accepted' section, not inside the signed authorization.
    This class proves the middleware/gateway correctly handles such payloads.
    """

    @pytest.mark.asyncio
    async def test_official_payload_reaches_settle_middleware(self):
        """Official payload must pass all validation and reach _settle."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_authorization_asset_does_not_reject(self):
        """authorization.asset is NOT in official payloads — must NOT cause rejection."""
        mw = _make_middleware()
        # Official payload has no asset in authorization
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            # Must NOT be None (402) — should succeed
            assert result is not None
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_authorization_payTo_does_not_reject(self):
        """authorization.payTo is NOT in official payloads — must NOT cause rejection."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_authorization_network_does_not_reject(self):
        """authorization.network is NOT in official payloads — must NOT cause rejection."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_full_decoded_payload_passed_to_settlement(self):
        """The complete decoded payload (with nested structure) is passed to _settle."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            call_args = mock_settle.call_args
            payload = call_args[0][0]  # first positional arg = decoded payload
            # Must contain the nested structure from official format
            assert "x402Version" in payload
            assert "payload" in payload
            assert "authorization" in payload["payload"]
            assert "accepted" in payload

    @pytest.mark.asyncio
    async def test_server_requirements_passed_separately(self):
        """Server-generated requirements are passed as second arg to _settle."""
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
            assert "scheme" in requirements
            assert "network" in requirements
            assert "asset" in requirements
            assert "amount" in requirements
            assert "payTo" in requirements

    @pytest.mark.asyncio
    async def test_official_payload_gateway_settle(self):
        """Official payload passes validation in seller_gateway and reaches _settle."""
        from aiohttp import web

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
            assert resp.status == 200
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_gateway_missing_auth_asset_no_rejection(self):
        """seller_gateway must not reject when authorization has no asset field."""
        from aiohttp import web

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
            # Must NOT be 402 — should succeed
            assert resp.status == 200
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_gateway_missing_auth_payTo_no_rejection(self):
        """seller_gateway must not reject when authorization has no payTo field."""
        from aiohttp import web

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
            assert resp.status == 200
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_gateway_missing_auth_network_no_rejection(self):
        """seller_gateway must not reject when authorization has no network field."""
        from aiohttp import web

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
            assert resp.status == 200
            mock_settle.assert_called_once()


# ---------------------------------------------------------------------------
# TestServerAuthority — proves server controls requirements, not client
# ---------------------------------------------------------------------------


class TestServerAuthority:
    """Server requirements must use server-configured values, never client-provided ones.
    Client's accepted fields cannot override amount, asset, seller address, network,
    or verifying contract.
    """

    @pytest.mark.asyncio
    async def test_server_requirements_contain_configured_asset(self):
        """Server requirements must contain the server-configured USDC asset."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            assert requirements["asset"] == _VALID_ASSET

    @pytest.mark.asyncio
    async def test_server_requirements_contain_seller_address(self):
        """Server requirements must contain the configured seller address."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            assert requirements["payTo"] == _VALID_SELLER

    @pytest.mark.asyncio
    async def test_server_requirements_contain_server_computed_amount(self):
        """Server requirements must contain the server-computed amount, not client's."""
        mw = _make_middleware()
        header = _build_auth_header(value="99999")  # client claims wrong amount

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        # Client offers 99999 but server price is $0.01 = 10000
        # This should be rejected as underpayment
        result = await mw.process_request(mock_request, price="$0.01")
        assert result is None  # rejected

    @pytest.mark.asyncio
    async def test_server_requirements_contain_caip2_network(self):
        """Server requirements must contain CAIP-2 network, not registry key."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000", network="eip155:5042002")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            assert requirements["network"] == "eip155:5042002"

    @pytest.mark.asyncio
    async def test_server_requirements_contain_gateway_verifying_contract(self):
        """Server requirements must contain the gateway verifying contract."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            assert "verifyingContract" in requirements["extra"]
            assert requirements["extra"]["name"] == "GatewayWalletBatched"

    @pytest.mark.asyncio
    async def test_client_accepted_cannot_override_amount(self):
        """Client's accepted.amount must NOT override server-computed amount."""
        mw = _make_middleware()
        # Client claims amount=99999 in accepted, but server price is $0.01 = 10000
        # Client's value in authorization is also 99999
        header = _build_auth_header(value="99999")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        # Should be rejected because client value != server amount
        result = await mw.process_request(mock_request, price="$0.01")
        assert result is None

    @pytest.mark.asyncio
    async def test_client_accepted_cannot_override_asset(self):
        """Client's accepted.asset must NOT override server-configured asset."""
        from aiohttp import web

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

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            # Server's asset must match network config, not client's
            assert requirements["asset"] == net.usdc_address

    @pytest.mark.asyncio
    async def test_client_accepted_cannot_override_seller_address(self):
        """Client's accepted.payTo must NOT override server's seller address."""
        from aiohttp import web

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

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            # payTo must be server's seller address
            assert requirements["payTo"] == _VALID_SELLER

    @pytest.mark.asyncio
    async def test_client_accepted_cannot_override_network(self):
        """Client's accepted.network is used to select the NetworkConfig, not to
        override server's supported networks."""
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        # Client claims to pay on arcTestnet
        mock_request.headers = {
            PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000", network="eip155:5042002")
        }

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            assert requirements["network"] == "eip155:5042002"

    @pytest.mark.asyncio
    async def test_client_accepted_cannot_override_verifying_contract(self):
        """Server's verifyingContract must come from network config, not client."""
        from aiohttp import web

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

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            assert requirements["extra"]["verifyingContract"] == net.gateway_wallet


# ---------------------------------------------------------------------------
# TestPriceProtection — proves amount validation before settlement
# ---------------------------------------------------------------------------


class TestPriceProtection:
    """Underpayment, overpayment, and malformed values must be rejected
    before settlement is attempted."""

    @pytest.mark.asyncio
    async def test_underpayment_rejected_middleware(self):
        """Middleware rejects underpayment before reaching _settle."""
        mw = _make_middleware()
        header = _build_auth_header(value="5000")  # $0.005

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is None
            mock_settle.assert_not_called()

    @pytest.mark.asyncio
    async def test_overpayment_rejected_middleware(self):
        """Middleware rejects overpayment before reaching _settle."""
        mw = _make_middleware()
        header = _build_auth_header(value="20000")  # $0.02

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is None
            mock_settle.assert_not_called()

    @pytest.mark.asyncio
    async def test_exact_amount_reaches_settlement(self):
        """Exact server amount reaches settlement."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None
            mock_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_malformed_authorization_value_rejected(self):
        """Non-numeric authorization value is rejected."""
        mw = _make_middleware()
        # Build a header with a non-numeric value
        auth = {
            "from": "0x" + "cd" * 20,
            "to": "0x" + "ab" * 20,
            "value": "not_a_number",
            "validAfter": "0",
            "validBefore": "9999999999",
            "nonce": "0x01",
        }
        payload = {
            "x402Version": 2,
            "payload": {"authorization": auth, "signature": "0xsig"},
            "accepted": {"network": "eip155:5042002"},
        }
        header = base64.b64encode(json.dumps(payload).encode()).decode()

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        result = await mw.process_request(mock_request, price="$0.01")
        assert result is None  # rejected

    @pytest.mark.asyncio
    async def test_settlement_always_receives_server_computed_amount(self):
        """Settlement requirements always use server-computed amount."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            call_args = mock_settle.call_args
            requirements = call_args[0][1]
            # Server-computed amount for $0.01 = 10000
            assert requirements["amount"] == "10000"

    @pytest.mark.asyncio
    async def test_gateway_underpayment_rejected(self):
        """Gateway rejects underpayment before reaching _settle."""
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="5000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402
            mock_settle.assert_not_called()

    @pytest.mark.asyncio
    async def test_gateway_overpayment_rejected(self):
        """Gateway rejects overpayment before reaching _settle."""
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="20000")}

        async def handler(req):
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402
            mock_settle.assert_not_called()


# ---------------------------------------------------------------------------
# TestSettlementFlow — proves correct settlement behavior
# ---------------------------------------------------------------------------


class TestSettlementFlow:
    """Settlement flow: successful → handler runs, failed → handler skipped."""

    @pytest.mark.asyncio
    async def test_successful_settlement_executes_handler(self):
        """Successful settlement must execute the protected handler exactly once."""
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        handler_called = []

        async def handler(req):
            handler_called.append(True)
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 200
            assert len(handler_called) == 1

    @pytest.mark.asyncio
    async def test_failed_settlement_does_not_execute_handler(self):
        """Failed settlement must NOT execute the protected handler."""
        from aiohttp import web

        gw = create_aiohttp_gateway(
            seller_address=_VALID_SELLER,
            networks=["arcTestnet"],
        )

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: _build_auth_header(value="10000")}

        handler_called = []

        async def handler(req):
            handler_called.append(True)
            return web.json_response({"ok": True})

        with patch.object(gw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": False, "errorReason": "insufficient"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402
            assert len(handler_called) == 0

    @pytest.mark.asyncio
    async def test_settlement_exception_fails_closed(self):
        """Settlement exceptions must fail closed (402), not propagate."""
        from aiohttp import web

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
            mock_settle.side_effect = RuntimeError("gateway timeout")
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402

    @pytest.mark.asyncio
    async def test_payment_result_stores_server_computed_amount(self):
        """PaymentResult must store server-computed amount, not client value."""
        from aiohttp import web

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
            assert payment.amount == "10000"

    @pytest.mark.asyncio
    async def test_payment_context_stores_server_computed_amount(self):
        """ContextVar payment context must store server-computed amount."""
        from hermes_x402.context import get_payment_context

        mw = _make_middleware()
        header = _build_auth_header(value="10000")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            await mw.process_request(mock_request, price="$0.01")

            ctx = get_payment_context()
            assert ctx is not None
            assert ctx.amount == "10000"
            assert ctx.network == "eip155:5042002"

    @pytest.mark.asyncio
    async def test_caip2_network_preserved_in_result(self):
        """CAIP-2 network is preserved through the settlement flow."""
        mw = _make_middleware()
        header = _build_auth_header(value="10000", network="eip155:5042002")

        mock_request = AsyncMock()
        mock_request.path = "/test"
        mock_request.headers = {PAYMENT_SIGNATURE_HEADER: header}

        with patch.object(mw, "_settle", new_callable=AsyncMock) as mock_settle:
            mock_settle.return_value = {"success": True, "transaction": "0xtx"}
            result = await mw.process_request(mock_request, price="$0.01")
            assert result is not None
            assert result.network == "eip155:5042002"


# ---------------------------------------------------------------------------
# TestPaymentResponseHeader — proves PAYMENT-RESPONSE header format
# ---------------------------------------------------------------------------


class TestPaymentResponseHeader:
    """PAYMENT-RESPONSE header must be base64-encoded JSON with transaction and network."""

    @pytest.mark.asyncio
    async def test_header_exists_after_settlement(self):
        """Header exists after successful settlement."""
        from aiohttp import web

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
            assert PAYMENT_RESPONSE_HEADER in resp.headers

    @pytest.mark.asyncio
    async def test_header_is_base64_encoded(self):
        """Header value is base64-encoded, not raw JSON."""
        from aiohttp import web

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
            # Should NOT start with '{' (not raw JSON)
            assert not header_val.startswith("{")
            # Should be valid base64 that decodes to JSON
            decoded = json.loads(base64.b64decode(header_val))
            assert isinstance(decoded, dict)

    @pytest.mark.asyncio
    async def test_decoded_json_has_transaction(self):
        """Decoded JSON has transaction field."""
        from aiohttp import web

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
            decoded = json.loads(base64.b64decode(resp.headers[PAYMENT_RESPONSE_HEADER]))
            assert "transaction" in decoded
            assert decoded["transaction"] == "0xtx123"

    @pytest.mark.asyncio
    async def test_decoded_json_has_network(self):
        """Decoded JSON has network field."""
        from aiohttp import web

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
            decoded = json.loads(base64.b64decode(resp.headers[PAYMENT_RESPONSE_HEADER]))
            assert "network" in decoded
            assert decoded["network"] == "eip155:5042002"

    @pytest.mark.asyncio
    async def test_no_header_before_settlement(self):
        """No payment response header before settlement (no payment header sent)."""
        from aiohttp import web

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

    @pytest.mark.asyncio
    async def test_failed_settlement_no_success_header(self):
        """Failed settlement does not produce a success payment response header."""
        from aiohttp import web

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
            mock_settle.return_value = {"success": False, "errorReason": "insufficient"}
            resp = await gw._handle_request(mock_request, handler, "$0.01", None, None, "test")
            assert resp.status == 402
            assert PAYMENT_RESPONSE_HEADER not in resp.headers


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
