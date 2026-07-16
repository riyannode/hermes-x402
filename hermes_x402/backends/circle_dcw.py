"""Circle Developer-Controlled Wallet backend for x402 buyer proofs."""

from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from typing import Any

import httpx

from hermes_x402.buyer.challenge import PAYMENT_SIGNATURE_HEADER
from hermes_x402.buyer.errors import (
    DcwApiError,
    DcwSigningError,
    InvalidPaymentChallengeError,
    PaymentSubmissionUnknownError,
)
from hermes_x402.buyer.models import PaymentProof
from hermes_x402.config import ARC_TESTNET


class CircleDcwBuyerBackend:
    """Create Circle Gateway payment proofs using a Developer-Controlled Wallet."""

    def __init__(
        self,
        *,
        wallet_id: str,
        wallet_address: str,
        entity_secret: str,
        api_key: str | None = None,
        blockchain: str = "ARC-TESTNET",
        chain: str = "arcTestnet",
    ):
        if chain != "arcTestnet":
            raise DcwApiError(f"Unsupported chain: {chain}")
        self.wallet_id = wallet_id
        self._wallet_address = wallet_address
        self._entity_secret = entity_secret
        self._api_key = api_key
        self.blockchain = blockchain
        self.chain = chain
        self._chain_config = ARC_TESTNET
        self._entity_public_key: str | None = None

    def __repr__(self) -> str:
        return (
            f"CircleDcwBuyerBackend(wallet_id={self.wallet_id!r}, "
            f"wallet_address={self._wallet_address!r}, blockchain={self.blockchain!r}, "
            f"chain={self.chain!r})"
        )

    @property
    def name(self) -> str:
        return "circle_dcw"

    @property
    def wallet_address(self) -> str:
        return self._wallet_address

    @property
    def _api_base(self) -> str:
        return (
            "https://api-sandbox.circle.com"
            if self._chain_config["is_testnet"]
            else "https://api.circle.com"
        )

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _get_entity_public_key(self) -> str:
        if self._entity_public_key:
            return self._entity_public_key
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(
                    f"{self._api_base}/v1/w3s/config/entity/publicKey",
                    headers=self._auth_headers(),
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DcwApiError("Circle entity public key request failed") from exc
        public_key = data.get("data", {}).get("publicKey") or data.get("publicKey")
        if not public_key:
            raise DcwApiError("Circle entity public key response missing publicKey")
        self._entity_public_key = str(public_key)
        return self._entity_public_key

    def _fresh_entity_secret_ciphertext(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        secret = self._entity_secret.strip()
        if not secret:
            raise DcwSigningError("Circle entity secret is required for DCW signing")
        if len(secret) != 64 or any(char not in "0123456789abcdefABCDEF" for char in secret):
            raise DcwSigningError(
                "Circle entity secret must be a 64-character hex-encoded 32-byte secret"
            )
        public_key_value = self._get_entity_public_key().strip()
        try:
            if "BEGIN PUBLIC KEY" in public_key_value:
                public_key = serialization.load_pem_public_key(public_key_value.encode())
            else:
                try:
                    public_key = serialization.load_der_public_key(
                        base64.b64decode(public_key_value)
                    )
                except Exception:
                    pem = (
                        f"-----BEGIN PUBLIC KEY-----\n{public_key_value}\n-----END PUBLIC KEY-----"
                    )
                    public_key = serialization.load_pem_public_key(pem.encode())
            encrypted = public_key.encrypt(
                bytes.fromhex(secret),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
        except DcwApiError:
            raise
        except Exception as exc:
            raise DcwSigningError("Circle entity secret encryption failed") from exc
        return base64.b64encode(encrypted).decode()

    async def create_payment_proof(
        self,
        *,
        url: str,
        method: str,
        body: dict[str, Any] | None,
        payment_required: dict[str, Any],
    ) -> PaymentProof:
        del url, method, body  # HTTP policy and request execution belong to the common service.
        accepts = payment_required.get("accepts")
        if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
            raise InvalidPaymentChallengeError("No accepted payment methods in 402 response")
        accepted = accepts[0]
        try:
            amount = str(accepted["amount"])
            pay_to = accepted["payTo"]
            max_timeout = int(accepted.get("maxTimeoutSeconds", 604900))
            extra = accepted.get("extra", {})
            if not isinstance(extra, dict):
                raise ValueError("extra")
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidPaymentChallengeError(
                "Payment-Required accepted method is malformed"
            ) from exc

        now = int(time.time())
        authorization = {
            "from": self.wallet_address,
            "to": pay_to,
            "value": int(amount),
            "validAfter": 0,
            "validBefore": now + max_timeout,
            "salt": int.from_bytes(secrets.token_bytes(32), "big"),
        }
        typed_data = {
            "types": {
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
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": extra.get("name", "GatewayWalletBatched"),
                "version": extra.get("version", "1"),
                "chainId": self._chain_config["chain_id"],
                "verifyingContract": extra.get(
                    "verifyingContract", self._chain_config["gateway_wallet"]
                ),
            },
            "message": authorization,
        }
        payload = {
            "walletId": self.wallet_id,
            "walletAddress": self.wallet_address,
            "blockchain": self.blockchain,
            "data": json.dumps(typed_data, separators=(",", ":")),
            "entitySecretCiphertext": self._fresh_entity_secret_ciphertext(),
            "memo": "x402 Gateway authorization",
        }
        headers = {
            "Content-Type": "application/json",
            **self._auth_headers(),
            "X-Request-Id": str(uuid.uuid4()),
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self._api_base}/v1/w3s/developer/sign/typedData",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise PaymentSubmissionUnknownError("Circle DCW signing outcome is unknown") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DcwApiError("Circle DCW signing request failed") from exc
        signature = data.get("data", {}).get("signature")
        if not isinstance(signature, str) or not signature:
            raise DcwSigningError("Circle DCW signing response missing signature")
        if not signature.startswith("0x"):
            signature = f"0x{signature}"
        payment_payload = {
            "x402Version": payment_required.get("x402Version", 2),
            "payload": {"authorization": authorization, "signature": signature},
            "resource": payment_required.get("resource", {}),
            "accepted": accepted,
        }
        return PaymentProof(
            backend=self.name,
            header_name=PAYMENT_SIGNATURE_HEADER,
            header_value=base64.b64encode(json.dumps(payment_payload).encode()).decode(),
            payer=self.wallet_address,
            amount=amount,
            network=accepted.get("network", f"eip155:{self._chain_config['chain_id']}"),
        )
