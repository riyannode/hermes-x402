"""Tests for hermes_x402.dns_validator."""

from __future__ import annotations

import importlib.util
import socket
from unittest.mock import AsyncMock

import pytest

spec = importlib.util.spec_from_file_location("dns_validator", "hermes_x402/dns_validator.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# ── is_ip_forbidden ────────────────────────────────────────────────────

FORBIDDEN_IPS = [
    "127.0.0.1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "169.254.0.1",
    "100.100.100.200",
    "224.0.0.1",
    "0.0.0.0",
    "::1",
    "fe80::1",
    "fc00::1",
    "not-an-ip",
]

ALLOWED_IPS = [
    "8.8.8.8",
    "1.1.1.1",
    "9.9.9.9",
    "2001:4860:4860::8888",
    "2606:4700:4700::1111",
]


@pytest.mark.parametrize("ip", FORBIDDEN_IPS, ids=lambda i: f"forbid-{i}")
def test_is_ip_forbidden_rejects(ip: str) -> None:
    assert mod.is_ip_forbidden(ip) is True


@pytest.mark.parametrize("ip", ALLOWED_IPS, ids=lambda i: f"allow-{i}")
def test_is_ip_forbidden_allows(ip: str) -> None:
    assert mod.is_ip_forbidden(ip) is False


# ── IDNA normalisation ─────────────────────────────────────────────────


def test_idna_lowercase() -> None:
    assert mod._normalise_idna("EXAMPLE.COM") == "example.com"


def test_idna_punycode() -> None:
    assert mod._normalise_idna("münchen.de") == "xn--mnchen-3ya.de"


def test_idna_passthrough() -> None:
    assert mod._normalise_idna("example.com") == "example.com"


# ── safe_host ──────────────────────────────────────────────────────────


def test_safe_host_strips_angle_brackets() -> None:
    assert mod._safe_host("<script>") == "script"


def test_safe_host_empty_fallback() -> None:
    assert mod._safe_host("") == "<invalid>"


# ── resolve_and_validate_destination (async, mock resolver) ───────────


def _mock_resolver(ips: list[str], family: int = socket.AF_INET) -> AsyncMock:
    r = AsyncMock()
    r.resolve.return_value = [(family, ip) for ip in ips]
    return r


@pytest.mark.asyncio
async def test_empty_url_rejected() -> None:
    with pytest.raises(ValueError, match="required"):
        await mod.resolve_and_validate_destination("")


@pytest.mark.asyncio
async def test_no_hostname_rejected() -> None:
    with pytest.raises(ValueError, match="hostname"):
        await mod.resolve_and_validate_destination("not-a-url")


@pytest.mark.asyncio
async def test_zero_addresses_rejected() -> None:
    with pytest.raises(ValueError, match="no addresses"):
        await mod.resolve_and_validate_destination(
            "https://example.com/", resolver=_mock_resolver([])
        )


@pytest.mark.asyncio
async def test_forbidden_ip_rejected() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        await mod.resolve_and_validate_destination(
            "https://evil.example.com/", resolver=_mock_resolver(["10.0.0.1"])
        )


@pytest.mark.asyncio
async def test_metadata_ip_rejected() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        await mod.resolve_and_validate_destination(
            "https://metadata.example.com/",
            resolver=_mock_resolver(["169.254.169.254"]),
        )


@pytest.mark.asyncio
async def test_aliyun_metadata_rejected() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        await mod.resolve_and_validate_destination(
            "https://aliyun.example.com/",
            resolver=_mock_resolver(["100.100.100.200"]),
        )


@pytest.mark.asyncio
async def test_valid_public_ip_accepted() -> None:
    result = await mod.resolve_and_validate_destination(
        "https://example.com/path",
        resolver=_mock_resolver(["93.184.216.34"]),
    )
    assert result == ("93.184.216.34",)


@pytest.mark.asyncio
async def test_mixed_valid_and_forbidden_rejected() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        await mod.resolve_and_validate_destination(
            "https://example.com/",
            resolver=_mock_resolver(["93.184.216.34", "192.168.1.1"]),
        )


@pytest.mark.asyncio
async def test_max_records_bound() -> None:
    ips = [f"93.184.{i}.{i}" for i in range(15)]
    result = await mod.resolve_and_validate_destination(
        "https://example.com/", resolver=_mock_resolver(ips)
    )
    assert len(result) == 10
