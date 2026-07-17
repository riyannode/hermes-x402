"""Service discovery protocol and shared models."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class DiscoveredService:
    """An x402 service discovered from a marketplace or registry."""

    provider: str
    name: str
    description: str
    url: str
    advertised_price_usdc: str | None
    advertised_networks: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ServiceDiscoveryProvider(Protocol):
    """Protocol for pluggable service discovery backends."""

    async def search(self, query: str, *, limit: int = 10) -> list[DiscoveredService]:
        """Search for services matching *query*."""
        ...  # pragma: no cover


def parse_discovery_providers() -> tuple[str, ...]:
    """Read ``X402_DISCOVERY_PROVIDERS`` and return a bounded tuple of names.

    The environment variable is a comma-separated list of provider identifiers
    (e.g. ``"circle-marketplace"``).  Empty entries are silently dropped.
    An absent or empty variable returns an empty tuple.
    """
    raw = os.environ.get("X402_DISCOVERY_PROVIDERS", "")
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name:
            names.append(name)
    return tuple(names)


def parse_discovery_host_allowlist() -> tuple[str, ...]:
    """Read ``X402_DISCOVERY_HOST_ALLOWLIST`` and return a bounded tuple of hosts.

    The environment variable is a comma-separated list of hostnames.
    Empty entries are silently dropped.  An absent or empty variable returns
    an empty tuple.
    """
    raw = os.environ.get("X402_DISCOVERY_HOST_ALLOWLIST", "")
    hosts: list[str] = []
    for item in raw.split(","):
        host = item.strip()
        if host:
            hosts.append(host)
    return tuple(hosts)
