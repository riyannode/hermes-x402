"""Shared Gateway preview state accessible from approval hook and handlers.

This module owns the in-memory preview store so that both the approval hook
(in entry.py) and the tool handlers (in tools.py) can read preview data
without circular imports.
"""

from __future__ import annotations

import time
from typing import Any

# Module-level preview store (in-memory only, not persisted)
_previews: dict[str, dict[str, Any]] = {}


def store_preview(preview_id: str, data: dict[str, Any]) -> None:
    """Store a preview entry."""
    _previews[preview_id] = data


def get_preview(preview_id: str) -> dict[str, Any] | None:
    """Get a preview entry by ID."""
    return _previews.get(preview_id)


def pop_preview(preview_id: str) -> dict[str, Any] | None:
    """Remove and return a preview entry."""
    return _previews.pop(preview_id, None)


def mark_preview_consumed(preview_id: str) -> None:
    """Mark a preview as consumed."""
    if preview_id in _previews:
        _previews[preview_id]["consumed"] = True


def get_gateway_preview_approval_summary(preview_id: str) -> dict[str, Any] | None:
    """Return a read-only sanitized summary for approval display.

    Returns None if the preview is missing, expired, or consumed.
    Never exposes body, wallet raw address, or internal fingerprints.
    """
    preview = _previews.get(preview_id)
    if not preview:
        return None

    now = time.time()
    if now > preview.get("expires_at", 0):
        return None

    if preview.get("consumed"):
        return None

    wallet = preview.get("wallet", "")
    # Mask wallet: show first 6 and last 4 chars
    masked_wallet = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet

    return {
        "service_url": preview.get("service_url", "unknown"),
        "deposit_amount": preview.get("deposit_amount", "0"),
        "masked_wallet": masked_wallet,
        "network": preview.get("wallet_network", "unknown"),
        "deposit_method": preview.get("deposit_method", "direct"),
        "expires_at": preview.get("expires_at", 0),
    }
