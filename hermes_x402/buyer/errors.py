"""Normalized, non-secret-bearing buyer errors."""


class BuyerError(Exception):
    """Base buyer error."""


class BuyerConfigurationError(BuyerError):
    """Buyer configuration is incomplete or ambiguous."""


class UnsupportedBuyerBackendError(BuyerConfigurationError):
    """Configured buyer backend is recognized but not implemented."""


class InvalidPaymentChallengeError(BuyerError):
    """A 402 Payment-Required challenge is absent or malformed."""


class PaymentPolicyError(BuyerError):
    """A requested payment violates local host or spending policy."""


class PaymentNotSubmittedError(BuyerError):
    """Payment was rejected before a proof could be safely created."""


class PaymentSubmissionUnknownError(BuyerError):
    """A signing/submission request may have completed, but its outcome is unknown."""


class PaymentProofError(BuyerError):
    """The selected backend could not create a usable payment proof."""


class PaidResourceRequestError(BuyerError):
    """The post-payment resource request failed before a normalized result existed."""


class DcwSigningError(PaymentProofError):
    """Circle DCW signing failed without exposing raw Circle response data."""


class DcwApiError(PaymentProofError):
    """Circle DCW API communication failed without exposing credentials."""
