"""Lazy, process-local runtime cache for the x402 plugin.

Initialized on first tool call. Does not hot-reload credentials
during the same Hermes process.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hermes_x402.buyer import X402BuyerTool
from hermes_x402.buyer.backend import BuyerBackend
from hermes_x402.buyer.policy import PaymentPolicy
from hermes_x402.circle_cli.client import CircleCliClient
from hermes_x402.circle_cli.runner import CircleCliRunner
from hermes_x402.config import X402Config

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"


class X402Runtime:
    """Process-local runtime that lazily initializes the buyer stack.

    Side-effect-free at construction time. All initialization happens
    on first tool call via `ensure_initialized()`.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._config: X402Config | None = None
        self._buyer_tool: X402BuyerTool | None = None
        self._cli_client: CircleCliClient | None = None
        self._init_error: str | None = None

    def ensure_initialized(self) -> None:
        """Initialize configuration and buyer backend on first call.

        Does not invoke Circle CLI, make HTTP requests, validate wallet
        authentication, perform payment, read OTP, accept Terms, create
        a wallet, start seller middleware, or log secrets.
        """
        if self._initialized:
            return
        self._initialized = True

        try:
            self._config = X402Config.from_env()
        except Exception as exc:
            self._init_error = str(exc)
            logger.debug("x402 runtime config load failed: %s", exc)
            return

        try:
            self._build_buyer()
        except Exception as exc:
            self._init_error = str(exc)
            logger.debug("x402 runtime buyer init failed: %s", exc)

    def _build_buyer(self) -> None:
        """Build the buyer tool from the validated config."""
        if self._config is None:
            return

        if self._config.role not in {"buyer", "dual"}:
            return

        backend = self._create_backend()
        if backend is None:
            return

        policy = PaymentPolicy(
            max_usdc=self._config.max_usdc_per_payment,
            host_allowlist=tuple(self._config.host_allowlist),
        )
        self._buyer_tool = X402BuyerTool(backend=backend, policy=policy)

    def _create_backend(self) -> BuyerBackend | None:
        """Create the appropriate buyer backend from config."""
        if self._config is None:
            return None

        if self._config.buyer_backend == "cli":
            runner = CircleCliRunner(
                executable=self._config.circle_cli_executable,
                cwd=Path(self._config.circle_cli_cwd) if self._config.circle_cli_cwd else None,
            )
            self._cli_client = CircleCliClient(runner=runner)
            from hermes_x402.backends.circle_cli import CircleCliBuyerBackend

            return CircleCliBuyerBackend(
                wallet_address_value=self._config.circle_cli_wallet_address,
                network=self._config.circle_cli_network,
                client=self._cli_client,
            )

        if self._config.buyer_backend == "dcw":
            from hermes_x402.backends.circle_dcw import CircleDcwBuyerBackend

            return CircleDcwBuyerBackend(
                wallet_id=self._config.wallet_id,
                wallet_address=self._config.wallet_address,
                entity_secret=self._config.entity_secret,
                api_key=self._config.api_key,
                blockchain=self._config.blockchain,
                chain=self._config.chain,
            )

        return None

    @property
    def config(self) -> X402Config | None:
        return self._config

    @property
    def buyer_tool(self) -> X402BuyerTool | None:
        return self._buyer_tool

    @property
    def cli_client(self) -> CircleCliClient | None:
        return self._cli_client

    @property
    def init_error(self) -> str | None:
        return self._init_error

    @property
    def is_configured(self) -> bool:
        return self._config is not None and self._init_error is None

    @property
    def is_available(self) -> bool:
        return self._buyer_tool is not None

    @property
    def role(self) -> str | None:
        return self._config.role if self._config else None

    @property
    def backend_name(self) -> str | None:
        return self._config.buyer_backend if self._config else None

    @property
    def network(self) -> str:
        if self._config and self._config.buyer_backend == "cli":
            return self._config.circle_cli_network
        if self._config and self._config.buyer_backend == "dcw":
            return self._config.blockchain
        return ""

    @property
    def wallet_address(self) -> str:
        if self._config and self._config.buyer_backend == "cli":
            return self._config.circle_cli_wallet_address
        if self._config and self._config.buyer_backend == "dcw":
            return self._config.wallet_address
        return ""

    @property
    def version(self) -> str:
        return _VERSION


# Module-level singleton — process-local, not thread-safe by design.
_runtime: X402Runtime | None = None


def get_runtime() -> X402Runtime:
    """Return the process-local runtime, creating it if needed."""
    global _runtime
    if _runtime is None:
        _runtime = X402Runtime()
    return _runtime


def reset_runtime() -> None:
    """Reset the runtime (for testing)."""
    global _runtime
    _runtime = None
