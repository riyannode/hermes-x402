"""Circle CLI marketplace adapter for service discovery."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from hermes_x402.circle_cli.errors import CircleCliError, CircleCliOutputError, CircleCliReadError
from hermes_x402.circle_cli.runner import CircleCliRunner
from hermes_x402.discovery.provider import DiscoveredService

_QUERY_MAX_LENGTH = 200
_LIMIT_MIN = 1
_LIMIT_MAX = 25
_RESULTS_MAX = 100
_PROVIDER_NAME = "circle-marketplace"


class CircleCliMarketplaceProvider:
    """Discover x402 services via ``circle services search``.

    This adapter shells out to the Circle CLI (already allowlisted in
    :class:`CircleCliRunner`) and normalises the JSON output into
    :class:`DiscoveredService` instances.

    The adapter performs **no payment** and makes **no network calls** beyond
    the subprocess execution of the CLI.
    """

    def __init__(self, runner: CircleCliRunner) -> None:
        self._runner = runner

    async def search(self, query: str, *, limit: int = 10) -> list[DiscoveredService]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        query = query.strip()[:_QUERY_MAX_LENGTH]
        if not (_LIMIT_MIN <= limit <= _LIMIT_MAX):
            raise ValueError(f"limit must be between {_LIMIT_MIN} and {_LIMIT_MAX}, got {limit}")

        args = (
            "services",
            "search",
            query,
            "--output",
            "json",
        )
        try:
            result = await self._runner.run_json(
                args,
                timeout_seconds=self._runner.read_timeout_seconds,
                operation="read",
            )
        except CircleCliError:
            raise
        except Exception as exc:
            raise CircleCliReadError("Circle CLI services search failed unexpectedly") from exc

        if result.exit_code != 0:
            raise CircleCliReadError(
                f"Circle CLI services search failed (exit code {result.exit_code})"
            )

        items = self._extract_items(result.parsed)
        services: list[DiscoveredService] = []
        for raw_item in items:
            service = self._normalise_item(raw_item)
            if service is not None:
                services.append(service)
        return services[:limit]

    @staticmethod
    def _extract_items(parsed: dict[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
        """Unwrap the CLI JSON envelope into a flat list of service dicts.

        Handles both ``{"data": {"items": [...]}}`` and ``{"items": [...]}``
        shapes.  Returns an empty list for ``None`` or ``[]``.
        """
        if parsed is None:
            return []

        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]

        if not isinstance(parsed, dict):
            raise CircleCliOutputError(
                "Circle CLI services search returned an unexpected JSON shape"
            )

        # Prefer {"data": {"items": [...]}} envelope.
        data = parsed.get("data")
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            raise CircleCliOutputError("Circle CLI services search data envelope is missing items")

        # Fall back to {"items": [...]} flat shape.
        items = parsed.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]

        raise CircleCliOutputError(
            "Circle CLI services search JSON does not contain a recognisable items list"
        )

    @staticmethod
    def _normalise_item(raw: dict[str, Any]) -> DiscoveredService | None:
        """Convert a single CLI JSON dict to a :class:`DiscoveredService`.

        Returns ``None`` when required fields are missing so the caller can
        skip incomplete entries instead of raising.
        """
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            return None

        url = raw.get("url") or raw.get("endpoint") or ""
        if not isinstance(url, str):
            url = ""

        description = raw.get("description", "")
        if not isinstance(description, str):
            description = ""

        price_raw = raw.get("price_usdc") or raw.get("price") or None
        if isinstance(price_raw, (int, float)):
            advertised_price_usdc = str(price_raw)
        elif isinstance(price_raw, str) and price_raw:
            advertised_price_usdc = price_raw
        else:
            advertised_price_usdc = None

        networks_raw = raw.get("networks") or raw.get("supported_networks") or ()
        if isinstance(networks_raw, (list, tuple)):
            advertised_networks = tuple(str(n) for n in networks_raw if isinstance(n, str) and n)
        else:
            advertised_networks = ()

        # Everything else goes into metadata.
        skip_keys = {
            "name",
            "url",
            "endpoint",
            "description",
            "price_usdc",
            "price",
            "networks",
            "supported_networks",
        }
        metadata: dict[str, Any] = {k: v for k, v in raw.items() if k not in skip_keys}

        return DiscoveredService(
            provider=_PROVIDER_NAME,
            name=name,
            description=description,
            url=url,
            advertised_price_usdc=advertised_price_usdc,
            advertised_networks=advertised_networks,
            metadata=metadata,
        )


_PUBLIC_PROVIDER_NAME = "public-marketplace"
_MARKETPLACE_BODY_LIMIT = 256_000
_MARKETPLACE_TIMEOUT_SECONDS = 10.0


class PublicMarketplaceProvider:
    """Read-only public marketplace discovery over a bounded HTTPS GET.

    The provider validates the marketplace URL and DNS destination before
    connecting. It never pays, persists trust, follows redirects, sends cookies,
    or automatically trusts discovered service hosts.
    """

    def __init__(
        self,
        *,
        marketplace_url: str,
        network_policy: str,
        discovery_host_allowlist: tuple[str, ...],
        allow_http: bool,
    ) -> None:
        self.marketplace_url = marketplace_url
        self.network_policy = network_policy
        self.discovery_host_allowlist = discovery_host_allowlist
        self.allow_http = allow_http

    async def search(self, query: str, *, limit: int = 10) -> list[DiscoveredService]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not (_LIMIT_MIN <= limit <= _LIMIT_MAX):
            raise ValueError(f"limit must be between {_LIMIT_MIN} and {_LIMIT_MAX}, got {limit}")

        from hermes_x402.dns_validator import resolve_and_validate_destination
        from hermes_x402.network_policy import NetworkPolicy

        policy = NetworkPolicy(
            mode=self.network_policy,  # type: ignore[arg-type]
            host_allowlist=self.discovery_host_allowlist,
            allow_http=self.allow_http,
        )
        policy.validate_url(self.marketplace_url)
        await resolve_and_validate_destination(self.marketplace_url)

        headers = {"accept": "application/json", "user-agent": "hermes-x402-discovery"}
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(_MARKETPLACE_TIMEOUT_SECONDS),
                follow_redirects=False,
                headers=headers,
            ) as client,
            client.stream("GET", self.marketplace_url) as response,
        ):
            if response.is_redirect:
                raise CircleCliReadError("Marketplace redirects are not followed")
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" not in content_type:
                raise CircleCliOutputError("Marketplace discovery response is not JSON")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > _MARKETPLACE_BODY_LIMIT:
                    raise CircleCliOutputError(
                        "Marketplace discovery response exceeds 256000 bytes"
                    )

        try:
            parsed = httpx.Response(200, content=bytes(raw)).json()
        except ValueError as exc:
            raise CircleCliOutputError("Marketplace discovery response is invalid JSON") from exc

        items = self._extract_items(parsed)
        services: list[DiscoveredService] = []
        for raw_item in items:
            service = self._normalise_item(raw_item)
            if service is not None:
                services.append(service)
        return services[:limit]

    @staticmethod
    def _extract_items(parsed: Any) -> list[dict[str, Any]]:
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if not isinstance(parsed, dict):
            raise CircleCliOutputError("Marketplace discovery returned an unexpected JSON shape")
        for key in ("items", "services", "results", "data"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if key == "data" and isinstance(value, dict):
                for nested in ("items", "services", "results"):
                    nested_value = value.get(nested)
                    if isinstance(nested_value, list):
                        return [item for item in nested_value if isinstance(item, dict)]
        raise CircleCliOutputError("Marketplace discovery JSON does not contain a service list")

    @staticmethod
    def _first_string(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @classmethod
    def _normalise_item(cls, raw: dict[str, Any]) -> DiscoveredService | None:
        url = cls._first_string(raw, ("url", "endpoint", "resource"))
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return None

        name = cls._first_string(raw, ("name", "title", "id")) or url
        description = cls._first_string(raw, ("description", "summary"))
        price = raw.get("price_usdc") or raw.get("price") or raw.get("amount")
        advertised_price_usdc = str(price) if isinstance(price, (int, float, str)) else None
        networks_raw = raw.get("networks") or raw.get("supported_networks") or raw.get("chains")
        if isinstance(networks_raw, (list, tuple)):
            advertised_networks = tuple(str(n) for n in networks_raw if isinstance(n, str) and n)
        else:
            advertised_networks = ()

        return DiscoveredService(
            provider=_PUBLIC_PROVIDER_NAME,
            name=name,
            description=description,
            url=url,
            advertised_price_usdc=advertised_price_usdc,
            advertised_networks=advertised_networks,
            metadata={
                k: v
                for k, v in raw.items()
                if k
                not in {
                    "name",
                    "title",
                    "id",
                    "url",
                    "endpoint",
                    "resource",
                    "description",
                    "summary",
                    "price_usdc",
                    "price",
                    "amount",
                    "networks",
                    "supported_networks",
                    "chains",
                }
            },
        )
