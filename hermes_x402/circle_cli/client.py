"""Typed client for the verified Circle CLI Agent Wallet and x402 commands."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from hermes_x402.circle_cli.errors import (
    CircleCliAuthenticationRequiredError,
    CircleCliDeploymentAmbiguousError,
    CircleCliDeploymentTimeoutError,
    CircleCliError,
    CircleCliOutputError,
    CircleCliPaymentFailedError,
    CircleCliPaymentOutcomeUnknownError,
    CircleCliPaymentRejectedError,
    CircleCliReadError,
    CircleCliTermsRequiredError,
    CircleCliTimeoutError,
    CircleCliVersionError,
    CircleCliWalletNotFoundError,
)
from hermes_x402.circle_cli.models import (
    AgentWallet,
    AgentWalletStatus,
    CircleCliResult,
    CircleCliVersion,
    CircleServicePayment,
    GatewayBalanceResult,
    GatewayDepositResult,
    LoginStartResult,
    SessionStatus,
    WalletBalance,
    WalletDeployResult,
)
from hermes_x402.circle_cli.runner import CircleCliRunner

_MINIMUM_VERSION = (0, 0, 6)
_PAYMENT_SUBMITTED_MARKERS = (
    "payment was submitted",
    "payment may have been submitted",
    "payment submitted but",
    "funds may have moved",
)
_TX_HASH = re.compile(r"0x[a-fA-F0-9]{64}")


class CircleCliClient:
    """Only documented Circle CLI commands, with typed output and safe errors."""

    def __init__(self, runner: CircleCliRunner):
        self.runner = runner

    @staticmethod
    def _data(result: CircleCliResult) -> dict[str, Any]:
        if not isinstance(result.parsed, dict):
            raise CircleCliOutputError("Circle CLI did not return a JSON object")
        data = result.parsed.get("data")
        if not isinstance(data, dict):
            raise CircleCliOutputError("Circle CLI JSON response is missing the data object")
        return data

    @staticmethod
    def _diagnostic(result: CircleCliResult) -> str:
        # Keep bounded, output-only diagnostic text internal to classifiers.
        return f"{result.stdout}\n{result.stderr}".lower()

    @classmethod
    def _require_read_success(cls, result: CircleCliResult) -> None:
        if result.exit_code == 0:
            return
        diagnostic = cls._diagnostic(result)
        if "auth_required" in diagnostic or "not logged in" in diagnostic or "terms" in diagnostic:
            raise CircleCliAuthenticationRequiredError(
                "Circle Agent Wallet authentication is required; complete login and "
                "any Terms step manually"
            )
        raise CircleCliReadError(f"Circle CLI read operation failed (exit code {result.exit_code})")

    @classmethod
    def _require_payment_success(cls, result: CircleCliResult) -> None:
        if result.exit_code == 0:
            return
        diagnostic = cls._diagnostic(result)
        if any(marker in diagnostic for marker in _PAYMENT_SUBMITTED_MARKERS):
            raise CircleCliPaymentOutcomeUnknownError(
                "Circle CLI reports payment submission may have occurred; "
                "do not retry automatically"
            )
        if "auth_required" in diagnostic or "not logged in" in diagnostic or "terms" in diagnostic:
            raise CircleCliAuthenticationRequiredError(
                "Circle Agent Wallet authentication is required; complete login and "
                "any Terms step manually"
            )
        if (
            "invalid_argument" in diagnostic
            or "not found" in diagnostic
            or "exceeds --max-amount" in diagnostic
        ):
            raise CircleCliPaymentRejectedError(
                f"Circle CLI rejected payment before submission (exit code {result.exit_code})"
            )
        raise CircleCliPaymentFailedError(
            f"Circle CLI payment failed (exit code {result.exit_code})"
        )

    async def version(self) -> CircleCliVersion:
        result = await self.runner.run_text(
            ("--version",), timeout_seconds=self.runner.read_timeout_seconds, operation="read"
        )
        if result.exit_code != 0:
            raise CircleCliVersionError("Could not read Circle CLI version")
        value = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
        if not match:
            raise CircleCliVersionError("Circle CLI version output is malformed")
        parsed = tuple(int(part) for part in match.groups())
        if parsed < _MINIMUM_VERSION:
            minimum = ".".join(str(part) for part in _MINIMUM_VERSION)
            raise CircleCliVersionError(f"Circle CLI {minimum} or newer is required")
        return CircleCliVersion(value=".".join(match.groups()))

    async def agent_wallet_status(self) -> AgentWalletStatus:
        result = await self.runner.run_json(
            ("wallet", "status", "--type", "agent", "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="auth",
        )
        self._require_read_success(result)
        data = self._data(result)
        mainnet = data.get("mainnet")
        testnet = data.get("testnet")
        if not isinstance(mainnet, dict) or not isinstance(testnet, dict):
            raise CircleCliOutputError("Circle CLI wallet status JSON is malformed")
        email = mainnet.get("email") or testnet.get("email")
        return AgentWalletStatus(
            mainnet_status=str(mainnet.get("tokenStatus", "NOT_LOGGED_IN")),
            testnet_status=str(testnet.get("tokenStatus", "NOT_LOGGED_IN")),
            email=str(email) if email else None,
        )

    async def list_wallets(self, *, network: str) -> tuple[AgentWallet, ...]:
        result = await self.runner.run_json(
            ("wallet", "list", "--chain", network, "--type", "agent", "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="read",
        )
        self._require_read_success(result)
        wallets = self._data(result).get("wallets")
        if not isinstance(wallets, list):
            raise CircleCliOutputError("Circle CLI wallet list JSON is malformed")
        parsed: list[AgentWallet] = []
        for wallet in wallets:
            if not isinstance(wallet, dict) or not isinstance(wallet.get("address"), str):
                raise CircleCliOutputError("Circle CLI wallet entry is malformed")
            parsed.append(
                AgentWallet(
                    address=wallet["address"],
                    blockchain=str(wallet.get("blockchain", network)),
                    created_at=str(wallet["createDate"]) if wallet.get("createDate") else None,
                )
            )
        return tuple(parsed)

    async def get_balance(self, *, wallet_address: str, network: str) -> tuple[WalletBalance, ...]:
        result = await self.runner.run_json(
            (
                "wallet",
                "balance",
                "--address",
                wallet_address,
                "--chain",
                network,
                "--output",
                "json",
            ),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="read",
        )
        self._require_read_success(result)
        balances = self._data(result).get("balances")
        if not isinstance(balances, list):
            raise CircleCliOutputError("Circle CLI balance JSON is malformed")
        normalized: list[WalletBalance] = []
        for balance in balances:
            token = balance.get("token") if isinstance(balance, dict) else None
            if not isinstance(balance, dict) or not isinstance(token, dict):
                raise CircleCliOutputError("Circle CLI balance entry is malformed")
            symbol, amount = token.get("symbol"), balance.get("amount")
            if not isinstance(symbol, str) or not isinstance(amount, str):
                raise CircleCliOutputError("Circle CLI balance entry is malformed")
            normalized.append(
                WalletBalance(
                    symbol=symbol,
                    amount=amount,
                    token_address=token.get("tokenAddress")
                    if isinstance(token.get("tokenAddress"), str)
                    else None,
                )
            )
        return tuple(normalized)

    async def supported_networks(self) -> tuple[str, ...]:
        result = await self.runner.run_json(
            ("blockchain", "list", "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="read",
        )
        self._require_read_success(result)
        data = self._data(result)
        blockchains = data.get("blockchains")
        if not isinstance(blockchains, list):
            raise CircleCliOutputError("Circle CLI blockchain list JSON is malformed")
        names = [item.get("blockchain") for item in blockchains if isinstance(item, dict)]
        return tuple(str(name) for name in names if isinstance(name, str))

    async def network_x402_identifier(self, network: str) -> str:
        """Return the exact x402 EIP-155 identifier for a CLI chain code."""
        result = await self.runner.run_json(
            ("blockchain", "list", "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="read",
        )
        self._require_read_success(result)
        blockchains = self._data(result).get("blockchains")
        if not isinstance(blockchains, list):
            raise CircleCliOutputError("Circle CLI blockchain list JSON is malformed")
        for item in blockchains:
            if (
                not isinstance(item, dict)
                or str(item.get("blockchain", "")).lower() != network.lower()
            ):
                continue
            chain_id = item.get("evmChainId")
            if isinstance(chain_id, int) and chain_id > 0:
                return f"eip155:{chain_id}"
            raise CircleCliOutputError(
                "Circle CLI blockchain entry is missing a valid EVM chain ID"
            )
        raise CircleCliReadError(
            "Configured Circle CLI network was not returned by blockchain list"
        )

    async def verify_selected_wallet(self, *, wallet_address: str, network: str) -> None:
        status = await self.agent_wallet_status()
        if not status.authenticated:
            raise CircleCliAuthenticationRequiredError(
                "Circle Agent Wallet session is not authenticated; login manually before payment"
            )
        wallets = await self.list_wallets(network=network)
        if not wallets:
            raise CircleCliWalletNotFoundError(
                "No Agent Wallet exists on the configured CLI network"
            )
        if not any(wallet.address.lower() == wallet_address.lower() for wallet in wallets):
            raise CircleCliWalletNotFoundError("Configured Circle CLI wallet address was not found")

    async def pay_x402(
        self,
        *,
        url: str,
        method: str,
        body: dict[str, Any] | None,
        headers: Mapping[str, str],
        wallet_address: str,
        network: str,
        max_usdc: str | None,
    ) -> CircleServicePayment:
        args = [
            "services",
            "pay",
            url,
            "--address",
            wallet_address,
            "--chain",
            network,
            "-X",
            method.upper(),
            "--timeout",
            str(int(self.runner.payment_timeout_seconds)),
        ]
        if max_usdc is not None:
            args.extend(("--max-amount", max_usdc))
        if body is not None:
            import json

            args.extend(("--data", json.dumps(body, separators=(",", ":"))))
        for name, value in headers.items():
            args.extend(("-H", f"{name}: {value}"))
        args.extend(("--output", "json"))
        try:
            result = await self.runner.run_json(
                args, timeout_seconds=self.runner.payment_timeout_seconds, operation="payment"
            )
            self._require_payment_success(result)
            data = self._data(result)
            payment = data.get("payment")
            if not isinstance(payment, dict) or "response" not in data:
                raise CircleCliOutputError("Circle CLI payment success JSON is malformed")
            required = ("amount", "chain", "scheme", "seller")
            if not all(isinstance(payment.get(field), str) for field in required):
                raise CircleCliOutputError(
                    "Circle CLI payment JSON is missing required payment fields"
                )
            receipt = payment.get("receipt")
            if receipt is not None and not isinstance(receipt, str):
                raise CircleCliOutputError("Circle CLI payment receipt is malformed")
            transaction = _TX_HASH.search(receipt or "")
            return CircleServicePayment(
                response=data["response"],
                amount=payment["amount"],
                chain=payment["chain"],
                scheme=payment["scheme"],
                seller=payment["seller"],
                receipt=receipt,
                transaction_id=transaction.group(0) if transaction else None,
            )
        except CircleCliPaymentRejectedError:
            raise
        except CircleCliAuthenticationRequiredError:
            raise
        except (CircleCliTimeoutError, CircleCliError) as exc:
            raise CircleCliPaymentOutcomeUnknownError(
                "Circle CLI payment did not produce a definite pre-submission rejection; "
                "do not retry automatically"
            ) from exc

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def session_status(self) -> SessionStatus:
        """Check Circle CLI session status."""
        result = await self.runner.run_json(
            ("session", "status", "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="auth",
        )
        # Session status may return non-zero for expired/not-logged-in
        diagnostic = self._diagnostic(result)
        if result.exit_code != 0:
            if "terms" in diagnostic:
                return SessionStatus(
                    authenticated=False,
                    environment="unknown",
                    terms_accepted=False,
                    status_code="TERMS_REQUIRED",
                )
            if "not logged in" in diagnostic or "expired" in diagnostic:
                return SessionStatus(
                    authenticated=False,
                    environment="unknown",
                    status_code="NOT_LOGGED_IN",
                )
            raise CircleCliReadError(
                f"Circle CLI session status failed (exit code {result.exit_code})"
            )

        data = self._data(result)
        authenticated = bool(data.get("authenticated", False))
        environment = str(data.get("environment", "unknown"))
        email = data.get("email")
        terms_accepted = bool(data.get("termsAccepted", True))
        status_code = str(data.get("status", "VALID"))

        return SessionStatus(
            authenticated=authenticated,
            environment=environment,
            email=str(email) if email else None,
            terms_accepted=terms_accepted,
            status_code=status_code,
        )

    async def login_start(self, *, email: str) -> LoginStartResult:
        """Start email OTP login flow."""
        result = await self.runner.run_json(
            ("login", "--email", email, "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="auth",
        )
        diagnostic = self._diagnostic(result)
        if result.exit_code != 0:
            if "terms" in diagnostic:
                raise CircleCliTermsRequiredError(
                    "Circle Terms of Use must be accepted before login"
                )
            if "invalid" in diagnostic or "not found" in diagnostic:
                raise CircleCliReadError("Circle CLI rejected the email for login")
            raise CircleCliReadError(
                f"Circle CLI login start failed (exit code {result.exit_code})"
            )

        data = self._data(result)
        request_id = str(data.get("requestId", ""))
        masked_email = str(data.get("email", email))
        if not request_id:
            raise CircleCliOutputError("Circle CLI login response is missing requestId")

        return LoginStartResult(
            request_id=request_id,
            email_masked=masked_email,
            otp_required=True,
        )

    async def login_complete(self, *, request_id: str, otp: str) -> SessionStatus:
        """Submit OTP to complete login."""
        result = await self.runner.run_json(
            ("login", "otp", "--request-id", request_id, "--otp", otp, "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="auth",
        )
        diagnostic = self._diagnostic(result)
        if result.exit_code != 0:
            if "terms" in diagnostic:
                raise CircleCliTermsRequiredError(
                    "Circle Terms of Use must be accepted after OTP verification"
                )
            if "invalid" in diagnostic or "expired" in diagnostic or "rejected" in diagnostic:
                raise CircleCliReadError(
                    "Circle CLI OTP verification failed (invalid, expired, or rejected)"
                )
            raise CircleCliReadError(
                f"Circle CLI OTP submission failed (exit code {result.exit_code})"
            )

        # Verify session after completion
        return await self.session_status()

    async def logout(self) -> None:
        """Clear Circle CLI session. Idempotent."""
        import contextlib

        with contextlib.suppress(CircleCliError, CircleCliTimeoutError):
            await self.runner.run_json(
                ("logout", "--output", "json"),
                timeout_seconds=self.runner.read_timeout_seconds,
                operation="auth",
            )

    # ------------------------------------------------------------------
    # Wallet management
    # ------------------------------------------------------------------

    async def wallet_create(self, *, network: str) -> AgentWallet:
        """Create a new Agent Wallet."""
        result = await self.runner.run_json(
            ("wallet", "create", "--chain", network, "--type", "agent", "--output", "json"),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="auth",
        )
        self._require_read_success(result)
        data = self._data(result)
        wallet_data = data.get("wallet")
        if not isinstance(wallet_data, dict) or not isinstance(wallet_data.get("address"), str):
            raise CircleCliOutputError("Circle CLI wallet create response is malformed")
        return AgentWallet(
            address=wallet_data["address"],
            blockchain=str(wallet_data.get("blockchain", network)),
            created_at=str(wallet_data["createDate"]) if wallet_data.get("createDate") else None,
        )

    async def wallet_deploy(self, *, wallet_address: str, network: str) -> WalletDeployResult:
        """Deploy Agent Wallet SCA on-chain."""
        try:
            result = await self.runner.run_json(
                (
                    "wallet",
                    "deploy",
                    "--address",
                    wallet_address,
                    "--chain",
                    network,
                    "--output",
                    "json",
                ),
                timeout_seconds=self.runner.payment_timeout_seconds,
                operation="payment",
            )
        except CircleCliTimeoutError as exc:
            raise CircleCliDeploymentTimeoutError(
                "SCA deployment timed out; check status before retrying"
            ) from exc
        except CircleCliError as exc:
            raise CircleCliDeploymentAmbiguousError(
                "SCA deployment outcome is ambiguous; do not retry automatically"
            ) from exc

        if result.exit_code != 0:
            diagnostic = self._diagnostic(result)
            if "already" in diagnostic and "deployed" in diagnostic:
                return WalletDeployResult(
                    wallet_address=wallet_address,
                    status="already_deployed",
                )
            raise CircleCliReadError(
                f"Circle CLI wallet deploy failed (exit code {result.exit_code})"
            )

        data = self._data(result)
        operation_id = data.get("operationId")
        tx_hash = data.get("transactionHash")
        status = str(data.get("status", "submitted"))

        return WalletDeployResult(
            wallet_address=wallet_address,
            operation_id=str(operation_id) if operation_id else None,
            transaction_hash=str(tx_hash) if tx_hash else None,
            status=status,
        )

    # ------------------------------------------------------------------
    # Gateway operations
    # ------------------------------------------------------------------

    async def gateway_balance(self, *, wallet_address: str, network: str) -> GatewayBalanceResult:
        """Get Circle Gateway balance for the configured wallet."""
        result = await self.runner.run_json(
            (
                "gateway",
                "balance",
                "--address",
                wallet_address,
                "--chain",
                network,
                "--output",
                "json",
            ),
            timeout_seconds=self.runner.read_timeout_seconds,
            operation="read",
        )
        self._require_read_success(result)
        data = self._data(result)
        balance = data.get("balance", data.get("totalBalance", "0"))
        domain = data.get("domain")
        return GatewayBalanceResult(
            total_usdc=str(balance),
            network=network,
            domain=int(domain) if isinstance(domain, int) else None,
        )

    async def gateway_deposit(
        self,
        *,
        wallet_address: str,
        network: str,
        amount: str,
    ) -> GatewayDepositResult:
        """Execute a Gateway deposit."""
        try:
            result = await self.runner.run_json(
                (
                    "gateway",
                    "deposit",
                    "--address",
                    wallet_address,
                    "--chain",
                    network,
                    "--amount",
                    amount,
                    "--output",
                    "json",
                ),
                timeout_seconds=self.runner.payment_timeout_seconds,
                operation="payment",
            )
        except CircleCliTimeoutError as exc:
            raise CircleCliPaymentOutcomeUnknownError(
                "Gateway deposit timed out; do not retry automatically"
            ) from exc

        if result.exit_code != 0:
            diagnostic = self._diagnostic(result)
            if any(m in diagnostic for m in _PAYMENT_SUBMITTED_MARKERS):
                raise CircleCliPaymentOutcomeUnknownError(
                    "Gateway deposit may have been submitted; do not retry automatically"
                )
            raise CircleCliReadError(
                f"Circle CLI gateway deposit failed (exit code {result.exit_code})"
            )

        data = self._data(result)
        operation_id = data.get("operationId")
        tx_hash = data.get("transactionHash")
        status = str(data.get("status", "submitted"))

        return GatewayDepositResult(
            operation_id=str(operation_id) if operation_id else None,
            transaction_hash=str(tx_hash) if tx_hash else None,
            status=status,
            network=network,
        )
