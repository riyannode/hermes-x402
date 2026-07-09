"""Hermes x402 — Circle nanopayment integration for Hermes Agent.

Seller: aiohttp middleware that settles directly via Circle Gateway (no verify step).
Buyer: Hermes tool definition using DCW signing (no raw private key).
Dual-role: Both seller + buyer in one agent.
"""

from hermes_x402.config import ARC_MAINNET, ARC_TESTNET, X402Config
from hermes_x402.middleware import create_aiohttp_middleware, X402SellerMiddleware
from hermes_x402.buyer import create_buyer_tool, X402BuyerTool
from hermes_x402.context import X402ContextBridge, get_payment_context, set_payment_context
from hermes_x402.agent import X402HermesAgent

__version__ = "0.1.0"
__all__ = [
    # Config
    "X402Config",
    "ARC_TESTNET",
    "ARC_MAINNET",
    # Seller
    "create_aiohttp_middleware",
    "X402SellerMiddleware",
    # Buyer
    "create_buyer_tool",
    "X402BuyerTool",
    # Context
    "X402ContextBridge",
    "get_payment_context",
    "set_payment_context",
    # Agent
    "X402HermesAgent",
]
