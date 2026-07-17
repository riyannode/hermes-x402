"""Circle CLI marketplace adapter for service discovery."""

from __future__ import annotations

from typing import Any

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
