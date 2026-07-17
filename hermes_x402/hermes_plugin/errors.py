"""Centralized error mapping from core exceptions to stable JSON error codes."""

from __future__ import annotations

import json
from typing import Any

from hermes_x402.buyer.errors import (
    BuyerConfigurationError,
    DcwApiError,
    DcwSigningError,
    InvalidPaymentChallengeError,
    PaidResourceRequestError,
    PaymentNotSubmittedError,
    PaymentPolicyError,
    PaymentProofError,
    PaymentSubmissionUnknownError,
    UnsupportedBuyerBackendError,
)
from hermes_x402.circle_cli.errors import (
    CircleCliAuthenticationRequiredError,
    CircleCliError,
    CircleCliNotInstalledError,
    CircleCliOutputError,
    CircleCliPaymentFailedError,
    CircleCliPaymentOutcomeUnknownError,
    CircleCliPaymentRejectedError,
    CircleCliReadError,
    CircleCliTimeoutError,
    CircleCliUnsupportedCapabilityError,
    CircleCliUnsupportedNetworkError,
    CircleCliVersionError,
    CircleCliWalletMismatchError,
    CircleCliWalletNotFoundError,
)

_ERROR_MAP: dict[type[BaseException], tuple[str, bool]] = {
    # Configuration
    BuyerConfigurationError: ("configuration_error", False),
    UnsupportedBuyerBackendError: ("unsupported_backend", False),
    # Challenge
    InvalidPaymentChallengeError: ("invalid_challenge", False),
    # Policy
    PaymentPolicyError: ("host_rejected", False),
    # Payment
    PaymentNotSubmittedError: ("payment_rejected", False),
    PaymentProofError: ("payment_failed", False),
    DcwSigningError: ("payment_failed", False),
    DcwApiError: ("payment_failed", False),
    PaymentSubmissionUnknownError: ("payment_outcome_unknown", False),
    PaidResourceRequestError: ("resource_failure_after_payment", False),
    # CLI errors
    CircleCliNotInstalledError: ("cli_missing", False),
    CircleCliVersionError: ("cli_version_unsupported", False),
    CircleCliAuthenticationRequiredError: ("authentication_required", False),
    CircleCliWalletNotFoundError: ("wallet_missing", False),
    CircleCliWalletMismatchError: ("wallet_mismatch", False),
    CircleCliUnsupportedNetworkError: ("network_unsupported", False),
    CircleCliPaymentRejectedError: ("payment_rejected", False),
    CircleCliPaymentFailedError: ("payment_failed", False),
    CircleCliPaymentOutcomeUnknownError: ("payment_outcome_unknown", False),
    CircleCliUnsupportedCapabilityError: ("unsupported_backend", False),
    CircleCliOutputError: ("internal_plugin_error", False),
    CircleCliReadError: ("internal_plugin_error", False),
    CircleCliTimeoutError: ("internal_plugin_error", False),
    CircleCliError: ("internal_plugin_error", False),
}


def map_exception(exc: BaseException) -> dict[str, Any]:
    """Map a known exception to a stable JSON error result.

    Never exposes: traceback, raw stdout, raw stderr, subprocess argv,
    request authorization headers, entity secret, API key, token, OTP.
    """
    error_code = "internal_plugin_error"
    retry_safe = False

    for exc_type, (code, retry) in _ERROR_MAP.items():
        if isinstance(exc, exc_type):
            error_code = code
            retry_safe = retry
            break

    # Ambiguous payment outcomes are never retry-safe
    if error_code == "payment_outcome_unknown":
        retry_safe = False

    message = _safe_message(exc)

    return {
        "success": False,
        "error": error_code,
        "message": message,
        "retry_safe": retry_safe,
    }


def _safe_message(exc: BaseException) -> str:
    """Extract a safe, user-facing message without leaking internals."""
    msg = str(exc).strip()
    if not msg:
        msg = "An unexpected error occurred."
    # Truncate extremely long messages
    if len(msg) > 500:
        msg = msg[:497] + "..."
    return msg


def format_error_result(exc: BaseException) -> str:
    """Return a JSON string for an error."""
    return json.dumps(map_exception(exc), ensure_ascii=False)


def format_success_result(data: dict[str, Any]) -> str:
    """Return a JSON string for a success result."""
    return json.dumps(data, ensure_ascii=False, default=str)
