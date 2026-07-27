"""Public HTTP marketplace adapter for x402 service discovery.

This provider queries a caller-supplied public HTTPS marketplace endpoint and
normalises common JSON result shapes into :class:`DiscoveredService` instances.
It is read-only and never signs, settles, deposits, or pays.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from hermes_x402.discovery.circle_marketplace import _LIMIT_MAX, _LIMIT_MIN, _QUERY_MAX_LENGTH
from hermes_x402.discovery.provider import DiscoveredService
from hermes_x402.network_policy import validate_url_strict

_PROVIDER_NAME = "public-marketplace"
_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 256_000


class PublicMarketplaceDiscoveryError(RuntimeError):
    """Raised when a public marketplace cannot be queried or parsed safely."""


class PublicHttpMarketplaceProvider:
    """Discover x402 services from a public HTTP JSON marketplace endpoint.

    The endpoint is supplied by the caller.  If it contains ``{query}`` or
    ``{limit}`` placeholders they are expanded safely.  Otherwise the provider
    appends ``query`` and ``limit`` query parameters.
    """

    def __init__(
        self,
        marketplace_url: str,
        *,
        allow_http: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        err = validate_url_strict(marketplace_url, (), "public", allow_http)
        if err:
            raise ValueError(err)
        self._marketplace_url = marketplace_url
        self._allow_http = allow_http
        self._client = client

    async def search(self, query: str, *, limit: int = 10) -> list[DiscoveredService]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        query = query.strip()[:_QUERY_MAX_LENGTH]
        if not (_LIMIT_MIN <= limit <= _LIMIT_MAX):
            raise ValueError(f"limit must be between {_LIMIT_MIN} and {_LIMIT_MAX}, got {limit}")

        url = self._build_search_url(query, limit)
        err = validate_url_strict(url, (), "public", self._allow_http)
        if err:
            raise ValueError(err)

        parsed = await self._fetch_json(url)
        items = self._extract_items(parsed)
        services: list[DiscoveredService] = []
        for raw_item in items:
            service = self._normalise_item(raw_item)
            if service is not None:
                services.append(service)
        return services[:limit]

    def _build_search_url(self, query: str, limit: int) -> str:
        if "{query}" in self._marketplace_url or "{limit}" in self._marketplace_url:
            return self._marketplace_url.replace("{query}", quote_component(query)).replace(
                "{limit}", str(limit)
            )

        parts = urlsplit(self._marketplace_url)
        existing = parts.query
        extra = urlencode({"query": query, "limit": str(limit)})
        query_string = f"{existing}&{extra}" if existing else extra
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query_string, parts.fragment))

    async def _fetch_json(self, url: str) -> Any:
        headers = {"accept": "application/json"}
        if self._client is not None:
            response = await self._client.get(url, headers=headers, follow_redirects=False)
            return _decode_response(response)

        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers, follow_redirects=False)
            return _decode_response(response)

    @staticmethod
    def _extract_items(parsed: Any) -> list[dict[str, Any]]:
        if parsed is None:
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if not isinstance(parsed, dict):
            raise PublicMarketplaceDiscoveryError("marketplace returned an unexpected JSON shape")

        for key in ("items", "services", "results"):
            items = parsed.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]

        data = parsed.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("items", "services", "results"):
                items = data.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]

        raise PublicMarketplaceDiscoveryError(
            "marketplace JSON does not contain items, services, or results"
        )

    @staticmethod
    def _normalise_item(raw: Mapping[str, Any]) -> DiscoveredService | None:
        name_raw = raw.get("name") or raw.get("title") or raw.get("id")
        url_raw = raw.get("url") or raw.get("endpoint") or raw.get("resource")
        if not isinstance(url_raw, str) or not url_raw:
            return None
        if not isinstance(name_raw, str) or not name_raw:
            name_raw = url_raw

        description = raw.get("description") or raw.get("summary") or ""
        if not isinstance(description, str):
            description = ""

        price_raw = raw.get("price_usdc") or raw.get("price") or raw.get("amount")
        if isinstance(price_raw, (int, float)):
            advertised_price_usdc = str(price_raw)
        elif isinstance(price_raw, str) and price_raw:
            advertised_price_usdc = price_raw
        else:
            advertised_price_usdc = None

        networks_raw = (
            raw.get("networks")
            or raw.get("supported_networks")
            or raw.get("chains")
            or ()
        )
        if isinstance(networks_raw, (list, tuple)):
            advertised_networks = tuple(str(n) for n in networks_raw if isinstance(n, str) and n)
        elif isinstance(networks_raw, str) and networks_raw:
            advertised_networks = (networks_raw,)
        else:
            advertised_networks = ()

        skip_keys = {
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
        metadata = {str(k): v for k, v in raw.items() if k not in skip_keys}

        return DiscoveredService(
            provider=_PROVIDER_NAME,
            name=name_raw,
            description=description,
            url=url_raw,
            advertised_price_usdc=advertised_price_usdc,
            advertised_networks=advertised_networks,
            metadata=metadata,
        )


def quote_component(value: str) -> str:
    """URL-encode a template component for marketplace URL placeholders."""
    return urlencode({"q": value})[2:]


def _decode_response(response: httpx.Response) -> Any:
    if response.is_redirect:
        raise PublicMarketplaceDiscoveryError("marketplace redirects are not followed")
    if response.status_code >= 400:
        raise PublicMarketplaceDiscoveryError(
            f"marketplace request failed with HTTP {response.status_code}"
        )
    content = response.content
    if len(content) > _MAX_RESPONSE_BYTES:
        raise PublicMarketplaceDiscoveryError("marketplace response exceeds maximum size")
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise PublicMarketplaceDiscoveryError("marketplace response must be JSON")
    try:
        return response.json()
    except ValueError as exc:
        raise PublicMarketplaceDiscoveryError("marketplace response is invalid JSON") from exc
