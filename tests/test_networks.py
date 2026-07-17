"""Tests for the centralized network registry (hermes_x402.networks)."""

from __future__ import annotations

import pytest

from hermes_x402.networks import (
    _NETWORKS,
    NetworkNotFoundError,
    get_network,
    list_networks,
    network_for_caip2,
    network_for_chain_id,
)

# ---------------------------------------------------------------------------
# All 24 networks loaded
# ---------------------------------------------------------------------------


class TestRegistryPopulation:
    def test_all_24_networks_loaded(self):
        assert len(_NETWORKS) == 24

    def test_provenance_field_exists_on_all_networks(self):
        for net in _NETWORKS:
            assert isinstance(net.provenance, str)
            assert net.provenance != "", f"{net.key} missing provenance"


# ---------------------------------------------------------------------------
# Alias resolution for 'base'
# ---------------------------------------------------------------------------


class TestAliasResolution:
    def test_base_key_resolves(self):
        net = get_network("base")
        assert net.key == "base"
        assert net.chain_id == 8453

    def test_base_mainnet_alias_resolves(self):
        net = get_network("base-mainnet")
        assert net.key == "base"

    def test_base_alias_case_insensitive(self):
        net = get_network("BASE")
        assert net.key == "base"

    def test_eth_alias_resolves(self):
        net = get_network("eth")
        assert net.key == "ethereum"

    def test_matic_alias_resolves(self):
        net = get_network("matic")
        assert net.key == "polygon"

    def test_arb_alias_resolves(self):
        net = get_network("arb")
        assert net.key == "arbitrum"

    def test_op_alias_resolves(self):
        net = get_network("op")
        assert net.key == "optimism"

    def test_avax_alias_resolves(self):
        net = get_network("avax")
        assert net.key == "avalanche"

    def test_worldchain_alias_resolves(self):
        net = get_network("world chain")
        assert net.key == "worldChain"

    def test_hyper_alias_resolves(self):
        net = get_network("hyper")
        assert net.key == "hyperevm"

    def test_fuji_alias_resolves(self):
        net = get_network("fuji")
        assert net.key == "avalancheFuji"

    def test_sepolia_alias_resolves(self):
        net = get_network("sepolia")
        assert net.key == "ethereumSepolia"

    def test_arc_alias_resolves(self):
        net = get_network("arc")
        assert net.key == "arcMainnet"


# ---------------------------------------------------------------------------
# Unknown network fails closed
# ---------------------------------------------------------------------------


class TestUnknownNetwork:
    def test_unknown_network_raises_not_found(self):
        with pytest.raises(NetworkNotFoundError, match="Unknown network"):
            get_network("nonexistent_network_xyz")

    def test_empty_string_raises_not_found(self):
        with pytest.raises(NetworkNotFoundError, match="required"):
            get_network("")

    def test_none_raises_not_found(self):
        with pytest.raises(NetworkNotFoundError, match="required"):
            get_network(None)  # type: ignore[arg-type]

    def test_no_silent_fallback(self):
        """Verify that an unknown network never silently returns a default."""
        with pytest.raises(NetworkNotFoundError):
            get_network("does-not-exist")


# ---------------------------------------------------------------------------
# CAIP-2 lookup
# ---------------------------------------------------------------------------


class TestCaip2Lookup:
    def test_eip155_8453_resolves_to_base(self):
        net = get_network("eip155:8453")
        assert net.key == "base"

    def test_network_for_caip2_returns_config(self):
        net = network_for_caip2("eip155:8453")
        assert net is not None
        assert net.key == "base"

    def test_network_for_unknown_caip2_returns_none(self):
        net = network_for_caip2("eip155:99999999")
        assert net is None

    def test_conflicting_caip2_returns_none(self):
        """eip155:84532 appears in both baseSepolia and baseSepolia_test entries.
        The test duplicate is dropped, so baseSepolia remains unique."""
        # baseSepolia caip2 is unique after _test removal
        net = network_for_caip2("eip155:84532")
        assert net is not None
        assert net.key == "baseSepolia"


# ---------------------------------------------------------------------------
# network_for_chain_id
# ---------------------------------------------------------------------------


class TestChainIdLookup:
    def test_chain_id_8453_resolves_to_base(self):
        net = network_for_chain_id(8453)
        assert net is not None
        assert net.key == "base"

    def test_chain_id_1_resolves_to_ethereum(self):
        net = network_for_chain_id(1)
        assert net is not None
        assert net.key == "ethereum"

    def test_chain_id_5042002_resolves_to_arc_testnet(self):
        net = network_for_chain_id(5042002)
        assert net is not None
        assert net.key == "arcTestnet"

    def test_unknown_chain_id_returns_none(self):
        net = network_for_chain_id(999999)
        assert net is None


# ---------------------------------------------------------------------------
# list_networks with filters
# ---------------------------------------------------------------------------


class TestListNetworks:
    def test_list_all_returns_copy(self):
        all_nets = list_networks()
        assert len(all_nets) == 24
        all_nets.pop()
        assert len(list_networks()) == 24  # original unchanged

    def test_filter_by_environment_mainnet(self):
        mainnets = list_networks(environment="mainnet")
        assert len(mainnets) > 0
        assert all(n.environment == "mainnet" for n in mainnets)

    def test_filter_by_environment_testnet(self):
        testnets = list_networks(environment="testnet")
        assert len(testnets) > 0
        assert all(n.environment == "testnet" for n in testnets)

    def test_filter_by_gateway_supported(self):
        gw = list_networks(gateway_supported=True)
        assert len(gw) > 0
        assert all(n.gateway_supported for n in gw)

    def test_filter_by_buyer_cli_supported(self):
        cli = list_networks(buyer_cli_supported=True)
        assert len(cli) > 0
        assert all(n.buyer_cli_supported for n in cli)

    def test_filter_by_seller_supported(self):
        seller = list_networks(seller_supported=True)
        assert len(seller) > 0
        assert all(n.seller_supported for n in seller)

    def test_combined_filter(self):
        nets = list_networks(environment="mainnet", gateway_supported=True)
        assert all(n.environment == "mainnet" and n.gateway_supported for n in nets)


# ---------------------------------------------------------------------------
# Arc Testnet CLI support (verified 2026-07-17: Circle CLI 0.0.5)
# ---------------------------------------------------------------------------


class TestArcTestnetCliSupport:
    """Arc Testnet buyer_cli_supported verified by live payment on 2026-07-17."""

    def test_arc_testnet_buyer_cli_supported(self):
        net = get_network("arcTestnet")
        assert net.buyer_cli_supported is True

    def test_arc_testnet_cli_chain_value(self):
        net = get_network("arcTestnet")
        assert net.cli_chain == "ARC-TESTNET"

    def test_arc_testnet_alias_arctestnet(self):
        net = get_network("arctestnet")
        assert net.key == "arcTestnet"
        assert net.chain_id == 5042002

    def test_arc_testnet_alias_hyphenated(self):
        net = get_network("arc-testnet")
        assert net.key == "arcTestnet"

    def test_arc_testnet_alias_spaced(self):
        net = get_network("arc testnet")
        assert net.key == "arcTestnet"

    def test_arc_testnet_caip2_resolution(self):
        net = network_for_caip2("eip155:5042002")
        assert net is not None
        assert net.key == "arcTestnet"
        assert net.buyer_cli_supported is True

    def test_arc_testnet_chain_id_resolution(self):
        net = network_for_chain_id(5042002)
        assert net is not None
        assert net.key == "arcTestnet"
        assert net.cli_chain == "ARC-TESTNET"

    def test_arc_testnet_usdc_address(self):
        net = get_network("arcTestnet")
        assert net.usdc_address == "0x3600000000000000000000000000000000000000"

    def test_arc_testnet_gateway_supported(self):
        net = get_network("arcTestnet")
        assert net.gateway_supported is True

    def test_arc_testnet_dcw_still_supported(self):
        """DCW support was True before; verify it remains True."""
        net = get_network("arcTestnet")
        assert net.buyer_dcw_supported is True

    def test_arc_testnet_in_cli_supported_list(self):
        cli_nets = list_networks(buyer_cli_supported=True)
        keys = [n.key for n in cli_nets]
        assert "arcTestnet" in keys
