"""New-host approval mechanism for x402 buyer requests.

Provides a trust-based gate for first-time hosts, backed by a local
JSON file at ``~/.hermes/x402_trusted_hosts.json``.  When enabled,
any host not in the trusted store requires explicit approval before
payment can proceed.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRUSTED_HOSTS_DIR = Path("~/.hermes").expanduser()
_TRUSTED_HOSTS_FILE = _TRUSTED_HOSTS_DIR / "x402_trusted_hosts.json"

# ---------------------------------------------------------------------------
# TrustedHostStore
# ---------------------------------------------------------------------------


class TrustedHostStore:
    """Thread-safe, file-backed store for trusted x402 hostnames.

    Persists to ``~/.hermes/x402_trusted_hosts.json``.  All operations
    are atomic: the file is read on load and written through a temporary
    path with an ``os.replace`` swap.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _TRUSTED_HOSTS_FILE
        self._lock = threading.Lock()
        self._hosts: set[str] = set()
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Load the file if not already loaded."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_from_disk()
            self._loaded = True

    def _load_from_disk(self) -> None:
        """Read trusted hosts from the JSON file."""
        if not self._path.exists():
            self._hosts = set()
            return

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and "trusted_hosts" in data:
                hosts = data["trusted_hosts"]
            elif isinstance(data, list):
                hosts = data
            else:
                hosts = []
            self._hosts = set(h.lower() for h in hosts if isinstance(h, str) and h)
        except (json.JSONDecodeError, OSError):
            self._hosts = set()

    def _save_to_disk(self) -> None:
        """Write trusted hosts to disk atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "trusted_hosts": sorted(self._hosts),
        }
        tmp_path = self._path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2, sort_keys=False) + "\n",
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(self._path))
        except OSError:
            # Best-effort: if write fails, state is still in memory
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)

    def is_trusted(self, host: str) -> bool:
        """Check if a hostname is in the trusted store."""
        self._ensure_loaded()
        return host.lower() in self._hosts

    def trust(self, host: str) -> None:
        """Add a hostname to the trusted store."""
        self._ensure_loaded()
        with self._lock:
            self._hosts.add(host.lower())
            self._save_to_disk()

    def untrust(self, host: str) -> None:
        """Remove a hostname from the trusted store."""
        self._ensure_loaded()
        with self._lock:
            self._hosts.discard(host.lower())
            self._save_to_disk()

    def list_trusted(self) -> list[str]:
        """Return sorted list of all trusted hostnames."""
        self._ensure_loaded()
        with self._lock:
            return sorted(self._hosts)


# Module-level singleton
_store: TrustedHostStore | None = None
_store_lock = threading.Lock()


def _get_store() -> TrustedHostStore:
    """Get or create the global TrustedHostStore singleton."""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        _store = TrustedHostStore()
        return _store


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def parse_approval_config() -> dict[str, Any]:
    """Parse approval configuration from environment variables.

    Returns:
        Dict with ``require_approval`` flag.
    """
    raw = os.environ.get("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "").strip().lower()
    return {
        "require_approval": raw in {"1", "true", "yes"},
    }


def check_approval_required(url: str, config: Any = None) -> dict[str, Any] | None:
    """Check if a URL requires new-host approval.

    Args:
        url: The resource URL to check.
        config: Optional config object. If it has a ``require_approval``
                attribute, that takes precedence over env vars.

    Returns:
        None if approval is not required (or not configured).
        A dict with approval_required info if the host is not trusted.
    """
    # Determine if approval is required
    require_approval = False

    if config is not None:
        # Config explicitly overrides env var
        require_approval = getattr(config, "require_approval", None)
        if require_approval is not None:
            # Config explicitly set — use it, don't fall through to env
            pass
        else:
            require_approval = False
    if not require_approval and config is None:
        approval_config = parse_approval_config()
        require_approval = approval_config.get("require_approval", False)

    if not require_approval:
        return None

    # Parse hostname from URL
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return {
            "error": "approval_required",
            "host": "",
            "new_host": True,
            "message": "Could not parse hostname from URL.",
        }

    # Check trusted store
    store = _get_store()
    if store.is_trusted(hostname):
        return None

    return {
        "error": "approval_required",
        "host": hostname,
        "new_host": True,
        "message": (
            f"Host '{hostname}' has not been approved. "
            "Use trust_host() to approve, or set "
            "X402_REQUIRE_APPROVAL_FOR_NEW_HOST=false to disable."
        ),
    }


def trust_host(host: str) -> None:
    """Trust a hostname for x402 payments.

    This adds the host to the persistent trusted store at
    ``~/.hermes/x402_trusted_hosts.json``.
    """
    store = _get_store()
    store.trust(host)


def untrust_host(host: str) -> None:
    """Remove a hostname from the trusted store."""
    store = _get_store()
    store.untrust(host)


def is_host_trusted(host: str) -> bool:
    """Check if a hostname is in the trusted store."""
    store = _get_store()
    return store.is_trusted(host)


def list_trusted_hosts() -> list[str]:
    """Return sorted list of all trusted hostnames."""
    store = _get_store()
    return store.list_trusted()
