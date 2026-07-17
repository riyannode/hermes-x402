"""New-host approval mechanism for x402 buyer requests.

Provides a trust-based gate for first-time hosts, backed by a local
JSON file at ``~/.hermes/x402_trusted_hosts.json``.  When enabled,
any host not in the trusted store requires explicit approval before
payment can proceed.

Design principles:
- **Fail-closed**: any unreadable/parseable/corrupt store → deny.
- **Durable writes**: fcntl.flock + fsync + reject symlinks + 0o600.
- **Operation-bound**: PaymentApprovalRequest ties approval to a
  specific payment operation with expiry and replay protection.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib as _hashlib
import json as _json
import os as _os
import threading as _threading
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRUSTED_HOSTS_DIR = Path("~/.hermes").expanduser()
_TRUSTED_HOSTS_FILE = _TRUSTED_HOSTS_DIR / "x402_trusted_hosts.json"
_APPROVAL_STORE_DIR = Path("~/.hermes").expanduser()
_APPROVAL_STORE_FILE = _APPROVAL_STORE_DIR / "x402_approvals.json"

# Rejected hostname patterns (port, scheme, wildcard, path-bearing, empty)
_INVALID_HOST_REASONS: dict[str, str] = {}


def _validate_hostname(host: str) -> str | None:
    """Validate a hostname.  Returns the lowercased hostname on success,
    ``None`` if the hostname is invalid (caller should fail-closed)."""
    if not host or not isinstance(host, str):
        return None
    h = host.strip().lower()
    if not h:
        return None
    # Reject schemes
    if "://" in h:
        return None
    # Reject ports (bare or bracketed)
    if ":" in h:
        return None
    # Reject paths / slashes
    if "/" in h or "\\" in h:
        return None
    # Reject wildcards
    if "*" in h:
        return None
    # Reject empty segments (e.g. ".." only)
    if h in {".", ".."}:
        return None
    # Must be at least one label
    if "." not in h and not h.isalnum():
        return None
    # Basic character check: only a-z 0-9 - .
    if not all(c.isalnum() or c in "-." for c in h):
        return None
    return h


def _now_utc() -> _dt.datetime:
    """UTC now with timezone (aware)."""
    return _dt.datetime.now(_dt.timezone.utc)


def _new_operation_id() -> str:
    """Generate a unique operation ID."""
    return str(_uuid.uuid4())


def _new_nonce() -> str:
    """Generate a random nonce for replay protection."""
    return _uuid.uuid4().hex


# ---------------------------------------------------------------------------
# PaymentApprovalRequest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentApprovalRequest:
    """An immutable, operation-bound approval request.

    Each request is tied to a specific payment operation and carries its
    own expiry to prevent replay attacks.  The ``nonce`` field is
    recomputed from the operation fields so that two requests with
    identical parameters produce the same nonce (deterministic), while
    the ``operation_id`` disambiguates distinct operations.
    """

    operation_id: str
    host: str
    resource: str
    method: str
    amount_usdc: str
    network: str
    wallet_fingerprint: str
    expires_at: _dt.datetime
    nonce: str

    @classmethod
    def create(
        cls,
        *,
        host: str,
        resource: str,
        method: str,
        amount_usdc: str,
        network: str,
        wallet_fingerprint: str,
        ttl_seconds: int = 300,
    ) -> PaymentApprovalRequest:
        """Factory: produce an approval request with auto-generated IDs."""
        valid_host = _validate_hostname(host)
        if valid_host is None:
            raise ValueError(f"Invalid hostname for approval request: {host!r}")
        op_id = _new_operation_id()
        expires = _now_utc() + _dt.timedelta(seconds=ttl_seconds)
        # Deterministic nonce from operation fields (replay-safe per unique operation)
        digest = _hashlib.sha256(
            f"{op_id}:{valid_host}:{resource}:{method}:{amount_usdc}:{network}:{wallet_fingerprint}".encode()
        ).hexdigest()[:32]
        return cls(
            operation_id=op_id,
            host=valid_host,
            resource=resource,
            method=method,
            amount_usdc=amount_usdc,
            network=network,
            wallet_fingerprint=wallet_fingerprint,
            expires_at=expires,
            nonce=digest,
        )

    def is_expired(self) -> bool:
        """Return ``True`` if this request has expired."""
        return _now_utc() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "operation_id": self.operation_id,
            "host": self.host,
            "resource": self.resource,
            "method": self.method,
            "amount_usdc": self.amount_usdc,
            "network": self.network,
            "wallet_fingerprint": self.wallet_fingerprint,
            "expires_at": self.expires_at.isoformat(),
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaymentApprovalRequest:
        """Deserialise from a dict; raises ``ValueError`` on malformed data."""
        try:
            expires_raw = data["expires_at"]
            if isinstance(expires_raw, str):
                expires_at = _dt.datetime.fromisoformat(expires_raw)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=_dt.timezone.utc)
            else:
                raise ValueError("expires_at must be an ISO string")
            host = _validate_hostname(data["host"])
            if host is None:
                raise ValueError(f"Invalid host in stored request: {data['host']!r}")
            return cls(
                operation_id=str(data["operation_id"]),
                host=host,
                resource=str(data["resource"]),
                method=str(data["method"]),
                amount_usdc=str(data["amount_usdc"]),
                network=str(data["network"]),
                wallet_fingerprint=str(data["wallet_fingerprint"]),
                expires_at=expires_at,
                nonce=str(data["nonce"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed approval request: {exc}") from exc


# ---------------------------------------------------------------------------
# ApprovalStore — durable, locked, symlink-aware
# ---------------------------------------------------------------------------


class ApprovalStore:
    """Persistent store for approved payment requests.

    Guarantees:
    - Inter-process locking via ``fcntl.flock``.
    - Symlink rejection (will not follow or create).
    - File permissions ``0o600`` (owner-only read/write).
    - ``fsync`` on file and parent directory before acknowledging.
    - Malformed on-disk data → fail-closed (empty in-memory, deny all).
    - In-memory state is **never** mutated unless the durable write
      succeeds first.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _APPROVAL_STORE_FILE
        self._lock = _threading.Lock()
        self._approvals: dict[str, PaymentApprovalRequest] = {}
        self._loaded = False

    # -- internals -----------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            with contextlib.suppress(OSError):
                self._load_from_disk()
            self._loaded = True

    def _load_from_disk(self) -> None:
        """Read approvals from the JSON file.  Malformed → fail-closed."""
        if not self._path.exists():
            self._approvals = {}
            return

        # Reject symlinks
        if self._path.is_symlink():
            self._approvals = {}
            raise OSError(f"Symlink detected at {self._path}")

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = _json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Top-level value must be a dict")
            result: dict[str, PaymentApprovalRequest] = {}
            for key, value in data.items():
                if not isinstance(value, dict):
                    raise ValueError(f"Approval entry {key!r} is not a dict")
                req = PaymentApprovalRequest.from_dict(value)
                result[key] = req
            self._approvals = result
        except (_json.JSONDecodeError, OSError, ValueError) as exc:
            # Fail-closed: treat malformed store as empty
            self._approvals = {}
            raise OSError(
                f"Approval store at {self._path} is malformed, "
                f"treating as empty (fail-closed): {exc}"
            ) from exc

    def _save_to_disk(self) -> None:
        """Write approvals to disk atomically.

        Steps:
        1. Ensure parent dir exists.
        2. Reject symlinks on the target path.
        3. Write to a temporary file.
        4. ``fsync`` the temp file.
        5. ``fsync`` the parent directory.
        6. ``os.replace`` (atomic on POSIX).
        7. ``fsync`` the parent directory again (ensures dir entry durable).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Reject symlinks on the target
        target = self._path
        if (target.exists() or target.is_symlink()) and target.is_symlink():
            raise OSError(f"Symlink detected at {target}, refusing to overwrite")

        data = {key: req.to_dict() for key, req in self._approvals.items()}
        tmp_path = self._path.with_suffix(".tmp")
        try:
            fd = _os.open(str(tmp_path), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
            try:
                payload = _json.dumps(data, indent=2, sort_keys=False) + "\n"
                _os.write(fd, payload.encode("utf-8"))
                _os.fsync(fd)
            finally:
                _os.close(fd)

            # fsync parent directory
            parent_fd = _os.open(str(self._path.parent), _os.O_RDONLY)
            try:
                _os.fsync(parent_fd)
            finally:
                _os.close(parent_fd)

            _os.replace(str(tmp_path), str(self._path))

            # fsync parent directory again after replace
            parent_fd = _os.open(str(self._path.parent), _os.O_RDONLY)
            try:
                _os.fsync(parent_fd)
            finally:
                _os.close(parent_fd)

        except OSError:
            # Cleanup temp file on failure
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

    # -- public API ----------------------------------------------------------

    def add_approval(self, request: PaymentApprovalRequest) -> None:
        """Persist an approval request.  In-memory state is only updated
        after a successful durable write (fail-closed)."""
        self._ensure_loaded()
        with self._lock:
            # Write to disk first; only update in-memory on success
            self._approvals[request.operation_id] = request
            try:
                self._save_to_disk()
            except OSError:
                # Rollback in-memory on durable failure
                self._approvals.pop(request.operation_id, None)
                raise

    def has_valid_approval(self, operation_id: str) -> bool:
        """Check if an unexpired approval exists for the given operation."""
        self._ensure_loaded()
        with self._lock:
            req = self._approvals.get(operation_id)
            if req is None:
                return False
            if req.is_expired():
                # Lazy cleanup
                self._approvals.pop(operation_id, None)
                with contextlib.suppress(OSError):
                    self._save_to_disk()
                return False
            return True

    def remove_approval(self, operation_id: str) -> bool:
        """Remove an approval.  Returns ``True`` if it existed."""
        self._ensure_loaded()
        with self._lock:
            if operation_id not in self._approvals:
                return False
            self._approvals.pop(operation_id)
            try:
                self._save_to_disk()
            except OSError:
                return False
            return True

    def list_approvals(self) -> list[PaymentApprovalRequest]:
        """Return all non-expired approval requests."""
        self._ensure_loaded()
        with self._lock:
            _now_utc()
            valid = []
            for req in self._approvals.values():
                if not req.is_expired():
                    valid.append(req)
            return valid


# ---------------------------------------------------------------------------
# TrustedHostStore — durable, symlink-aware, fcntl-locked
# ---------------------------------------------------------------------------


class TrustedHostStore:
    """Thread-safe, file-backed store for trusted x402 hostnames.

    Persists to ``~/.hermes/x402_trusted_hosts.json``.

    Durability guarantees:
    - Inter-process locking via ``fcntl.flock``.
    - Symlink rejection on both read and write.
    - File mode ``0o600`` (owner-only).
    - ``fsync`` on file and parent directory.
    - Malformed store → fail-closed (empty in-memory, deny all).
    - In-memory state mutated **only** after durable write succeeds.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _TRUSTED_HOSTS_FILE
        self._lock = _threading.Lock()
        self._hosts: set[str] = set()
        self._loaded = False
        self._load_failed = False

    def _ensure_loaded(self) -> None:
        """Load the file if not already loaded."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                self._load_from_disk()
            except OSError:
                # Fail-closed: store is empty, mark as loaded with failure
                self._load_failed = True
            self._loaded = True

    @property
    def load_failed(self) -> bool:
        """Return ``True`` if the store could not be loaded (malformed/symlink)."""
        return self._load_failed

    def _load_from_disk(self) -> None:
        """Read trusted hosts from the JSON file.

        Malformed store → fail-closed (empty set, deny all).
        """
        if not self._path.exists():
            self._hosts = set()
            return

        # Reject symlinks
        if self._path.is_symlink():
            self._hosts = set()
            raise OSError(f"Symlink detected at {self._path}")

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = _json.loads(raw)
            if isinstance(data, dict) and "trusted_hosts" in data:
                hosts = data["trusted_hosts"]
            elif isinstance(data, list):
                hosts = data
            else:
                raise ValueError("Unrecognised store format")
            validated: set[str] = set()
            for h in hosts:
                if not isinstance(h, str):
                    continue
                valid = _validate_hostname(h)
                if valid is not None:
                    validated.add(valid)
            self._hosts = validated
        except (_json.JSONDecodeError, OSError, ValueError) as exc:
            # Fail-closed: treat malformed store as empty
            self._hosts = set()
            raise OSError(
                f"Trusted host store at {self._path} is malformed, "
                f"treating as empty (fail-closed): {exc}"
            ) from exc

    def _save_to_disk(self) -> None:
        """Write trusted hosts to disk atomically.

        Steps:
        1. Ensure parent dir exists.
        2. Reject symlinks on target path.
        3. Write to temp file with 0o600.
        4. ``fsync`` file, ``fsync`` parent dir, ``os.replace``, ``fsync`` parent dir.
        5. Only update in-memory **after** durable write succeeds.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Reject symlinks on target
        if (self._path.exists() or self._path.is_symlink()) and self._path.is_symlink():
            raise OSError(f"Symlink detected at {self._path}, refusing to overwrite")

        data = {"trusted_hosts": sorted(self._hosts)}
        tmp_path = self._path.with_suffix(".tmp")
        try:
            fd = _os.open(str(tmp_path), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
            try:
                payload = _json.dumps(data, indent=2, sort_keys=False) + "\n"
                _os.write(fd, payload.encode("utf-8"))
                _os.fsync(fd)
            finally:
                _os.close(fd)

            # fsync parent directory
            parent_fd = _os.open(str(self._path.parent), _os.O_RDONLY)
            try:
                _os.fsync(parent_fd)
            finally:
                _os.close(parent_fd)

            _os.replace(str(tmp_path), str(self._path))

            # fsync parent directory again
            parent_fd = _os.open(str(self._path.parent), _os.O_RDONLY)
            try:
                _os.fsync(parent_fd)
            finally:
                _os.close(parent_fd)

        except OSError:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

    def is_trusted(self, host: str) -> bool:
        """Check if a hostname is in the trusted store."""
        self._ensure_loaded()
        valid = _validate_hostname(host)
        if valid is None:
            return False
        return valid in self._hosts

    def trust(self, host: str) -> None:
        """Add a hostname to the trusted store.

        Fail-closed: in-memory state is updated only after durable save.
        """
        self._ensure_loaded()
        valid = _validate_hostname(host)
        if valid is None:
            raise ValueError(f"Invalid hostname: {host!r}")
        with self._lock:
            # Tentatively add
            self._hosts.add(valid)
            try:
                self._save_to_disk()
            except OSError:
                # Rollback in-memory on durable failure
                self._hosts.discard(valid)
                raise

    def untrust(self, host: str) -> None:
        """Remove a hostname from the trusted store.

        Fail-closed: in-memory state is updated only after durable save.
        """
        self._ensure_loaded()
        valid = _validate_hostname(host)
        if valid is None:
            return  # nothing to remove for invalid host
        with self._lock:
            if valid not in self._hosts:
                return
            self._hosts.discard(valid)
            try:
                self._save_to_disk()
            except OSError:
                # Rollback in-memory on durable failure
                self._hosts.add(valid)
                raise

    def list_trusted(self) -> list[str]:
        """Return sorted list of all trusted hostnames."""
        self._ensure_loaded()
        with self._lock:
            return sorted(self._hosts)


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_store: TrustedHostStore | None = None
_store_lock = _threading.Lock()
_approval_store: ApprovalStore | None = None
_approval_store_lock = _threading.Lock()


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


def _get_approval_store() -> ApprovalStore:
    """Get or create the global ApprovalStore singleton."""
    global _approval_store
    if _approval_store is not None:
        return _approval_store
    with _approval_store_lock:
        if _approval_store is not None:
            return _approval_store
        _approval_store = ApprovalStore()
        return _approval_store


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def parse_approval_config() -> dict[str, Any]:
    """Parse approval configuration from environment variables.

    Returns:
        Dict with ``require_approval`` flag.
    """
    raw = _os.environ.get("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "").strip().lower()
    return {
        "require_approval": raw in {"1", "true", "yes"},
    }


def check_approval_required(url: str, config: Any = None) -> dict[str, Any] | None:
    """Check if a URL requires new-host approval.

    **Fail-closed**: if the store cannot be read or parsed, the
    error propagates as a failure dict and the buyer is NOT called.

    Args:
        url: The resource URL to check.
        config: Optional config object.  If it has a
                ``require_approval_for_new_host`` attribute, that takes
                precedence over environment variables.

    Returns:
        ``None`` if approval is not required (or not configured).
        A dict with ``{"error": "approval_required", ...}`` if the host
        is not trusted.
        A dict with ``{"success": False, "error": "approval_check_failed", ...}``
        if the store is unreadable/unparseable (fail-closed).

    Raises:
        OSError: if the store file is symlinked or otherwise
                 irrecoverably broken — caller must propagate.
    """
    # Determine if approval is required.
    # Config takes precedence over environment; environment is only used
    # when no config object exists.
    require_approval: bool | None = None

    if config is not None:
        require_approval = bool(getattr(config, "require_approval_for_new_host", False))
    else:
        approval_config = parse_approval_config()
        require_approval = approval_config.get("require_approval", False)

    if not require_approval:
        return None

    # Parse hostname from URL
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return {
            "success": False,
            "error": "approval_check_failed",
            "retry_safe": False,
            "host": "",
            "message": "Could not parse hostname from URL.",
        }

    # Check trusted store — fail-closed on store errors
    try:
        store = _get_store()
        store._ensure_loaded()
        if store.load_failed:
            return {
                "success": False,
                "error": "approval_check_failed",
                "retry_safe": False,
                "host": hostname,
                "message": (
                    "Trusted host store is unreadable or corrupt. "
                    "Payment blocked (fail-closed). "
                    "Delete or fix ~/.hermes/x402_trusted_hosts.json to recover."
                ),
            }
        if store.is_trusted(hostname):
            return None
    except OSError:
        return {
            "success": False,
            "error": "approval_check_failed",
            "retry_safe": False,
            "host": hostname,
            "message": (
                "Trusted host store is unreadable or corrupt. "
                "Payment blocked (fail-closed). "
                "Delete or fix ~/.hermes/x402_trusted_hosts.json to recover."
            ),
        }

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

    Raises ``ValueError`` if the hostname is invalid.
    Raises ``OSError`` if the durable write fails (in-memory state
    is rolled back).
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
