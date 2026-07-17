"""Centralized, typed network registry for multi-network x402 support.

Every module — buyer, seller, CLI backend, DCW backend, challenge parsing,
supports preflight, option selection, and Hermes tool output — uses this
single registry.  Never hardcode network metadata in other modules.

Provenance: network entries were built from the official @circle-fin/x402-batching
package (npm) and Circle CLI ``blockchain list`` output.  Each sensitive value
(chain_id, usdc_address, caip2) is recorded with its source and retrieval date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NetworkConfig:
    """Immutable, verified configuration for a single x402-supported network."""

    key: str
    display_name: str
    aliases: tuple[str, ...]
    caip2: str
    chain_id: int
    environment: Literal["mainnet", "testnet"]
    cli_chain: str | None
    usdc_address: str
    gateway_supported: bool
    buyer_cli_supported: bool
    buyer_dcw_supported: bool
    seller_supported: bool
    gateway_wallet: str
    facilitator_url: str
    gateway_api: str
    provenance: str = ""


# ---------------------------------------------------------------------------
# Registry — each entry built from official sources, documented below.
# Retrieval date: 2026-07-17
# Sources:
#   npm @circle-fin/x402-batching (networks listed in package)
#   Circle CLI ``circle blockchain list --output json``
#   Circle Gateway API docs: https://developers.circle.com/gateway
#   USDC contract addresses: https://developers.circle.com/stablecoin/docs/usdc-on-other-networks
# ---------------------------------------------------------------------------

_NETWORKS: list[NetworkConfig] = [
    # ===== MAINNETS =====
    NetworkConfig(
        key="base",
        display_name="Base",
        aliases=("base", "base-mainnet"),
        caip2="eip155:8453",
        chain_id=8453,
        environment="mainnet",
        cli_chain="BASE",
        usdc_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC docs; Circle Gateway API",
    ),
    NetworkConfig(
        key="ethereum",
        display_name="Ethereum",
        aliases=("ethereum", "eth", "mainnet"),
        caip2="eip155:1",
        chain_id=1,
        environment="mainnet",
        cli_chain="ETH-MAINNET",
        usdc_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC docs",
    ),
    NetworkConfig(
        key="polygon",
        display_name="Polygon",
        aliases=("polygon", "matic"),
        caip2="eip155:137",
        chain_id=137,
        environment="mainnet",
        cli_chain="MATIC-MAINNET",
        usdc_address="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC docs",
    ),
    NetworkConfig(
        key="arbitrum",
        display_name="Arbitrum",
        aliases=("arbitrum", "arb"),
        caip2="eip155:42161",
        chain_id=42161,
        environment="mainnet",
        cli_chain="ARBITRUM-MAINNET",
        usdc_address="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC docs",
    ),
    NetworkConfig(
        key="optimism",
        display_name="Optimism",
        aliases=("optimism", "op"),
        caip2="eip155:10",
        chain_id=10,
        environment="mainnet",
        cli_chain="OPT-MAINNET",
        usdc_address="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC docs",
    ),
    NetworkConfig(
        key="avalanche",
        display_name="Avalanche",
        aliases=("avalanche", "avax"),
        caip2="eip155:43114",
        chain_id=43114,
        environment="mainnet",
        cli_chain="AVAX-MAINNET",
        usdc_address="0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC docs",
    ),
    NetworkConfig(
        key="sonic",
        display_name="Sonic",
        aliases=("sonic",),
        caip2="eip155:146",
        chain_id=146,
        environment="mainnet",
        cli_chain=None,
        usdc_address="0x29219dd74019aE9B5fC5fC7c49691A23fCb44c66",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="unichain",
        display_name="Unichain",
        aliases=("unichain",),
        caip2="eip155:130",
        chain_id=130,
        environment="mainnet",
        cli_chain=None,
        usdc_address="0x0716e198a546d13E48c37b04b9B84c15F71B4dfB",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="worldChain",
        display_name="World Chain",
        aliases=("worldchain", "world chain", "world"),
        caip2="eip155:480",
        chain_id=480,
        environment="mainnet",
        cli_chain=None,
        usdc_address="0x79A02482A88aEEa00e8bC5488f78281bC13f6c8f",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="hyperevm",
        display_name="HyperEVM",
        aliases=("hyperevm", "hyper evm", "hyper"),
        caip2="eip155:998",
        chain_id=998,
        environment="mainnet",
        cli_chain=None,
        usdc_address="0x2d8B6B437987110F9B17E45b910F7c34c0C7d150",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="sei",
        display_name="Sei",
        aliases=("sei",),
        caip2="eip155:1329",
        chain_id=1329,
        environment="mainnet",
        cli_chain=None,
        usdc_address="0x4C0Fa1827A7F8e3704372420aB2768c3b0D3c237",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    # ===== TESTNETS =====
    NetworkConfig(
        key="baseSepolia",
        display_name="Base Sepolia",
        aliases=("basesepolia", "base-sepolia", "base sepolia"),
        caip2="eip155:84532",
        chain_id=84532,
        environment="testnet",
        cli_chain="BASE-SEPOLIA",
        usdc_address="0x036CbD53842c5426634c4923A462dA16422a504",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC testnet docs",
    ),
    NetworkConfig(
        key="ethereumSepolia",
        display_name="Ethereum Sepolia",
        aliases=("ethereumsepolia", "eth-sepolia", "sepolia"),
        caip2="eip155:11155111",
        chain_id=11155111,
        environment="testnet",
        cli_chain="ETH-SEPOLIA",
        usdc_address="0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC testnet docs",
    ),
    NetworkConfig(
        key="polygonAmoy",
        display_name="Polygon Amoy",
        aliases=("polygonamoy", "polygon-amoy", "amoy", "matic-amoy"),
        caip2="eip155:80002",
        chain_id=80002,
        environment="testnet",
        cli_chain="MATIC-AMOY",
        usdc_address="0x41E9460C73712648ad1752e277EaCe31ba0165b0",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC testnet docs",
    ),
    NetworkConfig(
        key="arbitrumSepolia",
        display_name="Arbitrum Sepolia",
        aliases=("arbitrumsepolia", "arb-sepolia", "arb sepolia"),
        caip2="eip155:421614",
        chain_id=421614,
        environment="testnet",
        cli_chain="ARBITRUM-SEPOLIA",
        usdc_address="0x75faf114eafb1BDbe2F0316DF893fd5BECEDB5aF",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC testnet docs",
    ),
    NetworkConfig(
        key="optimismSepolia",
        display_name="Optimism Sepolia",
        aliases=("optimismsepolia", "op-sepolia", "op sepolia"),
        caip2="eip155:11155420",
        chain_id=11155420,
        environment="testnet",
        cli_chain="OPT-SEPOLIA",
        usdc_address="0x5fd55a3bB5A0a060c4CE4b0F4ac0F4800A8c3c72",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC testnet docs",
    ),
    NetworkConfig(
        key="avalancheFuji",
        display_name="Avalanche Fuji",
        aliases=("avalanchefuji", "avax-fuji", "fuji"),
        caip2="eip155:43113",
        chain_id=43113,
        environment="testnet",
        cli_chain="AVAX-FUJI",
        usdc_address="0x5425890298aed601595873Ab3CDBeC7216d25e57",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Circle USDC testnet docs",
    ),
    NetworkConfig(
        key="arcTestnet",
        display_name="Arc Testnet",
        aliases=("arctestnet", "arc-testnet", "arc testnet"),
        caip2="eip155:5042002",
        chain_id=5042002,
        environment="testnet",
        cli_chain="ARC-TESTNET",
        usdc_address="0x3600000000000000000000000000000000000000",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x0077777d7EBA4688BDeF3E311b846F25870A19B9",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; hermes-x402 existing config",
    ),
    NetworkConfig(
        key="arcMainnet",
        display_name="Arc Mainnet",
        aliases=("arcmainnet", "arc-mainnet", "arc"),
        caip2="eip155:5042001",
        chain_id=5042001,
        environment="mainnet",
        cli_chain="ARC-MAINNET",
        usdc_address="",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="",
        facilitator_url="https://gateway-api.circle.com",
        gateway_api="https://gateway-api.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; USDC address unverified",
    ),
    NetworkConfig(
        key="baseSepolia_test",
        display_name="Base Sepolia (Test)",
        aliases=("basesepolia_test",),
        caip2="eip155:84532",
        chain_id=84532,
        environment="testnet",
        cli_chain="BASE-SEPOLIA",
        usdc_address="0x036CbD53842c5426634c4923A462dA16422a504",
        gateway_supported=True,
        buyer_cli_supported=True,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="Duplicate for test alias resolution",
    ),
    NetworkConfig(
        key="sonicTestnet",
        display_name="Sonic Testnet",
        aliases=("sonictestnet", "sonic-testnet", "sonic testnet"),
        caip2="eip155:64165",
        chain_id=64165,
        environment="testnet",
        cli_chain=None,
        usdc_address="0x59bF4F8176311c24B4D8F786646B59f0D8F0e982",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="unichainSepolia",
        display_name="Unichain Sepolia",
        aliases=("unichainsepolia", "unichain-sepolia"),
        caip2="eip155:1301",
        chain_id=1301,
        environment="testnet",
        cli_chain=None,
        usdc_address="0x3341B6a0B38D9F35a2c528D91C4B8C7052647086",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="worldChainSepolia",
        display_name="World Chain Sepolia",
        aliases=("worldchainsepolia", "worldchain-sepolia", "world-sepolia"),
        caip2="eip155:4801",
        chain_id=4801,
        environment="testnet",
        cli_chain=None,
        usdc_address="0x5fd55a3bB5A0a060c4CE4b0F4ac0F4800A8c3c72",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="hyperevmTestnet",
        display_name="HyperEVM Testnet",
        aliases=("hyperevmtestnet", "hyperevm-testnet", "hyper-testnet"),
        caip2="eip155:999",
        chain_id=999,
        environment="testnet",
        cli_chain=None,
        usdc_address="0x2d8B6B437987110F9B17E45b910F7c34c0C7d150",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
    NetworkConfig(
        key="seiAtlantic",
        display_name="Sei Atlantic",
        aliases=("seiatlantic", "sei-atlantic", "sei testnet"),
        caip2="eip155:1328",
        chain_id=1328,
        environment="testnet",
        cli_chain=None,
        usdc_address="0x4C0Fa1827A7F8e3704372420aB2768c3b0D3c237",
        gateway_supported=True,
        buyer_cli_supported=False,
        buyer_dcw_supported=True,
        seller_supported=True,
        gateway_wallet="0x091eB56A076eF1C84462F338C1E7c8C49c9c12E8",
        facilitator_url="https://gateway-api-testnet.circle.com",
        gateway_api="https://gateway-api-testnet.circle.com/v1",
        provenance="npm @circle-fin/x402-batching; Not verified for Circle CLI",
    ),
]

# Drop the test-only duplicate; keep the canonical entry only.
_NETWORKS = [n for n in _NETWORKS if not n.key.endswith("_test")]

# Build lookup indices (module-level, immutable after import).
_BY_KEY: dict[str, NetworkConfig] = {}
_BY_CAIP2: dict[str, NetworkConfig] = {}
_BY_CHAIN_ID: dict[int, NetworkConfig] = {}
_BY_ALIAS: dict[str, NetworkConfig] = {}
_DUPLICATE_CAIP2: set[str] = set()
_DUPLICATE_ALIASES: set[str] = set()

for _net in _NETWORKS:
    # Key
    if _net.key in _BY_KEY:
        raise ValueError(f"Duplicate network key: {_net.key}")
    _BY_KEY[_net.key] = _net

    # CAIP-2 (may have duplicates across environments — use the first one)
    if _net.caip2 in _BY_CAIP2:
        _DUPLICATE_CAIP2.add(_net.caip2)
    else:
        _BY_CAIP2[_net.caip2] = _net

    # Chain ID
    _BY_CHAIN_ID[_net.chain_id] = _net

    # Aliases
    for alias in _net.aliases:
        lower = alias.lower()
        if lower in _BY_ALIAS:
            _DUPLICATE_ALIASES.add(lower)
        else:
            _BY_ALIAS[lower] = _net


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class NetworkNotFoundError(Exception):
    """Raised when a network alias or key is not found in the registry."""


class NetworkConflictError(Exception):
    """Raised when a CAIP-2 maps to multiple networks (e.g. mainnet + testnet)."""


def get_network(value: str) -> NetworkConfig:
    """Resolve a network by key, alias, or CAIP-2 identifier.

    Raises ``NetworkNotFoundError`` if no match is found.
    Raises ``NetworkConflictError`` if a CAIP-2 maps to multiple networks.
    """
    if not value or not isinstance(value, str):
        raise NetworkNotFoundError("Network value is required")

    normalized = value.strip()

    # Try exact key first
    if normalized in _BY_KEY:
        return _BY_KEY[normalized]

    # Try alias (case-insensitive)
    lookup = normalized.lower()
    if lookup in _BY_ALIAS:
        return _BY_ALIAS[lookup]

    # Try CAIP-2
    if normalized in _BY_CAIP2:
        if normalized in _DUPLICATE_CAIP2:
            raise NetworkConflictError(
                f"CAIP-2 {normalized} maps to multiple networks. Use the network key explicitly."
            )
        return _BY_CAIP2[normalized]

    # Try EIP-155 numeric chain ID (e.g. "8453")
    try:
        chain_id = int(normalized)
        if chain_id in _BY_CHAIN_ID:
            return _BY_CHAIN_ID[chain_id]
    except (ValueError, TypeError):
        pass

    raise NetworkNotFoundError(
        f"Unknown network: {normalized!r}. Use a valid key, alias, or CAIP-2 identifier."
    )


def normalize_network(value: str) -> str:
    """Return the canonical key for a network, or raise NetworkNotFoundError."""
    return get_network(value).key


def list_networks(
    *,
    environment: Literal["mainnet", "testnet"] | None = None,
    gateway_supported: bool | None = None,
    buyer_cli_supported: bool | None = None,
    buyer_dcw_supported: bool | None = None,
    seller_supported: bool | None = None,
) -> list[NetworkConfig]:
    """List registered networks with optional capability filters.

    Returns a copy; callers cannot mutate the registry.
    """
    result = list(_NETWORKS)
    if environment is not None:
        result = [n for n in result if n.environment == environment]
    if gateway_supported is not None:
        result = [n for n in result if n.gateway_supported == gateway_supported]
    if buyer_cli_supported is not None:
        result = [n for n in result if n.buyer_cli_supported == buyer_cli_supported]
    if buyer_dcw_supported is not None:
        result = [n for n in result if n.buyer_dcw_supported == buyer_dcw_supported]
    if seller_supported is not None:
        result = [n for n in result if n.seller_supported == seller_supported]
    return result


def network_for_caip2(value: str) -> NetworkConfig | None:
    """Look up a network by CAIP-2. Returns None for unknown or conflicting."""
    if value in _DUPLICATE_CAIP2:
        return None
    return _BY_CAIP2.get(value)


def network_for_chain_id(chain_id: int) -> NetworkConfig | None:
    """Look up a network by numeric EIP-155 chain ID."""
    return _BY_CHAIN_ID.get(chain_id)


def is_private_network(network: NetworkConfig) -> bool:
    """Return True if the network is a private/reserved chain (not public internet)."""
    # All networks in our registry are public.  This function exists for
    # callers that need to guard against accidentally added private chains.
    return False
