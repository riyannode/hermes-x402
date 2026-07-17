"""Circle Agent Wallet buyer backend backed by documented Circle CLI commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from hermes_x402.buyer.errors import InvalidPaymentChallengeError, PaymentSubmissionUnknownError
from hermes_x402.buyer.models import ManagedPaymentResult
from hermes_x402.circle_cli.client import CircleCliClient
from hermes_x402.circle_cli.errors import (
    CircleCliPaymentOutcomeUnknownError,
    CircleCliUnsupportedNetworkError,
)


@dataclass
class CircleCliBuyerBackend:
    """Managed x402 buyer using ``circle services pay`` exactly once per request.

    The official CLI creates the payment authorization/header and performs the
    protected fetch itself. This backend therefore cannot create a reusable proof.
    """

    wallet_address_value: str
    network: str
    client: CircleCliClient
    _ready: bool = field(default=False, init=False, repr=False)
    _x402_network: str | None = field(default=None, init=False, repr=False)
    _active_fingerprints: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def name(self) -> str:
        return "circle_cli"

    @property
    def wallet_address(self) -> str:
        return self.wallet_address_value

    def __repr__(self) -> str:
        return (
            "CircleCliBuyerBackend("
            f"wallet_address={self.wallet_address_value!r}, network={self.network!r})"
        )

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        await self.client.version()
        supported = await self.client.supported_networks()
        if self.network.lower() not in {network.lower() for network in supported}:
            raise CircleCliUnsupportedNetworkError(
                f"Configured Circle CLI network {self.network!r} is not supported by this CLI"
            )
        self._x402_network = await self.client.network_x402_identifier(self.network)
        await self.client.verify_selected_wallet(
            wallet_address=self.wallet_address_value, network=self.network
        )
        self._ready = True

    def _select_safe_accept(self, payment_required: dict[str, Any]) -> dict[str, Any]:
        """Fail closed unless CLI's possible option set is exactly policy-validated."""
        accepts = payment_required["accepts"]
        if self._x402_network is None:  # defensive: _ensure_ready establishes this.
            raise InvalidPaymentChallengeError("Circle CLI network preflight is incomplete")
        matching = [item for item in accepts if item["network"] == self._x402_network]
        if not matching:
            raise InvalidPaymentChallengeError(
                "402 challenge has no accept matching the configured Circle CLI network"
            )

        # The CLI can select alternatives (including Gateway alternatives) internally.
        # Its command surface cannot pin an accept object, so any materially different
        # advertisement must fail closed rather than rely on CLI selection order.
        def material(item: dict[str, Any]) -> str:
            # Preserve every field by default: unknown accept fields may constrain
            # authorization semantics. Normalize only documented case-insensitive IDs.
            normalized = dict(item)
            for key in ("scheme", "network", "asset", "payTo"):
                if isinstance(normalized.get(key), str):
                    normalized[key] = normalized[key].lower()
            return json.dumps(normalized, sort_keys=True, separators=(",", ":"))

        materials = {material(item) for item in accepts}
        if len(materials) != 1:
            raise InvalidPaymentChallengeError(
                "Circle CLI cannot pin an exact accept; multiple materially different "
                "accepts are unsafe"
            )
        return matching[0]

    @staticmethod
    def _validate_challenge(payment_required: dict[str, Any]) -> None:
        version = payment_required.get("x402Version")
        accepts = payment_required.get("accepts")
        if version not in {1, 2} or not isinstance(accepts, list) or not accepts:
            raise InvalidPaymentChallengeError(
                "Circle CLI requires a supported x402 v1 or v2 challenge"
            )
        for accepted in accepts:
            if not isinstance(accepted, dict):
                raise InvalidPaymentChallengeError("Payment challenge accept entry is malformed")
            required = ("amount", "network", "payTo", "asset")
            if not all(
                isinstance(accepted.get(field), str) and accepted[field] for field in required
            ):
                raise InvalidPaymentChallengeError(
                    "Payment challenge must include amount, network, payTo, and asset"
                )
            if accepted.get("scheme") != "exact":
                raise InvalidPaymentChallengeError(
                    "Circle CLI backend supports only exact payment scheme"
                )

    def _fingerprint(
        self,
        *,
        url: str,
        payment_required: dict[str, Any],
        method: str,
        body: dict[str, Any] | None,
    ) -> str:
        accepts = payment_required["accepts"]
        first = accepts[0]
        material = {
            "wallet": self.wallet_address_value.lower(),
            "network": self.network.lower(),
            "url": url,
            "method": method,
            "body": body,
            "amount": first["amount"],
            "asset": first["asset"].lower(),
            "pay_to": first["payTo"].lower(),
            "resource": payment_required.get("resource"),
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, default=str).encode()
        ).hexdigest()

    @staticmethod
    def _reported_usdc_atomic(amount: str) -> int:
        try:
            decimal, unit = amount.strip().split()
            value = Decimal(decimal)
            exponent = value.as_tuple().exponent
            if (
                unit != "USDC"
                or not value.is_finite()
                or value < 0
                or not isinstance(exponent, int)
                or exponent < -6
            ):
                raise ValueError
            return int(value * Decimal(1_000_000))
        except (AttributeError, InvalidOperation, ValueError) as exc:
            raise CircleCliPaymentOutcomeUnknownError(
                "Circle CLI reported an unverifiable payment amount; do not retry automatically"
            ) from exc

    @staticmethod
    def _cli_amount_cap(selected_atomic: int, caller_cap: str | None) -> str:
        """Return the CLI's canonical decimal-USDC cap without float conversion."""
        cap_atomic = selected_atomic
        if caller_cap is not None:
            cap_atomic = min(cap_atomic, int(Decimal(caller_cap) * Decimal(1_000_000)))
        value = Decimal(cap_atomic) / Decimal(1_000_000)
        return format(value.normalize(), "f") if value else "0"

    async def pay_and_fetch(
        self,
        *,
        url: str,
        method: str,
        body: dict[str, Any] | None,
        headers: Mapping[str, str],
        payment_required: dict[str, Any],
        max_usdc: str | None,
    ) -> ManagedPaymentResult:
        self._validate_challenge(payment_required)
        await self._ensure_ready()
        selected = self._select_safe_accept(payment_required)
        fingerprint = self._fingerprint(
            url=url, payment_required=payment_required, method=method, body=body
        )
        if fingerprint in self._active_fingerprints:
            raise PaymentSubmissionUnknownError(
                "An equivalent Circle CLI payment is in progress or has an ambiguous outcome"
            )
        self._active_fingerprints.add(fingerprint)
        selected_atomic = int(selected["amount"])
        cli_max_usdc = self._cli_amount_cap(selected_atomic, max_usdc)
        try:
            paid = await self.client.pay_x402(
                url=url,
                method=method,
                body=body,
                headers=headers,
                wallet_address=self.wallet_address_value,
                network=self.network,
                max_usdc=cli_max_usdc,
            )
        except CircleCliPaymentOutcomeUnknownError as exc:
            # Retain this process-local fingerprint only for ambiguous submissions.
            raise PaymentSubmissionUnknownError(str(exc)) from exc
        except Exception:
            self._active_fingerprints.discard(fingerprint)
            raise
        reported_atomic = self._reported_usdc_atomic(paid.amount)
        cap_atomic = int(Decimal(max_usdc) * Decimal(1_000_000)) if max_usdc else None
        if (
            paid.seller.lower() != selected["payTo"].lower()
            or paid.chain.lower() != self.network.lower()
            or paid.scheme != selected["scheme"]
            or reported_atomic != selected_atomic
            or (cap_atomic is not None and reported_atomic > cap_atomic)
        ):
            raise PaymentSubmissionUnknownError(
                "Circle CLI payment result does not match the validated accept; "
                "do not retry automatically"
            )
        self._active_fingerprints.discard(fingerprint)
        return ManagedPaymentResult(
            status=None,
            data=paid.response,
            payer=self.wallet_address_value,
            amount=selected["amount"],
            network=selected["network"],
            transaction_id=paid.transaction_id,
            payment_status="resource_succeeded",
        )
