"""Backward-compatible aiohttp middleware adapter for x402 seller mode.

The canonical seller engine lives in :mod:`hermes_x402.seller_gateway`.  This
module preserves historical imports and the ``process_request`` API while
delegating parsing, challenge construction, validation, settlement, response
semantics, and ContextVar lifecycle to the canonical engine.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from aiohttp import web

from hermes_x402.seller_gateway import (
    CIRCLE_BATCHING_NAME,
    CIRCLE_BATCHING_SCHEME,
    CIRCLE_BATCHING_VERSION,
    DEFAULT_MAX_TIMEOUT_SECONDS,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X402_CHALLENGE_KEY,
    X402_ERROR_KEY,
    X402_PAYMENT_KEY,
    X402_VERSION,
    CircleFacilitatorClient,
    FacilitatorOutcome,
    FacilitatorSettlementResult,
    InMemoryReceiptStore,
    PaymentResult,
    ReceiptStore,
    X402Gateway,
    create_aiohttp_gateway,
    get_x402_challenge,
    get_x402_payment,
    set_x402_challenge,
)


class X402SellerMiddleware:
    """Compatibility adapter around :class:`seller_gateway.X402Gateway`.

    Historical usage called ``process_request(request, price)`` and then read
    ``request["x402_402"]`` when it returned ``None``.  That contract is kept,
    but the underlying behavior is canonical gateway behavior.
    """

    def __init__(
        self,
        seller_address: str,
        chain: str = "arcTestnet",
        facilitator_url: Optional[str] = None,
        description: str = "Paid resource",
        networks: Optional[list[str]] = None,
        *,
        public_base_url: str | None = None,
        allow_http: bool = False,
        receipt_store: ReceiptStore | None = None,
    ):
        resolved_networks = networks or [chain]
        self.seller_address = seller_address
        self.chain = chain
        self.description = description
        self._gateway = create_aiohttp_gateway(
            seller_address=seller_address,
            networks=resolved_networks,
            facilitator_url=facilitator_url,
            default_description=description,
            public_base_url=public_base_url
            or os.environ.get("X402_PUBLIC_BASE_URL", "https://seller.local"),
            allow_http=allow_http,
            receipt_store=receipt_store,
        )
        self._facilitator_url = self._gateway._facilitator_url
        self._networks = [n.caip2 for n in self._gateway._networks]
        self._accepted_chains = {n.caip2: n for n in self._gateway._networks}
        self._canonical_settle = self._gateway._settle

    def _build_requirements(self, amount: str, network: str) -> dict:
        return self._gateway._build_settle_requirements(amount, network, self._gateway._networks)

    def _build_402_response(self, amount: str, path: str) -> dict:
        body = self._gateway._build_402_body(
            amount,
            self.description,
            self._gateway._networks,
            None,
            path=path,
        )
        return {
            "status": 402,
            "headers": {
                PAYMENT_REQUIRED_HEADER: self._gateway._challenge_response(body).headers[
                    PAYMENT_REQUIRED_HEADER
                ]
            },
            "body": body,
        }

    async def _settle(self, payload: dict, requirements: dict) -> FacilitatorSettlementResult:
        return await self._canonical_settle(payload, requirements)

    async def process_request(
        self,
        request: web.Request,
        price: str | Decimal,
    ) -> Optional[PaymentResult]:
        async def _sentinel_handler(req: web.Request) -> web.Response:
            return web.Response(status=204)

        # Ensure tests that patch middleware._settle still affect the canonical path.
        self._gateway._settle = self._settle  # type: ignore[method-assign]
        response = await self._gateway._handle_request(
            request,
            _sentinel_handler,
            price,
            None,
            None,
            self.description,
        )
        if response.status == 204:
            candidate = get_x402_payment(request)
            if candidate is not None:
                return candidate
        if response.status == 402:
            try:
                body = response.body.decode("utf-8") if isinstance(response.body, bytes) else "{}"
                import json

                parsed = json.loads(body)
            except Exception:
                parsed = {}
            set_x402_challenge(
                request,
                {
                    "status": 402,
                    "headers": {
                        PAYMENT_REQUIRED_HEADER: response.headers.get(PAYMENT_REQUIRED_HEADER, "")
                    },
                    "body": parsed,
                },
            )
            return None
        request[X402_ERROR_KEY] = {"status": response.status}  # type: ignore[index]
        return None

    @staticmethod
    def _price_to_amount(price: str | Decimal) -> str:
        from hermes_x402.seller_gateway import _parse_price

        return _parse_price(price)


def create_aiohttp_middleware(
    seller_address: str,
    chain: str = "arcTestnet",
    facilitator_url: Optional[str] = None,
    description: str = "Paid resource",
    networks: Optional[list[str]] = None,
    *,
    public_base_url: str | None = None,
    allow_http: bool = False,
    receipt_store: ReceiptStore | None = None,
) -> X402SellerMiddleware:
    return X402SellerMiddleware(
        seller_address=seller_address,
        chain=chain,
        facilitator_url=facilitator_url,
        description=description,
        networks=networks,
        public_base_url=public_base_url,
        allow_http=allow_http,
        receipt_store=receipt_store,
    )


__all__ = [
    "PAYMENT_SIGNATURE_HEADER",
    "PAYMENT_REQUIRED_HEADER",
    "PAYMENT_RESPONSE_HEADER",
    "CIRCLE_BATCHING_SCHEME",
    "CIRCLE_BATCHING_NAME",
    "CIRCLE_BATCHING_VERSION",
    "X402_VERSION",
    "DEFAULT_MAX_TIMEOUT_SECONDS",
    "PaymentResult",
    "X402_PAYMENT_KEY",
    "X402_CHALLENGE_KEY",
    "get_x402_payment",
    "get_x402_challenge",
    "X402SellerMiddleware",
    "create_aiohttp_middleware",
    "X402Gateway",
    "create_aiohttp_gateway",
    "CircleFacilitatorClient",
    "FacilitatorOutcome",
    "FacilitatorSettlementResult",
    "ReceiptStore",
    "InMemoryReceiptStore",
]
