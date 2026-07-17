"""Public-network and strict-allowlist policy for x402 URL validation.

Provides a unified policy layer used by: service search URLs, x402_supports,
x402_service_inspect, x402_fetch, x402_pay, and seller callback URL validation.

Two modes:
  - strict_allowlist: hostname must pass X402_HOST_ALLOWLIST
  - public: any public HTTP/HTTPS destination may be inspected/paid,
            but private/internal/credential-bearing URLs remain blocked.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from hermes_x402.buyer.errors import PaymentPolicyError


@dataclass(frozen=True)
class NetworkPolicy:
    """Immutable network policy evaluated before any HTTP request."""

    mode: Literal["strict_allowlist", "public"] = "strict_allowlist"
    host_allowlist: tuple[str, ...] = field(default_factory=tuple)
    allow_http: bool = False  # HTTPS by default; HTTP only for dev mode

    def validate_url(self, url: str) -> None:
        """Validate URL against this policy.  Raises PaymentPolicyError on failure."""
        err = validate_url_strict(url, self.host_allowlist, self.mode, self.allow_http)
        if err:
            raise PaymentPolicyError(err)

    def is_url_allowed(self, url: str) -> bool:
        """Return True if URL passes validation (no exception)."""
        try:
            self.validate_url(url)
            return True
        except PaymentPolicyError:
            return False

    def validate_destination(self, url: str) -> str | None:
        """Validate URL and return error string or None (no exception)."""
        return validate_url_strict(url, self.host_allowlist, self.mode, self.allow_http)


def validate_url_strict(
    url: str,
    host_allowlist: tuple[str, ...] | list[str],
    mode: Literal["strict_allowlist", "public"],
    allow_http: bool = False,
) -> str | None:
    """Validate URL against network policy. Returns error string or None."""
    if not url or not isinstance(url, str):
        return "URL is required."

    # Bounded URL length
    if len(url) > 2048:
        return "URL exceeds maximum length of 2048."

    parsed = urlparse(url)

    # Scheme validation
    if parsed.scheme not in {"https", "http"}:
        return "URL must use https or http scheme."

    # HTTP requires explicit dev mode
    if parsed.scheme == "http" and not allow_http:
        return "HTTP URLs are not allowed. Use HTTPS or enable allow_http for development."

    # Hostname required
    if not parsed.hostname:
        return "URL must have a valid hostname."

    # No credentials (userinfo)
    if parsed.username or parsed.password:
        return "URL must not contain credentials (userinfo)."

    hostname = parsed.hostname.lower()

    # Block well-known SSRF targets
    blocked = {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "metadata.google.internal",
        "169.254.169.254",
    }
    if hostname in blocked:
        return f"Host is blocked: {hostname}"

    # DNS-resolved IP checks (if hostname is an IP literal)
    _ip_err = _check_ip_address(hostname)
    if _ip_err:
        return _ip_err

    # Private/reserved/multicast/unspecified (best-effort for IP literals)
    _reserved_err = _check_reserved_ranges(hostname)
    if _reserved_err:
        return _reserved_err

    # Enforce policy based on mode
    if mode == "strict_allowlist":
        if host_allowlist:
            allowed = any(
                hostname == item.lower() or hostname.endswith(f".{item.lower()}")
                for item in host_allowlist
            )
            if not allowed:
                return f"Host not in allowlist: {hostname}"
        else:
            # Empty allowlist in strict mode = nothing allowed
            return "No hosts are allowed (empty allowlist in strict_allowlist mode)."

    elif mode == "public" and host_allowlist:
        # In public mode, private/reserved IPs are already blocked above.
        # An allowlist may optionally further restrict destinations.
        allowed = any(
            hostname == item.lower() or hostname.endswith(f".{item.lower()}")
            for item in host_allowlist
        )
        if not allowed:
            return f"Host not in public-mode allowlist: {hostname}"

    return None


def _check_ip_address(hostname: str) -> str | None:
    """Check if hostname is a literal IP and whether it's private/reserved."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return None  # domain name, not an IP

    if ip.is_loopback:
        return f"Host is a loopback address: {hostname}"
    if ip.is_private:
        return f"Host resolves to a private address: {hostname}"
    if ip.is_link_local:
        return f"Host resolves to a link-local address: {hostname}"
    if ip.is_reserved:
        return f"Host resolves to a reserved address: {hostname}"
    if ip.is_multicast:
        return f"Host resolves to a multicast address: {hostname}"
    if ip.is_unspecified:
        return f"Host resolves to an unspecified address: {hostname}"
    return None


def _check_reserved_ranges(hostname: str) -> str | None:
    """Check common private/internal IP ranges that ipaddress may miss.

    Note: ``ipaddress.ip_address().is_private`` already correctly handles
    172.16.0.0/12 (RFC1918).  The broad ``"172."`` prefix check is
    intentionally omitted — only 10.0.0.0/8 and 192.168.0.0/16 are checked
    here as fallback for edge cases where ``ipaddress`` may not classify
    them.
    """
    private_prefixes = (
        "10.",
        "192.168.",
        "169.254.",
    )
    for prefix in private_prefixes:
        if hostname.startswith(prefix):
            return f"Host is in a private IP range: {hostname}"
    # IPv6 private/link-local (fe80::/10, fc00::/7, ::1)
    if hostname.startswith("fe80:") or hostname.startswith("fc") or hostname.startswith("fd"):
        return f"Host is in an IPv6 private range: {hostname}"
    return None


def parse_network_policy() -> NetworkPolicy:
    """Parse network policy from environment variables."""
    mode_raw = os.environ.get("X402_NETWORK_POLICY", "strict_allowlist").strip().lower()
    if mode_raw not in {"strict_allowlist", "public"}:
        mode_raw = "strict_allowlist"

    allowlist_raw = os.environ.get("X402_HOST_ALLOWLIST", "")
    allowlist = tuple(item.strip() for item in allowlist_raw.split(",") if item.strip())

    allow_http_raw = os.environ.get("X402_ALLOW_HTTP", "").strip().lower()
    allow_http = allow_http_raw in {"1", "true", "yes"}

    return NetworkPolicy(
        mode=mode_raw,  # type: ignore[arg-type]
        host_allowlist=allowlist,
        allow_http=allow_http,
    )
