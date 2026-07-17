"""Shared Gateway preview state accessible from approval hook and handlers.

This module owns the in-memory preview store so that both the approval hook
(in entry.py) and the tool handlers (in tools.py) can read preview data
without circular imports.

Thread-safe: all mutations go through an RLock so concurrent Hermes sessions
cannot race on preview claim/consume.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

# Maximum active previews to prevent unbounded memory growth
_MAX_ACTIVE_PREVIEWS = 256

# Module-level preview store (in-memory only, not persisted)
_previews: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def _purge_expired() -> int:
    """Remove expired previews under the lock. Returns count removed."""
    now = time.time()
    expired = [k for k, v in _previews.items() if now > v.get("expires_at", 0)]
    for k in expired:
        _previews.pop(k, None)
    return len(expired)


def store_preview(preview_id: str, data: dict[str, Any]) -> None:
    """Store a defensive copy of a preview entry.

    Purges expired entries and enforces the active cap.
    """
    with _lock:
        _purge_expired()

        # Enforce cap: if at limit, reject new preview
        if len(_previews) >= _MAX_ACTIVE_PREVIEWS:
            raise RuntimeError(
                f"Gateway preview store is full ({_MAX_ACTIVE_PREVIEWS}). "
                "Wait for existing previews to expire."
            )

        # Store a defensive deep copy
        _previews[preview_id] = copy.deepcopy(data)


def get_preview(preview_id: str) -> dict[str, Any] | None:
    """Get a defensive copy of a preview entry by ID."""
    with _lock:
        preview = _previews.get(preview_id)
        if preview is None:
            return None
        return copy.deepcopy(preview)


def pop_preview(preview_id: str) -> dict[str, Any] | None:
    """Remove and return a defensive copy of a preview entry."""
    with _lock:
        preview = _previews.pop(preview_id, None)
        if preview is None:
            return None
        return copy.deepcopy(preview)


def mark_preview_consumed(preview_id: str) -> None:
    """Mark a preview as consumed."""
    with _lock:
        if preview_id in _previews:
            _previews[preview_id]["consumed"] = True


def claim_preview_for_execution(preview_id: str) -> dict[str, Any] | None:
    """Atomically claim a preview for execution under a single lock.

    Under one lock:
    - verify existence
    - verify expiry
    - verify not consumed
    - mark consumed
    - return a defensive/deep copy of the preview

    Only one concurrent caller may successfully claim a preview.
    Returns None if the preview cannot be claimed.
    """
    with _lock:
        preview = _previews.get(preview_id)
        if preview is None:
            return None

        now = time.time()
        if now > preview.get("expires_at", 0):
            # Purge expired entry
            _previews.pop(preview_id, None)
            return None

        if preview.get("consumed"):
            return None

        # Mark consumed before returning copy
        preview["consumed"] = True

        return copy.deepcopy(preview)


def get_gateway_preview_approval_summary(preview_id: str) -> dict[str, Any] | None:
    """Return a read-only sanitized summary for approval display.

    Returns None if the preview is missing, expired, or consumed.
    Never exposes body, wallet raw address, or internal fingerprints.
    """
    with _lock:
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
