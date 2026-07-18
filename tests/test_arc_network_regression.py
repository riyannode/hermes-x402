"""Deterministic regression test for the merged Arc network bug.

Proves that the ARC-TESTNET CLI chain name is correctly preserved
throughout the tool chain — not silently converted to the registry
key (arcTestnet) or the CAIP-2 identifier (eip155:5042002).

Regression scenario:
  configured Circle CLI chain: ARC-TESTNET
  x402 challenge network key: arcTestnet
  network ID: eip155:5042002

Proves:
  - get_network resolves ARC-TESTNET → correct NetworkConfig
  - cli_chain is ARC-TESTNET (not arcTestnet)
  - caip2 is eip155:5042002
  - buyer_cli_supported is True
  - gateway_supported is True
"""

from __future__ import annotations


class TestArcCliNetworkCanonicalization:
    """Regression: ARC-TESTNET must resolve correctly through the registry."""

    def test_get_network_by_cli_chain(self):
        """get_network('ARC-TESTNET') resolves to the correct entry."""
        from hermes_x402.networks import get_network

        net = get_network("ARC-TESTNET")
        assert net.key == "arcTestnet"
        assert net.caip2 == "eip155:5042002"
        assert net.cli_chain == "ARC-TESTNET"
        assert net.environment == "testnet"
        assert net.buyer_cli_supported is True
        assert net.gateway_supported is True

    def test_get_network_by_registry_key(self):
        """get_network('arcTestnet') resolves to the same entry."""
        from hermes_x402.networks import get_network

        net = get_network("arcTestnet")
        assert net.key == "arcTestnet"
        assert net.caip2 == "eip155:5042002"
        assert net.cli_chain == "ARC-TESTNET"

    def test_get_network_by_caip2(self):
        """get_network('eip155:5042002') resolves to the same entry."""
        from hermes_x402.networks import get_network

        net = get_network("eip155:5042002")
        assert net.key == "arcTestnet"
        assert net.cli_chain == "ARC-TESTNET"

    def test_get_network_by_chain_id(self):
        """get_network('5042002') resolves to the same entry."""
        from hermes_x402.networks import get_network

        net = get_network("5042002")
        assert net.key == "arcTestnet"
        assert net.cli_chain == "ARC-TESTNET"

    def test_get_network_case_insensitive(self):
        """get_network is case-insensitive for aliases."""
        from hermes_x402.networks import get_network

        net = get_network("arctestnet")
        assert net.key == "arcTestnet"
        assert net.cli_chain == "ARC-TESTNET"

    def test_arc_testnet_chain_id(self):
        """Chain ID 5042002 matches the canonical Arc Testnet value."""
        from hermes_x402.networks import get_network

        net = get_network("ARC-TESTNET")
        assert net.chain_id == 5042002

    def test_arc_testnet_usdc_address(self):
        """USDC address on Arc Testnet is the canonical Circle address."""
        from hermes_x402.networks import get_network

        net = get_network("ARC-TESTNET")
        assert net.usdc_address == "0x3600000000000000000000000000000000000000"

    def test_arc_testnet_dcw_supported(self):
        """Arc Testnet is the ONLY network with DCW buyer support."""
        from hermes_x402.networks import get_network

        net = get_network("ARC-TESTNET")
        assert net.buyer_dcw_supported is True

    def test_arc_testnet_gateway_wallet(self):
        """Gateway wallet for Arc Testnet is canonical."""
        from hermes_x402.networks import get_network

        net = get_network("ARC-TESTNET")
        assert net.gateway_wallet == "0x0077777d7EBA4688BDeF3E311b846F25870A19B9"

    def test_cli_chain_not_registry_key(self):
        """cli_chain must be ARC-TESTNET, not arcTestnet or arc-testnet."""
        from hermes_x402.networks import get_network

        net = get_network("ARC-TESTNET")
        assert net.cli_chain == "ARC-TESTNET"
        assert net.cli_chain != "arcTestnet"
        assert net.cli_chain != "arc-testnet"
        assert net.cli_chain != "arctestnet"

    def test_all_resolution_forms_equal(self):
        """All input forms resolve to the identical NetworkConfig object."""
        from hermes_x402.networks import get_network

        forms = [
            "ARC-TESTNET",
            "arcTestnet",
            "arc-testnet",
            "eip155:5042002",
            "5042002",
        ]
        configs = [get_network(f) for f in forms]
        # All should be the same object (identity, not just equality)
        first = configs[0]
        for cfg in configs[1:]:
            assert cfg is first, f"Resolution mismatch: {cfg.key} != {first.key}"

    def test_config_from_env_arc_testnet(self):
        """X402Config.from_env with ARC-TESTNET network produces correct circle_cli_network."""
        import os
        from unittest.mock import patch

        env = {
            "X402_ROLE": "buyer",
            "X402_BUYER_BACKEND": "cli",
            "CIRCLE_AGENT_WALLET_ADDRESS": "0x1234567890abcdef1234567890abcdef12345678",
            "CIRCLE_AGENT_WALLET_NETWORK": "ARC-TESTNET",
            "X402_MAX_USDC_PER_PAYMENT": "0.10",
        }
        with patch.dict(os.environ, env, clear=False):
            from hermes_x402.config import X402Config

            config = X402Config.from_env()
            assert config.circle_cli_network == "ARC-TESTNET"
            assert config.blockchain == "ARC-TESTNET"

    def test_network_resolution_preserves_cli_chain_for_cli_operations(self):
        """When configured with ARC-TESTNET, CLI operations must use ARC-TESTNET."""
        from hermes_x402.networks import get_network

        # Simulate what runtime does: resolve network, extract cli_chain
        configured_network = "ARC-TESTNET"
        resolved = get_network(configured_network)

        # CLI operations must use cli_chain, NOT the registry key
        assert resolved.cli_chain == "ARC-TESTNET"
        # Protocol operations use caip2
        assert resolved.caip2 == "eip155:5042002"
        # These must be different values
        assert resolved.cli_chain != resolved.caip2
        assert resolved.cli_chain != resolved.key
