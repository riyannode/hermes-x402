"""Immutable, bounded models for documented Circle CLI JSON output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class CircleCliDiagnostics:
    stage: str
    monotonic_start: float
    elapsed_seconds: float | None = None
    executable: str = ""
    cwd: str | None = None
    sanitized_argv: tuple[str, ...] = ()
    pid: int | None = None
    return_code: int | None = None
    termination_signal: int | None = None
    timed_out: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    json_parse_ok: bool | None = None
    json_parse_error: str | None = None
    payment_log_dir: str | None = None
    payment_logs_before: tuple[str, ...] = ()
    payment_logs_after: tuple[str, ...] = ()
    new_payment_logs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CircleCliResult:
    argv: tuple[str, ...]  # OTP values are redacted to [REDACTED] for safety
    exit_code: int
    stdout: str
    stderr: str
    parsed: dict[str, Any] | list[Any] | None
    diagnostics: CircleCliDiagnostics | None = None


@dataclass(frozen=True)
class CircleCliVersion:
    value: str


@dataclass(frozen=True)
class AgentWalletStatus:
    mainnet_status: str
    testnet_status: str
    email: str | None = None
    terms_accepted: bool = True

    @property
    def authenticated(self) -> bool:
        return self.mainnet_status == "VALID" or self.testnet_status == "VALID"


@dataclass(frozen=True)
class AgentWallet:
    address: str
    blockchain: str
    created_at: str | None = None


@dataclass(frozen=True)
class WalletBalance:
    symbol: str
    amount: str
    token_address: str | None = None


@dataclass(frozen=True)
class CircleServicePayment:
    response: Any
    amount: str
    chain: str
    scheme: str
    seller: str
    receipt: str | None = None
    transaction_id: str | None = None


@dataclass(frozen=True)
class LoginStartResult:
    request_id: str
    email_masked: str
    otp_required: bool = True


@dataclass(frozen=True)
class GatewayBalanceResult:
    total_usdc: str
    network: str | None = None
    domain: int | None = None


@dataclass(frozen=True)
class GatewayDepositResult:
    operation_id: str | None = None
    transaction_hash: str | None = None
    status: str = "pending"
    network: str | None = None


Operation = Literal["read", "auth", "payment"]
