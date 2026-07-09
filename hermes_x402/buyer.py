"""Buyer tool for x402 nanopayments via Circle DCW.

Makes HTTP requests to x402-protected endpoints, handles 402 challenges,
signs payment via Circle Developer-Controlled Wallets (no raw private key),
and returns the result.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from hermes_x402.config import ARC_TESTNET, X402Config
from hermes_x402.context import get_payment_context

logger = logging.getLogger("hermes_x402.buyer")

PAYMENT_SIGNATURE_HEADER = "Payment-Signature"


@dataclass
class BuyerResult:
    """Result of a successful x402 payment."""

    status: int
    data: Any
    payer: str
    amount: str
    network: str


class X402BuyerTool:
    """Hermes tool that pays downstream x402 APIs using Circle DCW.

    Flow:
        1. Make GET/POST request to target URL
        2. If 402 response → decode Payment-Required header
        3. Sign payment via DCW signTypedData
        4. Retry with Payment-Signature header
        5. Return result
    """

    def __init__(
        self,
        wallet_id: str,
        wallet_address: str,
        entity_secret: str,
        api_key: Optional[str] = None,
        blockchain: str = "ARC-TESTNET",
        chain: str = "arcTestnet",
        max_usdc: Optional[str] = None,
        host_allowlist: Optional[list[str]] = None,
    ):
        self.wallet_id = wallet_id
        self.wallet_address = wallet_address
        self.entity_secret = entity_secret
        self.api_key = api_key
        self.blockchain = blockchain
        self.chain = chain
        self.max_usdc = max_usdc
        self.host_allowlist = host_allowlist or []

        # Resolve chain config
        if chain == "arcTestnet":
            self._chain_config = ARC_TESTNET
        else:
            raise ValueError(f"Unsupported chain: {chain}")

        # Entity public key cache (fetched lazily)
        self._entity_public_key: Optional[str] = None

    def _check_host(self, url: str) -> bool:
        """Check if URL host is in allowlist."""
        if not self.host_allowlist:
            return True
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        return any(host == h or host.endswith(f".{h}") for h in self.host_allowlist)

    def _get_entity_public_key(self) -> str:
        """Fetch Circle's entity public key for RSA-OAEP encryption.

        Cached after first fetch. The key is used to encrypt the entity secret
        into a fresh ciphertext per request (replay protection).
        """
        if self._entity_public_key:
            return self._entity_public_key

        api_base = "https://api.circle.com"
        if self._chain_config.get("is_testnet"):
            api_base = "https://api-sandbox.circle.com"

        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{api_base}/v1/w3s/config/entity/publicKey", headers=headers)
            resp.raise_for_status()
            data = resp.json()

        public_key = data.get("data", {}).get("publicKey") or data.get("publicKey")
        if not public_key:
            raise ValueError("Circle entity public key response missing publicKey")

        self._entity_public_key = str(public_key)
        return self._entity_public_key

    def _fresh_entity_secret_ciphertext(self) -> str:
        """Encrypt entity secret with RSA-OAEP for Circle DCW API.

        Circle requires:
        - Fresh ciphertext per request (replay protection)
        - RSA-OAEP with SHA-256
        - Entity public key from Circle API
        """
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        secret = self.entity_secret.strip()
        if not secret:
            raise ValueError("CIRCLE_ENTITY_SECRET is required for DCW signing")
        if not all(c in "0123456789abcdefABCDEF" for c in secret) or len(secret) != 64:
            raise ValueError(
                "CIRCLE_ENTITY_SECRET must be a 64-character hex-encoded 32-byte secret"
            )

        # Fetch Circle's entity public key
        public_key_value = self._get_entity_public_key().strip()

        # Parse public key (PEM or DER)
        if "BEGIN PUBLIC KEY" in public_key_value:
            key_bytes = public_key_value.encode()
            public_key = serialization.load_pem_public_key(key_bytes)
        else:
            try:
                key_bytes = base64.b64decode(public_key_value)
                public_key = serialization.load_der_public_key(key_bytes)
            except Exception:
                pem = (
                    "-----BEGIN PUBLIC KEY-----\n"
                    + public_key_value
                    + "\n-----END PUBLIC KEY-----"
                )
                public_key = serialization.load_pem_public_key(pem.encode())

        # Encrypt with RSA-OAEP (SHA-256)
        encrypted = public_key.encrypt(
            bytes.fromhex(secret),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return base64.b64encode(encrypted).decode()

    async def _sign_payment(self, payment_required: dict) -> str:
        """Sign payment via Circle DCW signTypedData.

        Uses the same flow as x402-header-agent's CircleDcwNative:
        1. Fetch entity public key (cached)
        2. Encrypt entity secret with RSA-OAEP
        3. Call Circle DCW signTypedData API
        4. Build x402 payment payload with nested structure
        """
        # Extract requirements from payment-402
        accepts = payment_required.get("accepts", [])
        if not accepts:
            raise ValueError("No accepted payment methods in 402 response")

        req = accepts[0]  # Use first accepted method
        network = req.get("network", f"eip155:{self._chain_config['chain_id']}")
        amount = req.get("amount", "0")
        pay_to = req.get("payTo", "")
        max_timeout = req.get("maxTimeoutSeconds", 604900)
        extra = req.get("extra", {})

        # Build EIP-3009 authorization
        now = int(time.time())
        valid_before = now + max_timeout

        # Generate random salt
        salt = int.from_bytes(secrets.token_bytes(32), "big")

        authorization = {
            "from": self.wallet_address,
            "to": pay_to,
            "value": int(amount),
            "validAfter": 0,
            "validBefore": valid_before,
            "salt": salt,
        }

        # EIP-712 typed data for GatewayWalletBatched
        domain = {
            "name": extra.get("name", "GatewayWalletBatched"),
            "version": extra.get("version", "1"),
            "chainId": self._chain_config["chain_id"],
            "verifyingContract": extra.get(
                "verifyingContract", self._chain_config["gateway_wallet"]
            ),
        }

        types = {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "salt", "type": "bytes32"},
            ],
        }

        typed_data = {
            "types": {
                "EIP712Domain": types["EIP712Domain"],
                **types,
            },
            "primaryType": "TransferWithAuthorization",
            "domain": domain,
            "message": authorization,
        }

        # Call Circle DCW signTypedData API
        api_base = "https://api.circle.com"
        if self._chain_config.get("is_testnet"):
            api_base = "https://api-sandbox.circle.com"

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Encrypt entity secret for this request
        entity_secret_ciphertext = self._fresh_entity_secret_ciphertext()

        payload = {
            "walletId": self.wallet_id,
            "walletAddress": self.wallet_address,
            "blockchain": self.blockchain,
            "data": json.dumps(typed_data, separators=(",", ":")),
            "entitySecretCiphertext": entity_secret_ciphertext,
            "memo": "x402 Gateway authorization",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{api_base}/v1/w3s/developer/sign/typedData",
                json=payload,
                headers={**headers, "X-Request-Id": str(uuid.uuid4())},
            )
            resp.raise_for_status()
            data = resp.json()

        signature = data.get("data", {}).get("signature")
        if not signature:
            raise ValueError(f"DCW signTypedData failed: {data}")

        # Normalize signature to 0x-prefixed
        if not signature.startswith("0x"):
            signature = "0x" + signature

        # Build payment payload — NESTED format (matches circlekit/x402-header-agent)
        payment_payload = {
            "x402Version": payment_required.get("x402Version", 2),
            "payload": {
                "authorization": authorization,
                "signature": signature,
            },
            "resource": payment_required.get("resource", {}),
            "accepted": req,
        }

        return base64.b64encode(json.dumps(payment_payload).encode()).decode()

    async def pay(
        self,
        url: str,
        method: str = "GET",
        body: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        max_usdc: Optional[str] = None,
    ) -> BuyerResult:
        """Pay for a resource via x402.

        Args:
            url: Target URL to pay for
            method: HTTP method (GET, POST)
            body: Optional request body for POST
            headers: Optional additional headers
            max_usdc: Optional max USDC to pay (overrides default)

        Returns:
            BuyerResult with status, data, and payment info
        """
        # Check host allowlist
        if not self._check_host(url):
            raise ValueError(f"Host not in allowlist: {url}")

        # Check spending policy
        effective_max = max_usdc or self.max_usdc

        # Make initial request
        request_headers = headers or {}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(
                method=method,
                url=url,
                json=body,
                headers=request_headers,
            )

            # If not 402, return directly
            if resp.status_code != 402:
                return BuyerResult(
                    status=resp.status_code,
                    data=(
                        resp.json()
                        if resp.headers.get("content-type", "").startswith("application/json")
                        else resp.text
                    ),
                    payer=self.wallet_address,
                    amount="0",
                    network=self._chain_config["network"],
                )

            # Decode 402 Payment-Required header
            payment_required_b64 = resp.headers.get("Payment-Required", "")
            if not payment_required_b64:
                raise ValueError("402 response missing Payment-Required header")

            payment_required = json.loads(base64.b64decode(payment_required_b64))

            # Validate amount against policy
            accepts = payment_required.get("accepts", [])
            if accepts and effective_max:
                amount = int(accepts[0].get("amount", "0"))
                max_amount = int(float(effective_max) * 1_000_000)
                if amount > max_amount:
                    raise ValueError(f"Payment {amount} exceeds max {max_amount} USDC")

            # Sign payment via DCW
            payment_signature = await self._sign_payment(payment_required)

            # Retry with payment header
            retry_headers = {**request_headers, PAYMENT_SIGNATURE_HEADER: payment_signature}
            resp2 = await client.request(
                method=method,
                url=url,
                json=body,
                headers=retry_headers,
            )

            # Extract payment info from response
            payer = self.wallet_address
            amount = accepts[0].get("amount", "0") if accepts else "0"
            network = (
                accepts[0].get("network", self._chain_config["network"])
                if accepts
                else self._chain_config["network"]
            )

            return BuyerResult(
                status=resp2.status_code,
                data=(
                    resp2.json()
                    if resp2.headers.get("content-type", "").startswith("application/json")
                    else resp2.text
                ),
                payer=payer,
                amount=amount,
                network=network,
            )


def create_buyer_tool(
    wallet_id: str,
    wallet_address: str,
    entity_secret: str,
    api_key: Optional[str] = None,
    blockchain: str = "ARC-TESTNET",
    chain: str = "arcTestnet",
    max_usdc: Optional[str] = None,
    host_allowlist: Optional[list[str]] = None,
) -> X402BuyerTool:
    """Create an x402 buyer tool for Hermes.

    Usage:
        tool = create_buyer_tool(
            wallet_id="...",
            wallet_address="0x...",
            entity_secret="...",
        )
        result = await tool.pay("https://api.example.com/premium")
    """
    return X402BuyerTool(
        wallet_id=wallet_id,
        wallet_address=wallet_address,
        entity_secret=entity_secret,
        api_key=api_key,
        blockchain=blockchain,
        chain=chain,
        max_usdc=max_usdc,
        host_allowlist=host_allowlist,
    )
