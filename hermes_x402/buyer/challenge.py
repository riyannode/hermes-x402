"""Strict parsing of x402 Payment-Required response headers."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from hermes_x402.buyer.errors import InvalidPaymentChallengeError

PAYMENT_REQUIRED_HEADER = "Payment-Required"
PAYMENT_SIGNATURE_HEADER = "Payment-Signature"


def parse_payment_required(encoded: str) -> dict[str, Any]:
    if not encoded:
        raise InvalidPaymentChallengeError("402 response missing Payment-Required header")
    try:
        decoded = base64.b64decode(encoded, validate=True)
        challenge = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPaymentChallengeError("Payment-Required header is malformed") from exc
    if not isinstance(challenge, dict):
        raise InvalidPaymentChallengeError("Payment-Required payload must be an object")
    accepts = challenge.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise InvalidPaymentChallengeError("No accepted payment methods in 402 response")
    first = accepts[0]
    if not isinstance(first, dict) or not isinstance(first.get("amount"), str):
        raise InvalidPaymentChallengeError("Payment-Required accepted method is malformed")
    return challenge
