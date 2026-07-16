"""Backend-neutral x402 buyer request, challenge, policy, and retry flow."""

from __future__ import annotations

from typing import Any

import httpx

from hermes_x402.buyer.backend import BuyerBackend
from hermes_x402.buyer.challenge import PAYMENT_SIGNATURE_HEADER, parse_payment_required
from hermes_x402.buyer.errors import BuyerError, PaidResourceRequestError, PaymentProofError
from hermes_x402.buyer.models import BuyerResult
from hermes_x402.buyer.policy import PaymentPolicy


class X402BuyerService:
    """Common x402 buyer flow. Wallet backends only create payment proofs."""

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
            if key.lower() != PAYMENT_SIGNATURE_HEADER.lower()
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
                accepted = payment_required["accepts"][0]
                self.policy.validate_amount(accepted["amount"], max_usdc)

                try:
                    proof = await self.backend.create_payment_proof(
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
