"""ContextVar bridge for x402 payment context propagation."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional


@dataclass
class PaymentContext:
    """Payment context propagated from seller middleware to protected handlers/tools."""

    payer: str
    amount: str
    network: str
    transaction: Optional[str] = None


_PAYMENT_CONTEXT: contextvars.ContextVar[Optional[PaymentContext]] = contextvars.ContextVar(
    "hermes_x402_payment_context", default=None
)


def set_payment_context(
    payer: str,
    amount: str,
    network: str,
    transaction: Optional[str] = None,
) -> PaymentContext:
    """Set the current payment context and return the value.

    Backward-compatible helper for callers that do not need token-based reset.
    Request handlers should prefer :func:`set_payment_context_token` and reset
    the returned token in a ``finally`` block.
    """
    ctx = PaymentContext(payer=payer, amount=amount, network=network, transaction=transaction)
    _PAYMENT_CONTEXT.set(ctx)
    return ctx


def set_payment_context_token(
    payer: str,
    amount: str,
    network: str,
    transaction: Optional[str] = None,
) -> contextvars.Token[Optional[PaymentContext]]:
    """Set payment context and return the ContextVar token for safe reset."""
    ctx = PaymentContext(payer=payer, amount=amount, network=network, transaction=transaction)
    return _PAYMENT_CONTEXT.set(ctx)


def reset_payment_context(token: contextvars.Token[Optional[PaymentContext]]) -> None:
    """Reset payment context using a token returned by set_payment_context_token()."""
    _PAYMENT_CONTEXT.reset(token)


def get_payment_context() -> Optional[PaymentContext]:
    """Get the current payment context."""
    return _PAYMENT_CONTEXT.get()


def clear_payment_context() -> None:
    """Clear the current payment context without token restoration."""
    _PAYMENT_CONTEXT.set(None)


class X402ContextBridge:
    """Compatibility wrapper around the payment ContextVar."""

    @staticmethod
    def set(
        payer: str,
        amount: str,
        network: str,
        transaction: Optional[str] = None,
    ) -> PaymentContext:
        return set_payment_context(payer, amount, network, transaction)

    @staticmethod
    def current() -> Optional[PaymentContext]:
        return get_payment_context()

    @staticmethod
    def clear() -> None:
        clear_payment_context()
