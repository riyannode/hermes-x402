"""Immutable, bounded models for documented Circle CLI JSON output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class CircleCliResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    parsed: dict[str, Any] | list[Any] | None


@dataclass(frozen=True)
class CircleCliVersion:
    value: str


@dataclass(frozen=True)
class AgentWalletStatus:
    mainnet_status: str
    testnet_status: str
    email: str | None = None

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


Operation = Literal["read", "auth", "payment"]
