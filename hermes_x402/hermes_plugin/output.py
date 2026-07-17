"""Safe output formatting for tool responses."""

from __future__ import annotations

import json
from typing import Any

from hermes_x402.hermes_plugin.schemas import MAX_OUTPUT_SIZE


def _truncate_text(text: str, limit: int = MAX_OUTPUT_SIZE) -> tuple[str, bool]:
    """Truncate text to limit, returning (truncated_text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    return text[: limit - 20] + "\n[... truncated ...]", True


def _safe_data_output(data: Any, original_size: int | None = None) -> dict[str, Any]:
    """Wrap data in a safe output structure with truncation."""
    if isinstance(data, str):
        truncated_text, was_truncated = _truncate_text(data)
        result: dict[str, Any] = {"data": truncated_text}
        if was_truncated:
            result["truncated"] = True
            result["original_size"] = original_size or len(data)
        return result
    if isinstance(data, (dict, list)):
        raw = json.dumps(data, ensure_ascii=False, default=str)
        truncated_text, was_truncated = _truncate_text(raw)
        result = {"data": data}
        if was_truncated:
            result["data"] = truncated_text
            result["truncated"] = True
            result["original_size"] = original_size or len(raw)
        return result
    return {"data": str(data)}


def _normalize_balance(balance_str: str) -> str:
    """Normalize a USDC balance string."""
    try:
        amount = int(balance_str)
        return f"{amount / 1_000_000:.6f}"
    except (ValueError, TypeError):
        return str(balance_str)


def safe_wallet_address(address: str) -> str:
    """Mask wallet address for safe display."""
    if not address or len(address) < 10:
        return address
    return address[:6] + "..." + address[-4:]


def format_json_result(data: Any) -> str:
    """Format any data as a JSON string result."""
    return json.dumps(data, ensure_ascii=False, default=str)
