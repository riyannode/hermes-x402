"""Backend-neutral x402 buyer request, challenge, policy, and retry flow."""

from __future__ import annotations

from typing import Any, cast

import httpx

from hermes_x402.buyer.backend import BuyerBackend, ManagedPaymentBackend, PaymentProofBackend
from hermes_x402.buyer.challenge import parse_payment_required
from hermes_x402.buyer.errors import (
    BuyerError,
    PaidResourceRequestError,
    PaymentProofError,
    PaymentSubmissionUnknownError,
)
from hermes_x402.buyer.models import BuyerResult
from hermes_x402.buyer.policy import PaymentPolicy

_PROTECTED_PAYMENT_HEADERS = {"payment-signature", "x-payment", "x-payment-response"}


class X402BuyerService:
    """Common URL/policy/challenge flow for proof and managed-payment backends."""

    def __init__(self, *, backend: BuyerBackend, policy: PaymentPolicy):
        self.backend = backend
        self.policy = policy

    @staticmethod
    def _response_data(response: httpx.Response) -> Any:
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text

    @staticmethod
    def _copy_non_payment_headers(headers: dict[str, str] | None) -> dict[str, str]:
        return {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in _PROTECTED_PAYMENT_HEADERS
        }

    async def pay(
        self,
        url: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_usdc: str | None = None,
    ) -> BuyerResult:
        self.policy.validate_url(url)
        normalized_method = method.upper()
        request_headers = self._copy_non_payment_headers(headers)

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.request(
                    method=normalized_method,
                    url=url,
                    json=body,
                    headers=request_headers,
                )
                if response.status_code != 402:
                    return BuyerResult(
                        status=response.status_code,
                        data=self._response_data(response),
                        payer=self.backend.wallet_address,
                        amount="0",
                        network="",
                        payment_status="not_submitted",
                    )

                payment_required = parse_payment_required(
                    response.headers.get("Payment-Required", "")
                )
                if hasattr(self.backend, "pay_and_fetch"):
                    # A managed CLI may choose internally; validate every advertised
                    # option before its one protected request.
                    for accepted in payment_required["accepts"]:
                        self.policy.validate_amount(accepted["amount"], max_usdc)
                    # The official CLI performs its own protected request. The first
                    # common request above is unpaid and exists solely for policy and
                    # challenge validation; there is no Python paid retry.
                    effective_cap = max_usdc or self.policy.max_usdc
                    managed_backend = cast(ManagedPaymentBackend, self.backend)
                    managed = await managed_backend.pay_and_fetch(
                        url=url,
                        method=normalized_method,
                        body=body,
                        headers=request_headers,
                        payment_required=payment_required,
                        max_usdc=(
                            self.policy.normalize_max_usdc(effective_cap)
                            if effective_cap is not None
                            else None
                        ),
                    )
                    return BuyerResult(
                        status=managed.status,
                        data=managed.data,
                        payer=managed.payer,
                        amount=managed.amount,
                        network=managed.network,
                        transaction_id=managed.transaction_id,
                        payment_status=managed.payment_status,
                    )

                if not hasattr(self.backend, "create_payment_proof"):
                    raise PaymentProofError("Buyer backend does not support a payment capability")
                proof_backend = cast(PaymentProofBackend, self.backend)
                self.policy.validate_amount(payment_required["accepts"][0]["amount"], max_usdc)
                try:
                    proof = await proof_backend.create_payment_proof(
                        url=url,
                        method=normalized_method,
                        body=body,
                        payment_required=payment_required,
                    )
                except BuyerError:
                    raise
                except Exception as exc:
                    raise PaymentProofError("Buyer backend failed to create payment proof") from exc

                retry_headers = dict(request_headers)
                retry_headers[proof.header_name] = proof.header_value
                try:
                    paid_response = await client.request(
                        method=normalized_method,
                        url=url,
                        json=body,
                        headers=retry_headers,
                    )
                except httpx.HTTPError as exc:
                    raise PaidResourceRequestError(
                        "Paid resource request failed after payment proof creation"
                    ) from exc
        except PaymentSubmissionUnknownError:
            raise
        except httpx.HTTPError as exc:
            raise PaidResourceRequestError("Resource request failed") from exc

        return BuyerResult(
            status=paid_response.status_code,
            data=self._response_data(paid_response),
            payer=proof.payer,
            amount=proof.amount,
            network=proof.network,
            transaction_id=proof.transaction_id,
            payment_status=(
                "resource_succeeded"
                if 200 <= paid_response.status_code < 400
                else "resource_failed_after_payment"
            ),
        )
