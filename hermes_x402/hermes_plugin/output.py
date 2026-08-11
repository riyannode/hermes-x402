"""Safe output formatting for tool responses."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

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


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_INVALID_HOST_DISPLAY = "[invalid host]"


def safe_host_for_display(value: Any) -> str:
    """Return a bounded hostname without credentials or control characters.

    Status output is model-visible, so malformed allowlist entries must not be
    echoed raw. Reject rather than repair control characters or userinfo.
    """
    if not isinstance(value, str) or not value or _CONTROL_CHARS.search(value):
        return _INVALID_HOST_DISPLAY
    try:
        parsed = urlparse(f"//{value}")
        if parsed.username is not None or parsed.password is not None:
            return _INVALID_HOST_DISPLAY
        hostname = parsed.hostname
    except (ValueError, UnicodeError):
        return _INVALID_HOST_DISPLAY
    if not hostname:
        return _INVALID_HOST_DISPLAY
    return hostname[:253]


def safe_host_allowlist_for_display(values: Any) -> list[str]:
    """Sanitize every configured allowlist entry for model-visible output."""
    if not isinstance(values, (list, tuple)):
        return []
    return [safe_host_for_display(value) for value in values]


def format_json_result(data: Any) -> str:
    """Format any data as a JSON string result."""
    return json.dumps(data, ensure_ascii=False, default=str)
