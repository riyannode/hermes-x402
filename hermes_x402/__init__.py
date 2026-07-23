"""Hermes x402 — Circle Gateway seller middleware and pluggable buyer backends."""

from hermes_x402.agent import X402HermesAgent
from hermes_x402.backends import CircleCliBuyerBackend, CircleDcwBuyerBackend
from hermes_x402.buyer import (
    BuyerBackend,
    BuyerResult,
    PaymentPolicy,
    X402BuyerService,
    X402BuyerTool,
    create_buyer_tool,
)
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
from hermes_x402.circle_cli import CircleCliClient, CircleCliRunner
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
from hermes_x402.config import ARC_MAINNET, ARC_TESTNET, X402Config
from hermes_x402.context import X402ContextBridge, get_payment_context, set_payment_context
from hermes_x402.middleware import (
    X402SellerMiddleware,
    create_aiohttp_middleware,
    get_x402_challenge,
    get_x402_payment,
)
from hermes_x402.network_policy import NetworkPolicy, parse_network_policy
from hermes_x402.networks import NetworkConfig, get_network, list_networks, normalize_network
from hermes_x402.seller_gateway import X402Gateway, create_aiohttp_gateway

__version__ = "0.2.0"
__all__ = [
    "ARC_MAINNET",
    "ARC_TESTNET",
    "BuyerBackend",
    "BuyerConfigurationError",
    "BuyerResult",
    "CircleCliAuthenticationRequiredError",
    "CircleCliBuyerBackend",
    "CircleCliClient",
    "CircleCliError",
    "CircleCliNotInstalledError",
    "CircleCliOutputError",
    "CircleCliPaymentFailedError",
    "CircleCliPaymentOutcomeUnknownError",
    "CircleCliPaymentRejectedError",
    "CircleCliReadError",
    "CircleCliRunner",
    "CircleCliTimeoutError",
    "CircleCliUnsupportedCapabilityError",
    "CircleCliUnsupportedNetworkError",
    "CircleCliVersionError",
    "CircleCliWalletMismatchError",
    "CircleCliWalletNotFoundError",
    "CircleDcwBuyerBackend",
    "DcwApiError",
    "DcwSigningError",
    "InvalidPaymentChallengeError",
    "NetworkConfig",
    "NetworkPolicy",
    "PaidResourceRequestError",
    "PaymentNotSubmittedError",
    "PaymentPolicy",
    "PaymentPolicyError",
    "PaymentProofError",
    "PaymentSubmissionUnknownError",
    "UnsupportedBuyerBackendError",
    "X402BuyerService",
    "X402BuyerTool",
    "X402Config",
    "X402ContextBridge",
    "X402Gateway",
    "X402HermesAgent",
    "X402SellerMiddleware",
    "create_aiohttp_gateway",
    "create_aiohttp_middleware",
    "create_buyer_tool",
    "get_network",
    "get_payment_context",
    "get_x402_challenge",
    "get_x402_payment",
    "list_networks",
    "normalize_network",
    "parse_network_policy",
    "set_payment_context",
]
