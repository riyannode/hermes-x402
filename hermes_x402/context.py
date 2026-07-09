"""ContextVar bridge for x402 payment context propagation.

When the seller middleware verifies a payment, the payment proof (payer, amount,
network, transaction) is stored in a ContextVar. Tools executing later can read
this to know who paid and how much — without passing the request object around.

This solves the critical gap: aiohttp middleware runs before the handler, but
Hermes tools execute in a separate context. ContextVars bridge the two.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional


@dataclass
class PaymentContext:
    """Payment context propagated from middleware to tools."""

    payer: str
    amount: str
    network: str
    transaction: Optional[str] = None


# ContextVar — survives across async boundaries
_PAYMENT_CONTEXT: contextvars.ContextVar[Optional[PaymentContext]] = contextvars.ContextVar(
    "hermes_x402_payment_context", default=None
)


def set_payment_context(
    payer: str,
    amount: str,
    network: str,
    transaction: Optional[str] = None,
) -> PaymentContext:
    """Set the current payment context (called by seller middleware)."""
    ctx = PaymentContext(payer=payer, amount=amount, network=network, transaction=transaction)
    _PAYMENT_CONTEXT.set(ctx)
    return ctx


def get_payment_context() -> Optional[PaymentContext]:
    """Get the current payment context (called by buyer tools)."""
    return _PAYMENT_CONTEXT.get()


def clear_payment_context() -> None:
    """Clear the current payment context."""
    _PAYMENT_CONTEXT.set(None)


class X402ContextBridge:
    """Manages payment context lifecycle for a request.

    Usage in middleware:
        bridge = X402ContextBridge()
        bridge.set(payer="0x...", amount="10000", network="eip155:5042002")

    Usage in tool:
        ctx = X402ContextBridge.current()
        if ctx:
            print(f"Paid by {ctx.payer}")
    """

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
