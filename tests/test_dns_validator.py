"""Tests for hermes_x402.dns_validator."""

from __future__ import annotations

import asyncio
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


# ── DNS retry / classification regressions ───────────────────────────────


class _SequenceResolver:
    def __init__(self, outcomes: list[object], family: int = socket.AF_INET):
        self.outcomes = list(outcomes)
        self.family = family
        self.calls = 0

    async def resolve(self, host: str, family: int = 0) -> list[tuple[int, str]]:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return [(self.family, ip) for ip in outcome]


@pytest.mark.asyncio
async def test_first_timeout_second_attempt_success(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_RESOLUTION_ATTEMPT_TIMEOUT", 0.01)
    monkeypatch.setattr(mod, "_RESOLUTION_BACKOFFS", (0, 0))
    resolver = _SequenceResolver([asyncio.TimeoutError(), ["93.184.216.34"]])
    result = await mod.resolve_and_validate_destination("https://example.com/", resolver=resolver)
    assert result == ("93.184.216.34",)
    assert resolver.calls == 2


@pytest.mark.asyncio
async def test_two_timeouts_third_attempt_success(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_RESOLUTION_ATTEMPT_TIMEOUT", 0.01)
    monkeypatch.setattr(mod, "_RESOLUTION_BACKOFFS", (0, 0))
    resolver = _SequenceResolver(
        [
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            ["93.184.216.34"],
        ]
    )
    result = await mod.resolve_and_validate_destination("https://example.com/", resolver=resolver)
    assert result == ("93.184.216.34",)
    assert resolver.calls == 3


@pytest.mark.asyncio
async def test_all_attempts_timeout_has_retry_safe_true(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_RESOLUTION_ATTEMPT_TIMEOUT", 0.01)
    monkeypatch.setattr(mod, "_RESOLUTION_BACKOFFS", (0, 0))
    resolver = _SequenceResolver(
        [
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ]
    )
    with pytest.raises(mod.DnsValidationError) as raised:
        await mod.resolve_and_validate_destination("https://example.com/", resolver=resolver)
    exc = raised.value
    assert exc.error_code == "dns_resolution_timeout"
    assert exc.retry_safe is True
    assert exc.attempts == 3
    assert "3 attempts" in str(exc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "expected_error"),
    [
        (
            [
                asyncio.TimeoutError(),
                socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
                socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
            ],
            "dns_resolution_failed",
        ),
        (
            [
                socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
                asyncio.TimeoutError(),
                asyncio.TimeoutError(),
            ],
            "dns_resolution_timeout",
        ),
        (
            [
                asyncio.TimeoutError(),
                asyncio.TimeoutError(),
                asyncio.TimeoutError(),
            ],
            "dns_resolution_timeout",
        ),
    ],
)
async def test_final_dns_failure_kind_controls_mixed_sequence_classification(
    monkeypatch, outcomes, expected_error
) -> None:
    monkeypatch.setattr(mod, "_RESOLUTION_ATTEMPT_TIMEOUT", 0.01)
    monkeypatch.setattr(mod, "_RESOLUTION_BACKOFFS", (0, 0))
    resolver = _SequenceResolver(outcomes)
    with pytest.raises(mod.DnsValidationError) as raised:
        await mod.resolve_and_validate_destination("https://example.com/", resolver=resolver)
    assert raised.value.error_code == expected_error
    assert raised.value.attempts == 3


@pytest.mark.asyncio
async def test_permanent_nxdomain_gaierror_classification() -> None:
    resolver = _SequenceResolver([socket.gaierror(socket.EAI_NONAME, "Name or service not known")])
    with pytest.raises(mod.DnsValidationError) as raised:
        await mod.resolve_and_validate_destination("https://missing.example/", resolver=resolver)
    exc = raised.value
    assert exc.error_code == "dns_resolution_failed"
    assert exc.retry_safe is True
    assert exc.attempts == 1
    assert resolver.calls == 1


@pytest.mark.asyncio
async def test_temporary_gaierror_retries_then_success(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_RESOLUTION_BACKOFFS", (0, 0))
    resolver = _SequenceResolver(
        [
            socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
            ["93.184.216.34"],
        ]
    )
    result = await mod.resolve_and_validate_destination("https://example.com/", resolver=resolver)
    assert result == ("93.184.216.34",)
    assert resolver.calls == 2


@pytest.mark.asyncio
async def test_duplicate_getaddrinfo_results_deduplicated() -> None:
    resolver = _SequenceResolver(
        [
            [
                "43.156.11.37",
                "43.156.11.37",
                "43.156.11.37",
            ]
        ]
    )
    result = await mod.resolve_and_validate_destination(
        "https://api.flowvidence.my.id/", resolver=resolver
    )
    assert result == ("43.156.11.37",)


@pytest.mark.asyncio
async def test_public_ipv4_success() -> None:
    result = await mod.resolve_and_validate_destination(
        "https://public.example/", resolver=_mock_resolver(["93.184.216.34"])
    )
    assert result == ("93.184.216.34",)


@pytest.mark.asyncio
async def test_private_reserved_ip_rejected_without_retry() -> None:
    resolver = _SequenceResolver([["10.0.0.1"], ["93.184.216.34"]])
    with pytest.raises(mod.DnsValidationError) as raised:
        await mod.resolve_and_validate_destination("https://private.example/", resolver=resolver)
    exc = raised.value
    assert exc.error_code == "destination_ip_rejected"
    assert exc.retry_safe is False
    assert resolver.calls == 1


@pytest.mark.asyncio
async def test_mixed_public_private_results_rejected_without_retry() -> None:
    resolver = _SequenceResolver([["93.184.216.34", "192.168.1.1"], ["93.184.216.34"]])
    with pytest.raises(mod.DnsValidationError) as raised:
        await mod.resolve_and_validate_destination("https://mixed.example/", resolver=resolver)
    exc = raised.value
    assert exc.error_code == "destination_ip_rejected"
    assert exc.retry_safe is False
    assert resolver.calls == 1


@pytest.mark.asyncio
async def test_pay_handler_dns_failure_does_not_call_backend(monkeypatch) -> None:
    from types import SimpleNamespace

    from hermes_x402.dns_validator import DnsValidationError
    from hermes_x402.hermes_plugin import tools as plugin_tools

    class _Ctx:
        def __init__(self) -> None:
            self.tools = {}

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs["handler"]

    buyer = SimpleNamespace(pay=AsyncMock())
    runtime = SimpleNamespace(
        ensure_initialized=lambda: None,
        is_available=True,
        config=SimpleNamespace(
            network_policy="public",
            host_allowlist=(),
            allow_http=False,
            max_usdc_per_payment="0.003",
            require_approval_for_new_host=False,
        ),
        buyer_tool=buyer,
    )
    monkeypatch.setattr(plugin_tools, "get_runtime", lambda: runtime)

    async def _fail_dns(url: str):
        raise DnsValidationError(
            "DNS resolution for api.flowvidence.my.id timed out after 3 attempts in 1000ms.",
            error_code="dns_resolution_timeout",
            retry_safe=True,
            attempts=3,
            elapsed_ms=1000,
        )

    monkeypatch.setattr("hermes_x402.dns_validator.resolve_and_validate_destination", _fail_dns)
    ctx = _Ctx()
    plugin_tools.register_payment_tools(ctx)
    result = await ctx.tools["x402_pay"](
        {
            "url": "https://api.flowvidence.my.id/v1/intelligence/addresses",
            "method": "POST",
            "body": {
                "addresses": ["0x3c46624b62fa4cf3d63e6bdd60dc1b79a43ceb22"],
                "network": "arcTestnet",
            },
            "max_usdc": "0.000250",
        }
    )
    assert '"error": "dns_resolution_timeout"' in result
    assert '"retry_safe": true' in result
    buyer.pay.assert_not_awaited()
