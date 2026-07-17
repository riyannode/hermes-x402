"""Tests for the service discovery module."""

from __future__ import annotations

from typing import Any

import pytest

from hermes_x402.circle_cli.errors import CircleCliOutputError, CircleCliReadError
from hermes_x402.circle_cli.models import CircleCliResult
from hermes_x402.discovery.circle_marketplace import (
    _LIMIT_MAX,
    _QUERY_MAX_LENGTH,
    CircleCliMarketplaceProvider,
)
from hermes_x402.discovery.provider import (
    DiscoveredService,
    ServiceDiscoveryProvider,
    parse_discovery_host_allowlist,
    parse_discovery_providers,
)

# ---------------------------------------------------------------------------
# DiscoveredService
# ---------------------------------------------------------------------------


class TestDiscoveredService:
    def test_frozen(self):
        svc = DiscoveredService(
            provider="p",
            name="n",
            description="d",
            url="https://x.com",
            advertised_price_usdc="1.0",
            advertised_networks=("a",),
            metadata={"k": 1},
        )
        with pytest.raises(AttributeError):
            svc.name = "nope"

    def test_defaults(self):
        svc = DiscoveredService(
            provider="p",
            name="n",
            description="",
            url="",
            advertised_price_usdc=None,
        )
        assert svc.advertised_networks == ()
        assert svc.metadata == {}

    def test_protocol_runtime_check(self):
        assert isinstance(CircleCliMarketplaceProvider, type)
        # runtime_checkable only works on instances
        runner = _FakeRunner()
        assert isinstance(CircleCliMarketplaceProvider(runner), ServiceDiscoveryProvider)


# ---------------------------------------------------------------------------
# Env var parsing
# ---------------------------------------------------------------------------


class TestParseDiscoveryProviders:
    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("X402_DISCOVERY_PROVIDERS", "a, b , c")
        assert parse_discovery_providers() == ("a", "b", "c")

    def test_empty(self, monkeypatch):
        monkeypatch.setenv("X402_DISCOVERY_PROVIDERS", "")
        assert parse_discovery_providers() == ()

    def test_absent(self, monkeypatch):
        monkeypatch.delenv("X402_DISCOVERY_PROVIDERS", raising=False)
        assert parse_discovery_providers() == ()


class TestParseDiscoveryHostAllowlist:
    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("X402_DISCOVERY_HOST_ALLOWLIST", "a.com, b.com ")
        assert parse_discovery_host_allowlist() == ("a.com", "b.com")

    def test_empty(self, monkeypatch):
        monkeypatch.setenv("X402_DISCOVERY_HOST_ALLOWLIST", "")
        assert parse_discovery_host_allowlist() == ()

    def test_absent(self, monkeypatch):
        monkeypatch.delenv("X402_DISCOVERY_HOST_ALLOWLIST", raising=False)
        assert parse_discovery_host_allowlist() == ()


# ---------------------------------------------------------------------------
# CircleCliMarketplaceProvider — runner fake
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Minimal runner stub matching the CircleCliRunner interface."""

    read_timeout_seconds: float = 30

    def __init__(
        self,
        parsed: dict[str, Any] | list[Any] | None = None,
        *,
        exit_code: int = 0,
    ):
        self.parsed = parsed
        self.exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []

    async def run_json(self, args, **_: Any):
        self.calls.append(tuple(args))
        return CircleCliResult(
            argv=tuple(args),
            exit_code=self.exit_code,
            stdout="{}",
            stderr="",
            parsed=self.parsed,
        )


# ---------------------------------------------------------------------------
# CircleCliMarketplaceProvider — search basics
# ---------------------------------------------------------------------------


class TestCircleCliMarketplaceProviderSearch:
    async def test_data_envelope(self):
        runner = _FakeRunner(
            {
                "data": {
                    "items": [
                        {"name": "S1", "url": "https://s1.example", "description": "one"},
                    ]
                }
            }
        )
        svc = CircleCliMarketplaceProvider(runner)
        results = await svc.search("weather", limit=5)
        assert len(results) == 1
        assert results[0].provider == "circle-marketplace"
        assert results[0].name == "S1"
        assert results[0].url == "https://s1.example"
        assert runner.calls[0] == (
            "services",
            "search",
            "weather",
            "--output",
            "json",
        )

    async def test_flat_envelope(self):
        runner = _FakeRunner({"items": [{"name": "F1", "url": "https://f.example"}]})
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert len(results) == 1
        assert results[0].name == "F1"

    async def test_list_envelope(self):
        runner = _FakeRunner([{"name": "L1", "url": "https://l.example"}])
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert len(results) == 1
        assert results[0].name == "L1"

    async def test_empty_results(self):
        runner = _FakeRunner({"data": {"items": []}})
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results == []

    async def test_none_parsed(self):
        runner = _FakeRunner(None)
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results == []

    async def test_limit_truncates(self):
        items = [{"name": f"S{i}", "url": f"https://s{i}.example"} for i in range(10)]
        runner = _FakeRunner({"data": {"items": items}})
        results = await CircleCliMarketplaceProvider(runner).search("q", limit=3)
        assert len(results) == 3

    async def test_query_trimming(self):
        runner = _FakeRunner({"data": {"items": []}})
        await CircleCliMarketplaceProvider(runner).search("a" * 300, limit=10)
        assert runner.calls[0][2] == "a" * _QUERY_MAX_LENGTH

    async def test_query_stripped(self):
        runner = _FakeRunner({"data": {"items": []}})
        await CircleCliMarketplaceProvider(runner).search("  hello  ", limit=10)
        assert runner.calls[0][2] == "hello"


# ---------------------------------------------------------------------------
# CircleCliMarketplaceProvider — validation
# ---------------------------------------------------------------------------


class TestCircleCliMarketplaceProviderValidation:
    async def test_empty_query_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            await CircleCliMarketplaceProvider(_FakeRunner()).search("  ")

    async def test_limit_zero_raises(self):
        with pytest.raises(ValueError, match="limit"):
            await CircleCliMarketplaceProvider(_FakeRunner()).search("q", limit=0)

    async def test_limit_too_high_raises(self):
        with pytest.raises(ValueError, match="limit"):
            await CircleCliMarketplaceProvider(_FakeRunner()).search("q", limit=_LIMIT_MAX + 1)


# ---------------------------------------------------------------------------
# CircleCliMarketplaceProvider — errors
# ---------------------------------------------------------------------------


class TestCircleCliMarketplaceProviderErrors:
    async def test_non_zero_exit_raises_read_error(self):
        runner = _FakeRunner(None, exit_code=1)
        with pytest.raises(CircleCliReadError, match="exit code 1"):
            await CircleCliMarketplaceProvider(runner).search("q")

    async def test_string_parsed_raises_output_error(self):
        runner = _FakeRunner("just a string")
        with pytest.raises(CircleCliOutputError, match="unexpected JSON shape"):
            await CircleCliMarketplaceProvider(runner).search("q")

    async def test_dict_without_items_raises_output_error(self):
        runner = _FakeRunner({"something": "else"})
        with pytest.raises(CircleCliOutputError, match="recognisable items"):
            await CircleCliMarketplaceProvider(runner).search("q")

    async def test_data_envelope_missing_items_raises_output_error(self):
        runner = _FakeRunner({"data": {"something": "else"}})
        with pytest.raises(CircleCliOutputError, match="missing items"):
            await CircleCliMarketplaceProvider(runner).search("q")


# ---------------------------------------------------------------------------
# CircleCliMarketplaceProvider — normalise
# ---------------------------------------------------------------------------


class TestCircleCliMarketplaceProviderNormalise:
    async def test_missing_name_skipped(self):
        runner = _FakeRunner({"data": {"items": [{"url": "https://x.com"}]}})
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results == []

    async def test_price_numeric(self):
        runner = _FakeRunner(
            {"data": {"items": [{"name": "N", "url": "https://x.com", "price_usdc": 0.05}]}}
        )
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results[0].advertised_price_usdc == "0.05"

    async def test_price_string(self):
        runner = _FakeRunner(
            {"data": {"items": [{"name": "N", "url": "https://x.com", "price_usdc": "1.5"}]}}
        )
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results[0].advertised_price_usdc == "1.5"

    async def test_networks_from_list(self):
        runner = _FakeRunner(
            {"data": {"items": [{"name": "N", "url": "https://x.com", "networks": ["a", "b"]}]}}
        )
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results[0].advertised_networks == ("a", "b")

    async def test_supported_networks_fallback(self):
        runner = _FakeRunner(
            {
                "data": {
                    "items": [
                        {
                            "name": "N",
                            "url": "https://x.com",
                            "supported_networks": ["x"],
                        }
                    ]
                }
            }
        )
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results[0].advertised_networks == ("x",)

    async def test_metadata_extra_keys(self):
        runner = _FakeRunner(
            {
                "data": {
                    "items": [
                        {
                            "name": "N",
                            "url": "https://x.com",
                            "version": "2.0",
                            "author": "acme",
                        }
                    ]
                }
            }
        )
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results[0].metadata == {"version": "2.0", "author": "acme"}

    async def test_endpoint_fallback_for_url(self):
        runner = _FakeRunner({"data": {"items": [{"name": "N", "endpoint": "https://e.example"}]}})
        results = await CircleCliMarketplaceProvider(runner).search("q")
        assert results[0].url == "https://e.example"


# ---------------------------------------------------------------------------
# Discovery — CLI argument exactness (no shell)
# ---------------------------------------------------------------------------


class TestDiscoveryCliArgs:
    async def test_cli_args_are_tuple_not_string(self):
        runner = _FakeRunner({"data": {"items": []}})
        await CircleCliMarketplaceProvider(runner).search("weather", limit=5)
        args = runner.calls[0]
        assert isinstance(args, tuple)
        assert args == ("services", "search", "weather", "--output", "json")


# ---------------------------------------------------------------------------
# Discovery — timeout enforced
# ---------------------------------------------------------------------------


class TestDiscoveryTimeout:
    async def test_timeout_propagated_from_runner(self):
        runner = _FakeRunner({"data": {"items": []}})
        runner.read_timeout_seconds = 15.0
        svc = CircleCliMarketplaceProvider(runner)
        await svc.search("q")
        # The provider passes timeout_seconds to run_json
        assert runner.calls[0] == ("services", "search", "q", "--output", "json")


# ---------------------------------------------------------------------------
# Discovery — result limit (1-25) enforced
# ---------------------------------------------------------------------------


class TestDiscoveryResultLimit:
    async def test_limit_min_boundary(self):
        runner = _FakeRunner({"data": {"items": []}})
        # limit=1 is valid
        await CircleCliMarketplaceProvider(runner).search("q", limit=1)

    async def test_limit_max_boundary(self):
        runner = _FakeRunner({"data": {"items": []}})
        # limit=25 is valid
        await CircleCliMarketplaceProvider(runner).search("q", limit=25)

    async def test_limit_zero_rejected(self):
        with pytest.raises(ValueError, match="limit"):
            await CircleCliMarketplaceProvider(_FakeRunner()).search("q", limit=0)

    async def test_limit_26_rejected(self):
        with pytest.raises(ValueError, match="limit"):
            await CircleCliMarketplaceProvider(_FakeRunner()).search("q", limit=26)


# ---------------------------------------------------------------------------
# Discovery — no payment during search
# ---------------------------------------------------------------------------


class TestDiscoveryNoPayment:
    async def test_search_never_triggers_settle(self):
        """search() only calls run_json; it never calls any settlement endpoint."""
        runner = _FakeRunner({"data": {"items": []}})
        runner.settle_called = False
        original_run_json = runner.run_json

        async def tracked_run_json(*a, **kw):
            return await original_run_json(*a, **kw)

        runner.run_json = tracked_run_json
        svc = CircleCliMarketplaceProvider(runner)
        results = await svc.search("test")
        assert results == []
        # Only one call — the search
        assert len(runner.calls) == 1
        assert runner.calls[0][0] == "services"


# ---------------------------------------------------------------------------
# Discovery — no discovered URL automatically trusted
# ---------------------------------------------------------------------------


class TestDiscoveryNoAutoTrust:
    async def test_discovered_url_not_in_trusted_store(self):
        from hermes_x402.buyer.approval import _get_store

        runner = _FakeRunner(
            {"data": {"items": [{"name": "S1", "url": "https://discovered.example.com"}]}}
        )
        svc = CircleCliMarketplaceProvider(runner)
        results = await svc.search("test")
        assert len(results) == 1

        store = _get_store()
        assert not store.is_trusted("discovered.example.com")
