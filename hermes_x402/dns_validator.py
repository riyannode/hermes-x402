"""Async DNS resolution with SSRF destination validation.

Resolves hostnames to IP addresses via ``socket.getaddrinfo`` (run in a
thread-pool to stay non-blocking) and rejects any destination whose resolved
addresses include loopback, private, link-local, metadata, reserved,
multicast, or unspecified ranges.

.. important:: **DNS Rebinding TOCTOU Limitation**

   This validator checks IPs at *resolution time*.  Between the moment we
   validate and the moment the HTTP client actually connects, a DNS rebinding
   attack can swap the record to a private address.  Mitigations:

   * Use this module *closest* to the connection point (ideally right before
     ``aiohttp.request`` / ``httpx.AsyncClient`` call).
   * Where possible, pin the resolved IP into the ``Host`` header or connect
     directly via ``aiohttp.TCPConnector`` with a pre-resolved IP and SNI.
   * For high-security deployments, run the full HTTP request through a
     network-level deny-list or egress proxy rather than relying solely on
     application-layer DNS checks.

   This is an inherent limitation of any application-layer SSRF defence that
   does not own the connection socket.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hard cap on the number of A/AAAA records we process to bound work.
_MAX_RESOLVED_RECORDS: int = 10

# Timeout (seconds) for each DNS resolution attempt.
_RESOLUTION_ATTEMPT_TIMEOUT: float = 5.0

# Maximum number of attempts for transient DNS failures.
_MAX_RESOLUTION_ATTEMPTS: int = 3

# Bounded backoff schedule between attempts (seconds).
_RESOLUTION_BACKOFFS: tuple[float, ...] = (0.25, 0.75)


# ---------------------------------------------------------------------------
# Resolver abstraction
# ---------------------------------------------------------------------------


@runtime_checkable
class AsyncResolver(Protocol):
    """Protocol for an injectable async DNS resolver.

    Implementations receive a hostname (already IDNA-normalised) and an
    address family hint, and return a list of ``(family, address)`` tuples
    suitable for passing to ``socket.connect()``.
    """

    async def resolve(
        self,
        host: str,
        family: int = 0,
    ) -> list[tuple[int, str]]:
        """Resolve *host* and return ``(socket.AF_*, ip_string)`` pairs."""
        ...


class DefaultResolver:
    """Production resolver backed by ``socket.getaddrinfo`` in a thread."""

    async def resolve(
        self,
        host: str,
        family: int = 0,  # AF_UNSPEC → both IPv4 & IPv6
    ) -> list[tuple[int, str]]:
        """Resolve *host* via the system resolver (non-blocking wrapper)."""

        def _blocking() -> list[tuple[int, str]]:
            # socket.getaddrinfo returns (family, type, proto, canonname,
            # sockaddr) tuples.  We only need family + IP string.
            results: list[tuple[int, str]] = []
            try:
                for fam, _type, _proto, _canon, sockaddr in socket_getaddrinfo(host, family, 0):
                    # sockaddr is (ip, port) for IPv4 or (ip, port, flow, scope)
                    # for IPv6.  We only want the IP.
                    ip_str = sockaddr[0]
                    # Sanity: only accept dotted-decimal / colon-hex forms
                    # that ``ip_address`` can parse.
                    try:
                        ipaddress.ip_address(ip_str)
                    except ValueError:
                        continue
                    results.append((fam, ip_str))
            except _socket_mod.gaierror:
                raise
            except OSError as exc:
                logger.debug("getaddrinfo failed for %s: %s", host, exc)
            return results

        return await asyncio.to_thread(_blocking)


# We import the raw function once so tests can monkey-patch it easily.
import socket as _socket_mod  # noqa: E402  (must follow __future__ imports)

socket_getaddrinfo = _socket_mod.getaddrinfo  # public for test replacement


# ---------------------------------------------------------------------------
# IP forbidden checks
# ---------------------------------------------------------------------------

# Cloud metadata endpoints that may not be caught by ``is_private``.
_EXTRA_BLOCKED_IPS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure metadata
        "100.100.100.200",  # Aliyun (Alibaba Cloud) metadata
    }
)


def is_ip_forbidden(ip_str: str) -> bool:
    """Return ``True`` if *ip_str* is an address we must never connect to.

    Covers:

    * **Loopback** – ``127.0.0.0/8``, ``::1``
    * **Private** – ``10.0.0.0/8``, ``172.16.0.0/12``, ``192.168.0.0/16``,
      ``fc00::/7``
    * **Link-local** – ``169.254.0.0/16``, ``fe80::/10``
    * **Reserved / special** – ``100.64.0.0/10`` (carrier-grade NAT),
      ``192.0.0.0/24``, ``192.88.79.0/24``, etc.
    * **Multicast** – ``224.0.0.0/4``, ``ff00::/8``
    * **Unspecified** – ``0.0.0.0``, ``::``
    * **Cloud metadata** – ``169.254.169.254``, ``100.100.100.200``

    This function is *synchronous* so it can be used outside async contexts.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # If it can't be parsed as an IP at all, block it.
        return True

    # Standard flag checks (covers loopback, private, link-local, reserved,
    # multicast, unspecified).
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return True

    # Explicit cloud-metadata addresses that ``is_private`` / ``is_reserved``
    # may or may not flag depending on the Python / stdlib version.
    if ip_str in _EXTRA_BLOCKED_IPS:
        return True

    # The entire 169.254.0.0/16 link-local block (is_link_local above should
    # catch this, but belt-and-suspenders for older Python builds).
    if addr.version == 4:
        octets = ip_str.split(".")
        if len(octets) == 4 and octets[0] == "169" and octets[1] == "254":
            return True

    return False


class DnsValidationError(ValueError):
    """Structured DNS/destination validation failure for tool error mapping."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retry_safe: bool,
        attempts: int = 0,
        elapsed_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_safe = retry_safe
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------


async def resolve_and_validate_destination(
    url: str,
    *,
    resolver: AsyncResolver | None = None,
) -> tuple[str, ...]:
    """Resolve the hostname in *url* and validate every resulting IP.

    Parameters
    ----------
    url:
        A full URL string (``https://example.com/path``).
    resolver:
        Optional :class:`AsyncResolver` implementation for testing.
        Defaults to :class:`DefaultResolver`.

    Returns
    -------
    tuple[str, ...]
        A tuple of validated IP address strings on success.

    Raises
    ------
    ValueError
        If the destination is invalid, resolves to zero addresses, or any
        resolved address is forbidden.  The exception message is a
        human-readable, sanitised error string suitable for user display.
    """
    if not url or not isinstance(url, str):
        raise DnsValidationError(
            "URL is required for DNS validation.",
            error_code="destination_not_allowed",
            retry_safe=False,
        )

    if resolver is None:
        resolver = DefaultResolver()

    # --- Parse hostname ---
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise DnsValidationError(
            "URL must contain a valid hostname for DNS validation.",
            error_code="destination_not_allowed",
            retry_safe=False,
        )

    # --- IDNA normalization ---
    hostname = _normalise_idna(hostname)

    # --- Resolve with bounded retry for transient resolver failures only ---
    resolved, attempts, elapsed_ms = await _resolve_with_retries(hostname, resolver)

    if not resolved:
        raise DnsValidationError(
            f"DNS resolution for {_safe_host(hostname)} returned no addresses "
            f"after {attempts} attempt(s) in {elapsed_ms}ms.",
            error_code="dns_resolution_failed",
            retry_safe=True,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
        )

    # --- Deduplicate, bound unique records, and validate every unique address ---
    unique_ips = _dedupe_resolved_ips(resolved)[:_MAX_RESOLVED_RECORDS]

    valid_ips: list[str] = []
    for ip_str in unique_ips:
        if is_ip_forbidden(ip_str):
            logger.info(
                "DNS validation rejected hostname=%s attempts=%d elapsed_ms=%d ip=%s "
                "classification=destination_ip_rejected",
                hostname,
                attempts,
                elapsed_ms,
                _safe_ip(ip_str),
            )
            raise DnsValidationError(
                f"Destination {_safe_host(hostname)} resolves to a forbidden "
                f"address: {_safe_ip(ip_str)}.",
                error_code="destination_ip_rejected",
                retry_safe=False,
                attempts=attempts,
                elapsed_ms=elapsed_ms,
            )
        valid_ips.append(ip_str)

    logger.info(
        "DNS validation passed hostname=%s attempts=%d elapsed_ms=%d resolved_ips=%s "
        "classification=success",
        hostname,
        attempts,
        elapsed_ms,
        valid_ips,
    )
    return tuple(valid_ips)


async def _resolve_with_retries(
    hostname: str,
    resolver: AsyncResolver,
) -> tuple[list[tuple[int, str]], int, int]:
    """Resolve with bounded retries for transient DNS failures only."""
    started = time.monotonic()
    final_failure_kind: Literal["timeout", "transient_dns"] | None = None
    last_exc: BaseException | None = None

    for attempt in range(1, _MAX_RESOLUTION_ATTEMPTS + 1):
        attempt_started = time.monotonic()
        try:
            resolved = await asyncio.wait_for(
                resolver.resolve(hostname, family=0),  # AF_UNSPEC → v4 + v6
                timeout=_RESOLUTION_ATTEMPT_TIMEOUT,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "DNS resolution attempt hostname=%s attempt=%d elapsed_ms=%d exception_class=None",
                hostname,
                attempt,
                int((time.monotonic() - attempt_started) * 1000),
            )
            return resolved, attempt, elapsed_ms
        except asyncio.TimeoutError as exc:
            final_failure_kind = "timeout"
            last_exc = exc
            logger.warning(
                "DNS resolution attempt hostname=%s attempt=%d elapsed_ms=%d exception_class=%s",
                hostname,
                attempt,
                int((time.monotonic() - attempt_started) * 1000),
                type(exc).__name__,
            )
        except _socket_mod.gaierror as exc:
            final_failure_kind = "transient_dns"
            last_exc = exc
            logger.warning(
                "DNS resolution attempt hostname=%s attempt=%d elapsed_ms=%d exception_class=%s",
                hostname,
                attempt,
                int((time.monotonic() - attempt_started) * 1000),
                type(exc).__name__,
            )
            if not _is_temporary_gaierror(exc):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "DNS validation failed hostname=%s attempts=%d elapsed_ms=%d "
                    "classification=dns_resolution_failed",
                    hostname,
                    attempt,
                    elapsed_ms,
                )
                raise DnsValidationError(
                    f"DNS resolution for {_safe_host(hostname)} failed after "
                    f"{attempt} attempt(s) in {elapsed_ms}ms.",
                    error_code="dns_resolution_failed",
                    retry_safe=True,
                    attempts=attempt,
                    elapsed_ms=elapsed_ms,
                ) from exc
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "DNS resolution attempt hostname=%s attempt=%d elapsed_ms=%d exception_class=%s",
                hostname,
                attempt,
                int((time.monotonic() - attempt_started) * 1000),
                type(exc).__name__,
            )
            logger.info(
                "DNS validation failed hostname=%s attempts=%d elapsed_ms=%d "
                "classification=dns_resolution_failed",
                hostname,
                attempt,
                elapsed_ms,
            )
            raise DnsValidationError(
                f"DNS resolution for {_safe_host(hostname)} failed after "
                f"{attempt} attempt(s) in {elapsed_ms}ms.",
                error_code="dns_resolution_failed",
                retry_safe=True,
                attempts=attempt,
                elapsed_ms=elapsed_ms,
            ) from exc

        if attempt < _MAX_RESOLUTION_ATTEMPTS:
            await asyncio.sleep(_RESOLUTION_BACKOFFS[attempt - 1])

    elapsed_ms = int((time.monotonic() - started) * 1000)
    attempts = _MAX_RESOLUTION_ATTEMPTS
    if final_failure_kind == "timeout":
        logger.info(
            "DNS validation failed hostname=%s attempts=%d elapsed_ms=%d "
            "classification=dns_resolution_timeout",
            hostname,
            attempts,
            elapsed_ms,
        )
        raise DnsValidationError(
            f"DNS resolution for {_safe_host(hostname)} timed out after "
            f"{attempts} attempts in {elapsed_ms}ms.",
            error_code="dns_resolution_timeout",
            retry_safe=True,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
        ) from last_exc

    logger.info(
        "DNS validation failed hostname=%s attempts=%d elapsed_ms=%d "
        "classification=dns_resolution_failed",
        hostname,
        attempts,
        elapsed_ms,
    )
    raise DnsValidationError(
        f"DNS resolution for {_safe_host(hostname)} failed after "
        f"{attempts} attempts in {elapsed_ms}ms.",
        error_code="dns_resolution_failed",
        retry_safe=True,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
    ) from last_exc


def _is_temporary_gaierror(exc: _socket_mod.gaierror) -> bool:
    """Return True only for resolver errors documented as temporary."""
    return bool(exc.args and exc.args[0] == getattr(_socket_mod, "EAI_AGAIN", object()))


def _dedupe_resolved_ips(resolved: list[tuple[int, str]]) -> list[str]:
    """Deduplicate getaddrinfo output while preserving deterministic order."""
    unique: list[str] = []
    seen: set[str] = set()
    for _family, ip_str in resolved:
        if ip_str in seen:
            continue
        try:
            ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        seen.add(ip_str)
        unique.append(ip_str)
    return unique


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_idna(hostname: str) -> str:
    """Encode internationalised hostnames to IDNA (punycode).

    Falls back to the original hostname if encoding fails (e.g. already
    ASCII).
    """
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError):
        return hostname.lower()


def _safe_host(hostname: str) -> str:
    """Return a sanitised hostname for error messages (no special chars)."""
    # Only allow alphanumeric, hyphens, dots – nothing that could be
    # interpreted as markup or control characters.
    return "".join(c for c in hostname if c.isalnum() or c in "-.") or "<invalid>"


def _safe_ip(ip_str: str) -> str:
    """Redact / mask an IP string for logging if it is truly internal."""
    # We intentionally *do* reveal the IP in error messages because the
    # caller needs to know *why* the request was blocked.  For production
    # logging you may want to redact; the error messages are for humans.
    return ip_str
