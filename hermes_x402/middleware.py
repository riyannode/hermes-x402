"""aiohttp middleware for x402 seller mode.

Wraps Circle Gateway settle() directly — skips verify() for lower latency,
matching Circle's official recommendation:
"Use settle() directly rather than calling verify() followed by settle()."

Wire format matches circlekit and x402-header-agent:
{
  "x402Version": 2,
  "payload": {"authorization": {...}, "signature": "..."},
  "resource": {...},
  "accepted": {...}
}
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from aiohttp import web

from hermes_x402.config import ARC_TESTNET
from hermes_x402.context import set_payment_context

logger = logging.getLogger("hermes_x402.middleware")

PAYMENT_SIGNATURE_HEADER = "Payment-Signature"
PAYMENT_REQUIRED_HEADER = "Payment-Required"
PAYMENT_RESPONSE_HEADER = "Payment-Response"

# Circle Gateway constants
CIRCLE_BATCHING_SCHEME = "exact"
CIRCLE_BATCHING_NAME = "GatewayWalletBatched"
CIRCLE_BATCHING_VERSION = "1"
X402_VERSION = 2
DEFAULT_MAX_TIMEOUT_SECONDS = 604900


@dataclass
class PaymentResult:
    """Result of a successful payment settlement."""

    payer: str
    amount: str
    network: str
    transaction: Optional[str] = None


class X402SellerMiddleware:
    """aiohttp middleware that settles x402 payments via Circle Gateway.

    Flow:
        1. Check for Payment-Signature header
        2. If missing → return 402 + Payment-Required header
        3. If present → decode nested payload, build requirements, call settle()
        4. If settle succeeds → set payment context, call next handler
        5. If settle fails → return 402 with error
    """

    def __init__(
        self,
        seller_address: str,
        chain: str = "arcTestnet",
        facilitator_url: Optional[str] = None,
        description: str = "Paid resource",
        networks: Optional[list[str]] = None,
    ):
        self.seller_address = seller_address
        self.chain = chain
        self.description = description

        # Resolve chain config
        if chain == "arcTestnet":
            self._chain_config = ARC_TESTNET
        else:
            raise ValueError(f"Unsupported chain: {chain}. Use arcTestnet.")

        self._facilitator_url = facilitator_url or self._chain_config["facilitator_url"]
        self._networks = networks or [self._chain_config["network"]]

        # Build accepted chains map
        self._accepted_chains: dict[str, dict] = {}
        for net in self._networks:
            if net == self._chain_config["network"]:
                self._accepted_chains[net] = self._chain_config
            else:
                raise ValueError(f"Network {net} not configured for chain {chain}")

    def _build_requirements(self, amount: str, network: str) -> dict[str, Any]:
        """Build server-side payment requirements for settle()."""
        cc = self._accepted_chains[network]
        return {
            "scheme": CIRCLE_BATCHING_SCHEME,
            "network": network,
            "asset": cc["usdc"],
            "amount": amount,
            "payTo": self.seller_address,
            "maxTimeoutSeconds": DEFAULT_MAX_TIMEOUT_SECONDS,
            "extra": {
                "name": CIRCLE_BATCHING_NAME,
                "version": CIRCLE_BATCHING_VERSION,
                "verifyingContract": cc["gateway_wallet"],
            },
        }

    def _build_402_response(self, amount: str, path: str) -> dict[str, Any]:
        """Build 402 Payment Required response."""
        accepts = []
        for network_id, cc in self._accepted_chains.items():
            accepts.append(
                {
                    "scheme": CIRCLE_BATCHING_SCHEME,
                    "network": network_id,
                    "asset": cc["usdc"],
                    "amount": amount,
                    "payTo": self.seller_address,
                    "maxTimeoutSeconds": DEFAULT_MAX_TIMEOUT_SECONDS,
                    "extra": {
                        "name": CIRCLE_BATCHING_NAME,
                        "version": CIRCLE_BATCHING_VERSION,
                        "verifyingContract": cc["gateway_wallet"],
                    },
                }
            )

        body = {
            "x402Version": X402_VERSION,
            "resource": {
                "url": path,
                "description": self.description,
                "mimeType": "application/json",
            },
            "accepts": accepts,
        }

        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        return {"status": 402, "headers": {PAYMENT_REQUIRED_HEADER: encoded}, "body": body}

    async def _settle(self, payload: dict, requirements: dict) -> dict:
        """Call Circle Gateway settle() endpoint directly (skip verify).

        Uses correct field names: paymentPayload and paymentRequirements.
        """
        settle_url = f"{self._facilitator_url}/v1/x402/settle"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                settle_url,
                json={
                    "paymentPayload": payload,
                    "paymentRequirements": requirements,
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()

    async def process_request(
        self,
        request: web.Request,
        price: str,
    ) -> Optional[PaymentResult]:
        """Process an incoming aiohttp request for x402 payment.

        Returns:
            PaymentResult if payment succeeded.
            None if caller should return a 402 response (check request["x402_402"]).

        Expects nested wire format:
            {"payload": {"authorization": {...}, "signature": "..."}, "resource": ..., "accepted": ...}
        """
        path = request.path
        payment_header = request.headers.get(PAYMENT_SIGNATURE_HEADER)

        if not payment_header:
            # No payment → store 402 response for caller
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        # Decode payment header
        try:
            raw = base64.b64decode(payment_header).decode()
            decoded = json.loads(raw)
        except Exception as e:
            logger.warning("Invalid payment header: %s", e)
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        # Extract nested payload (matches circlekit/x402-header-agent format)
        # Format: {"payload": {"authorization": {...}, "signature": "..."}, "resource": ..., "accepted": ...}
        inner_payload = decoded.get("payload", {})
        authorization = inner_payload.get("authorization", {})
        # Fallback: check flat format (backward compat)
        if not authorization:
            authorization = decoded.get("authorization", {})
        if not authorization:
            logger.warning("Missing authorization in payment payload")
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        payer = authorization.get("from", "")
        client_value = str(authorization.get("value", "0"))
        network = decoded.get("accepted", {}).get(
            "network", decoded.get("network", self._chain_config["network"])
        )

        # Validate network is CAIP-2 and accepted by this seller
        if network not in self._accepted_chains:
            logger.warning("Network %s not accepted by this seller", network)
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        # Compute SERVER amount from price — never trust client value for settlement
        server_amount = self._price_to_amount(price)

        # Validate client value matches server-computed amount
        try:
            client_atomic = int(client_value)
            server_atomic = int(server_amount)
        except (ValueError, TypeError):
            logger.warning("Malformed authorization value: %s", client_value)
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        if client_atomic != server_atomic:
            logger.warning(
                "Underpayment rejected: client=%s server=%s", client_value, server_amount
            )
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        # Build requirements from SERVER-computed amount
        requirements = self._build_requirements(server_amount, network)

        # Settle directly (skip verify) — pass the full decoded payload
        # Circle Gateway expects: {"paymentPayload": {...}, "paymentRequirements": {...}}
        try:
            settle_result = await self._settle(decoded, requirements)
        except Exception as e:
            logger.error("Settle failed: %s", e)
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        if not settle_result.get("success"):
            reason = settle_result.get("errorReason", "unknown")
            logger.warning("Settlement rejected: %s", reason)
            amount = self._price_to_amount(price)
            request["x402_402"] = self._build_402_response(amount, path)
            return None

        # Payment succeeded — use server-computed amount in result
        transaction = settle_result.get("transaction", "")
        result = PaymentResult(
            payer=payer,
            amount=server_amount,
            network=network,
            transaction=transaction,
        )

        # Store on request and set context for tools
        request["x402_payment"] = result
        set_payment_context(
            payer=payer,
            amount=server_amount,
            network=network,
            transaction=transaction,
        )

        logger.info("Payment settled: %s USDC by %s tx=%s", server_amount, payer, transaction)
        return result

    @staticmethod
    def _price_to_amount(price: str) -> str:
        """Convert price string like '$0.01' to USDC amount string '10000'."""
        if price.startswith("$"):
            price = price[1:]
        usdc = float(price)
        return str(int(usdc * 1_000_000))  # 6 decimals


def create_aiohttp_middleware(
    seller_address: str,
    chain: str = "arcTestnet",
    facilitator_url: Optional[str] = None,
    description: str = "Paid resource",
    networks: Optional[list[str]] = None,
) -> X402SellerMiddleware:
    """Create an x402 seller middleware for aiohttp.

    Usage:
        middleware = create_aiohttp_middleware(
            seller_address="0x...",
            chain="arcTestnet",
        )

        # In your aiohttp handler:
        result = await middleware.process_request(request, price="$0.01")
        if result is None:
            resp_402 = request["x402_402"]
            return web.json_response(
                resp_402["body"],
                status=resp_402["status"],
                headers=resp_402["headers"],
            )
        # Payment succeeded — result is PaymentResult
    """
    return X402SellerMiddleware(
        seller_address=seller_address,
        chain=chain,
        facilitator_url=facilitator_url,
        description=description,
        networks=networks,
    )
