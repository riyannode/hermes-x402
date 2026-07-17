"""Typed client for the verified Circle CLI Agent Wallet and x402 commands."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from hermes_x402.circle_cli.errors import (
    CircleCliAuthenticationRequiredError,
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
    WalletBalance,
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
        # Determine terms_accepted from whichever section is valid
        terms_accepted = True
        if isinstance(mainnet, dict) and mainnet.get("tokenStatus") != "NOT_VALID":
            terms_accepted = bool(mainnet.get("termsAccepted", True))
        elif isinstance(testnet, dict) and testnet.get("tokenStatus") != "NOT_VALID":
            terms_accepted = bool(testnet.get("termsAccepted", True))
        return AgentWalletStatus(
            mainnet_status=str(mainnet.get("tokenStatus", "NOT_LOGGED_IN")),
            testnet_status=str(testnet.get("tokenStatus", "NOT_LOGGED_IN")),
            email=str(email) if email else None,
            terms_accepted=terms_accepted,
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
    # Session management (v0.0.6 wallet-scoped commands)
    # ------------------------------------------------------------------

    async def login_start(
        self,
        *,
        email: str,
        testnet: bool = False,
    ) -> LoginStartResult:
        """Start email OTP login using Circle CLI v0.0.6 wallet-scoped command.

        Args:
            email: Email address for login.
            testnet: If True, add --testnet flag for testnet environments.
        """
        args = ["wallet", "login", email, "--type", "agent", "--init"]
        if testnet:
            args.append("--testnet")
        args.extend(["--output", "json"])

        result = await self.runner.run_json(
            tuple(args),
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

    async def login_complete(self, *, request_id: str, otp: str) -> AgentWalletStatus:
        """Submit OTP to complete login using Circle CLI v0.0.6 wallet-scoped command."""
        result = await self.runner.run_json(
            ("wallet", "login", "--request", request_id, "--otp", otp, "--output", "json"),
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
        return await self.agent_wallet_status()

    # ------------------------------------------------------------------
    # Gateway operations
    # ------------------------------------------------------------------

    async def gateway_balance(self, *, wallet_address: str, network: str) -> GatewayBalanceResult:
        """Get Circle Gateway balance for the configured wallet.

        Raises CircleCliOutputError for malformed balance responses.
        Never fabricates zero from malformed data.
        Rejects NaN, Infinity, -Infinity, and negative balances.
        """
        from decimal import Decimal, InvalidOperation

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

        def _validate_balance(raw: str, context: str) -> Decimal:
            """Parse and validate a balance string. Raises on any issue."""
            if not isinstance(raw, str):
                raise CircleCliOutputError(
                    f"Gateway balance {context} is not a string: {type(raw).__name__}"
                )
            if not raw.strip():
                raise CircleCliOutputError(f"Gateway balance {context} is empty")
            try:
                parsed = Decimal(raw)
            except (InvalidOperation, ValueError) as exc:
                raise CircleCliOutputError(
                    f"Gateway balance {context} is not a valid decimal: {raw!r}"
                ) from exc
            if not parsed.is_finite():
                raise CircleCliOutputError(f"Gateway balance {context} is not finite: {raw!r}")
            if parsed < 0:
                raise CircleCliOutputError(f"Gateway balance {context} is negative: {raw!r}")
            return parsed

        # Parse balance — fail closed on any malformed response
        balance_str: str | None = None

        if "total" in data:
            parsed = _validate_balance(data["total"], "'total'")
            balance_str = str(parsed)
        elif "balances" in data:
            balances = data["balances"]
            if not isinstance(balances, list):
                raise CircleCliOutputError(
                    f"Gateway balance 'balances' is not a list: {type(balances).__name__}"
                )
            total = Decimal("0")
            found_usdc = False
            for entry in balances:
                if not isinstance(entry, dict):
                    raise CircleCliOutputError(
                        f"Gateway balance entry is not an object: {type(entry).__name__}"
                    )
                symbol = entry.get("symbol")
                amount = entry.get("amount")
                if not isinstance(symbol, str):
                    raise CircleCliOutputError(f"Gateway balance entry missing 'symbol': {entry!r}")
                if not isinstance(amount, str):
                    raise CircleCliOutputError(
                        f"Gateway balance entry 'amount' is not a string: {amount!r}"
                    )
                parsed = _validate_balance(amount, f"entry[{symbol!r}].amount")
                if symbol == "USDC":
                    total += parsed
                    found_usdc = True
            if not found_usdc:
                raise CircleCliOutputError("Gateway balance response has no USDC entry")
            if not total.is_finite():
                raise CircleCliOutputError("Gateway balance summed total is not finite")
            balance_str = str(total)
        elif "totalBalance" in data:
            parsed = _validate_balance(data["totalBalance"], "'totalBalance'")
            balance_str = str(parsed)
        else:
            raise CircleCliOutputError(
                "Gateway balance response is missing 'total', 'balances', and 'totalBalance'"
            )

        domain = data.get("domain")
        return GatewayBalanceResult(
            total_usdc=balance_str,
            network=network,
            domain=int(domain) if isinstance(domain, int) else None,
        )

    async def gateway_deposit(
        self,
        *,
        wallet_address: str,
        network: str,
        amount: str,
        method: str = "direct",
    ) -> GatewayDepositResult:
        """Execute a Gateway deposit using Circle CLI v0.0.6.

        Args:
            wallet_address: Wallet address for the deposit.
            network: CLI chain code (e.g., "ARC-TESTNET").
            amount: USDC amount to deposit.
            method: Deposit method — "direct" for same-chain, "eco" for
                    cross-chain (Base/Base Sepolia only).
        """
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
                    "--method",
                    method,
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
