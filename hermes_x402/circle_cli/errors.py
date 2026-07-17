"""Safe, normalized errors for the official Circle CLI adapter."""

from __future__ import annotations

from hermes_x402.buyer.errors import BuyerError


class CircleCliError(BuyerError):
    """Base Circle CLI failure with no raw process output attached."""


class CircleCliNotInstalledError(CircleCliError):
    """The configured Circle executable cannot be started."""


class CircleCliVersionError(CircleCliError):
    """The installed CLI does not meet the supported contract version."""


class CircleCliTimeoutError(CircleCliError):
    """A non-payment Circle CLI operation timed out."""


class CircleCliOutputError(CircleCliError):
    """Circle CLI emitted malformed, oversized, or unexpected output."""


class CircleCliAuthenticationRequiredError(CircleCliError):
    """The selected Agent Wallet session is absent, expired, or Terms-gated."""


class CircleCliWalletNotFoundError(CircleCliError):
    """The explicitly configured Agent Wallet was not returned by Circle CLI."""


class CircleCliWalletMismatchError(CircleCliError):
    """The configured wallet identity did not match the selected CLI wallet."""


class CircleCliUnsupportedNetworkError(CircleCliError):
    """The configured CLI network is not supported by the installed CLI."""


class CircleCliUnsupportedCapabilityError(CircleCliError):
    """A command or payment capability is outside the adapter's allowlist."""


class CircleCliReadError(CircleCliError):
    """A read-only Circle CLI command failed."""


class CircleCliPaymentRejectedError(CircleCliError):
    """Circle CLI rejected payment before submission."""


class CircleCliPaymentFailedError(CircleCliError):
    """Circle CLI reported a definite payment failure."""


class CircleCliPaymentOutcomeUnknownError(CircleCliError):
    """Payment submission may have happened; it must never be retried automatically."""
