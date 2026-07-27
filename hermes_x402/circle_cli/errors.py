"""Safe, normalized errors for the official Circle CLI adapter."""

from __future__ import annotations

from hermes_x402.buyer.errors import BuyerError


class CircleCliError(BuyerError):
    """Base Circle CLI failure with no raw process output attached."""


class CircleCliNotSpawnedError(CircleCliError):
    """Circle CLI subprocess was not spawned."""


class CircleCliNotInstalledError(CircleCliNotSpawnedError):
    """The configured Circle executable cannot be started."""


class CircleCliExecutableNotFoundError(CircleCliNotInstalledError):
    """The configured Circle executable was not found."""


class CircleCliVersionError(CircleCliError):
    """The installed CLI does not meet the supported contract version."""


class CircleCliTimeoutError(CircleCliError):
    """A Circle CLI operation timed out."""


class CircleCliTimeoutBeforePaymentLogError(CircleCliTimeoutError):
    """Payment command timed out before Circle CLI persisted a payment log."""


class CircleCliTimeoutAfterPaymentLogError(CircleCliTimeoutError):
    """Payment command timed out after Circle CLI persisted a payment log."""


class CircleCliOutputError(CircleCliError):
    """Circle CLI emitted malformed, oversized, or unexpected output."""


class CircleCliInvalidJsonOutputError(CircleCliOutputError):
    """Circle CLI emitted output that could not be parsed as JSON."""


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


class CircleCliExitNonzeroError(CircleCliPaymentFailedError):
    """Circle CLI exited non-zero without evidence of payment submission."""


class CircleCliPaymentOutcomeUnknownError(CircleCliError):
    """Payment submission may have happened; it must never be retried automatically."""


class CircleCliTermsRequiredError(CircleCliError):
    """Circle Terms of Use must be accepted manually before proceeding."""
