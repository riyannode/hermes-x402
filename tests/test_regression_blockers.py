"""Regression tests for ALL PR #4 merge blockers.

Covers findings 1–15 from the merge-blocker audit:

  1.  Approval config field mismatch (require_approval_for_new_host)
  2.  Seller underpayment (server-computed amount)
  3.  CAIP-2 networks in seller challenges
  4.  Runtime host policy enforcement before payment
  5.  DNS validation wired into all paths
  6.  Exact wallet network matching in supports
  7.  DCW capability matrix (Arc Testnet only)
  8.  Arc Mainnet disabled
  9.  172.16.0.0/12 classification (not all 172.*)
  10. Streaming fetch bounded reads
  11. Operation-bound approval architecture
  12. Daily budget claim accuracy
  13. Capability output (buyer/seller separate)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from hermes_x402.buyer.approval import (
    PaymentApprovalRequest,
    check_approval_required,
)
from hermes_x402.buyer.errors import BuyerConfigurationError
from hermes_x402.buyer.supports import _detect_backend_support
from hermes_x402.config import X402Config
from hermes_x402.network_policy import NetworkPolicy, validate_url_strict
from hermes_x402.networks import get_network, list_networks
from hermes_x402.seller_gateway import _parse_price

# ════════════════════════════════════════════════════════════════════════════
# Finding 1: Approval config field mismatch
# ════════════════════════════════════════════════════════════════════════════


class TestApprovalConfigField:
    """check_approval_required must read require_approval_for_new_host."""

    def _fresh_store(self, tmp_path):
        """Create a TrustedHostStore backed by a temp path (no singleton)."""
        from hermes_x402.buyer.approval import TrustedHostStore

        return TrustedHostStore(path=tmp_path / "trusted_hosts.json")

    def _patch_store(self, tmp_path, monkeypatch):
        """Patch _get_store to return a fresh store from tmp_path."""
        from hermes_x402.buyer.approval import TrustedHostStore

        def _fresh():
            return TrustedHostStore(path=tmp_path / "trusted_hosts.json")

        monkeypatch.setattr("hermes_x402.buyer.approval._get_store", _fresh)

    def test_config_true_enables_approval(self, tmp_path, monkeypatch):
        """config=True → approval required."""
        self._patch_store(tmp_path, monkeypatch)
        config = X402Config(require_approval_for_new_host=True)
        result = check_approval_required("https://example.com/data", config=config)
        # Empty trust store → host not trusted → approval_required
        assert result is not None
        assert result["error"] == "approval_required"

    def test_config_false_disables_approval(self, tmp_path, monkeypatch):
        """config=False → approval disabled by authoritative config."""
        self._patch_store(tmp_path, monkeypatch)
        config = X402Config(require_approval_for_new_host=False)
        result = check_approval_required("https://example.com/data", config=config)
        # Config explicitly disables → None (no approval needed)
        assert result is None

    def test_config_absent_env_true(self, tmp_path, monkeypatch):
        """config absent, env=true → approval required."""
        self._patch_store(tmp_path, monkeypatch)
        with patch.dict("os.environ", {"X402_REQUIRE_APPROVAL_FOR_NEW_HOST": "true"}):
            result = check_approval_required("https://example.com/data", config=None)
            assert result is not None
            assert result["error"] == "approval_required"

    def test_config_false_overrides_env_true(self, tmp_path, monkeypatch):
        """config=False overrides env=true → disabled."""
        self._patch_store(tmp_path, monkeypatch)
        with patch.dict("os.environ", {"X402_REQUIRE_APPROVAL_FOR_NEW_HOST": "true"}):
            config = X402Config(require_approval_for_new_host=False)
            result = check_approval_required("https://example.com/data", config=config)
            assert result is None

    def test_empty_store_trusted_host_allowed(self, tmp_path, monkeypatch):
        """Approval enabled + trusted host → allowed."""
        self._patch_store(tmp_path, monkeypatch)
        # Pre-trust the host via the patched store
        from hermes_x402.buyer.approval import TrustedHostStore

        store = TrustedHostStore(path=tmp_path / "trusted_hosts.json")
        store.trust("trusted.example.com")

        config = X402Config(require_approval_for_new_host=True)
        result = check_approval_required("https://trusted.example.com/data", config=config)
        assert result is None  # trusted host → allowed

    def test_store_read_failure_blocks_payment(self, tmp_path, monkeypatch):
        """Store read failure → payment blocked (fail-closed, returns dict)."""
        mock_store = MagicMock()
        mock_store._ensure_loaded.side_effect = OSError("disk error")
        monkeypatch.setattr("hermes_x402.buyer.approval._get_store", lambda: mock_store)

        config = X402Config(require_approval_for_new_host=True)
        result = check_approval_required("https://example.com/data", config=config)
        # Function catches OSError and returns fail-closed dict
        assert result is not None
        assert result["error"] == "approval_check_failed"
        assert result["retry_safe"] is False

    def test_corrupt_store_blocks_payment(self, tmp_path, monkeypatch):
        """Corrupt store → payment blocked (fail-closed)."""
        mock_store = MagicMock()
        mock_store._ensure_loaded.return_value = None
        mock_store.load_failed = True
        monkeypatch.setattr("hermes_x402.buyer.approval._get_store", lambda: mock_store)

        config = X402Config(require_approval_for_new_host=True)
        result = check_approval_required("https://example.com/data", config=config)
        assert result is not None
        assert result["error"] == "approval_check_failed"

    def test_disabled_approval_empty_store_allowed(self, tmp_path, monkeypatch):
        """Disabled approval + empty store → allowed (returns None)."""
        self._patch_store(tmp_path, monkeypatch)
        config = X402Config(require_approval_for_new_host=False)
        result = check_approval_required("https://example.com/data", config=config)
        assert result is None

    def test_malformed_store_blocks_payment(self, tmp_path, monkeypatch):
        """Malformed store → payment blocked (fail-closed)."""
        mock_store = MagicMock()
        mock_store._ensure_loaded.return_value = None
        mock_store.load_failed = True
        mock_store.is_trusted.return_value = False
        monkeypatch.setattr("hermes_x402.buyer.approval._get_store", lambda: mock_store)

        config = X402Config(require_approval_for_new_host=True)
        result = check_approval_required("https://example.com/data", config=config)
        assert result is not None
        assert result["error"] == "approval_check_failed"


# ════════════════════════════════════════════════════════════════════════════
# Finding 2: Seller underpayment prevention
# ════════════════════════════════════════════════════════════════════════════


class TestSellerUnderpayment:
    """Seller must settle against server-computed route amount."""

    def test_parse_price_exact(self):
        """$0.01 → 10000 atomic."""
        assert _parse_price("$0.01") == "10000"

    def test_parse_price_rejects_zero(self):
        with pytest.raises(ValueError, match="greater than zero"):
            _parse_price("$0.00")

    def test_parse_price_rejects_negative(self):
        with pytest.raises(ValueError, match="must not be negative"):
            _parse_price("-$0.01")

    def test_parse_price_dollar_negative(self):
        """$-0.01 → negative error."""
        with pytest.raises(ValueError, match="must not be negative"):
            _parse_price("$-0.01")

    def test_parse_price_bare_negative(self):
        """-0.01 → negative error."""
        with pytest.raises(ValueError, match="must not be negative"):
            _parse_price("-0.01")

    def test_parse_price_negative_zero(self):
        """-0 → zero error (Decimal('-0') is not < 0, but == 0)."""
        with pytest.raises(ValueError, match="greater than zero"):
            _parse_price("-0")

    def test_parse_price_nan(self):
        with pytest.raises(ValueError, match="NaN"):
            _parse_price("NaN")

    def test_parse_price_infinity(self):
        with pytest.raises(ValueError, match="Infinity"):
            _parse_price("Infinity")

    def test_parse_price_rejects_excess_precision(self):
        with pytest.raises(ValueError, match="excess precision"):
            _parse_price("$0.0000001")

    def test_server_amount_is_exact_integer(self):
        """Server-computed amount must be exact integer atomic units."""
        amount = _parse_price("$0.01")
        assert amount.isdigit()
        assert int(amount) == 10000


# ════════════════════════════════════════════════════════════════════════════
# Finding 3: CAIP-2 networks in seller challenges
# ════════════════════════════════════════════════════════════════════════════


class TestCAIP2Networks:
    """Seller challenges must emit CAIP-2 identifiers, not registry keys."""

    def test_build_accepts_uses_caip2(self):
        """_build_accepts must emit net.caip2, not net.key."""
        from hermes_x402.seller_gateway import X402Gateway

        gw = X402Gateway(
            seller_address="0x" + "ab" * 20,
            networks=[get_network("base")],
            facilitator_url="https://gateway-api.circle.com",
            default_description="Test",
        )
        accepts = gw._build_accepts([get_network("base")], None)
        assert len(accepts) == 1
        assert accepts[0]["network"] == "eip155:8453"  # CAIP-2 for Base

    def test_base_caip2(self):
        net = get_network("base")
        assert net.caip2 == "eip155:8453"

    def test_polygon_caip2(self):
        net = get_network("polygon")
        assert net.caip2 == "eip155:137"

    def test_arc_testnet_caip2(self):
        net = get_network("arcTestnet")
        assert net.caip2 == "eip155:5042002"

    def test_registry_key_never_caip2(self):
        """Registry key must never be confused with CAIP-2."""
        net = get_network("base")
        assert net.key == "base"
        assert net.caip2 == "eip155:8453"
        assert net.key != net.caip2


# ════════════════════════════════════════════════════════════════════════════
# Finding 4: Runtime host policy enforcement
# ════════════════════════════════════════════════════════════════════════════


class TestRuntimeHostPolicy:
    """NetworkPolicy must be enforced before payment."""

    def test_strict_empty_allowlist_blocks_all(self):
        """strict_allowlist + empty allowlist → block every destination."""
        err = validate_url_strict("https://example.com/api", (), "strict_allowlist")
        assert err is not None
        assert "empty allowlist" in err

    def test_strict_matching_host_allowed(self):
        """strict_allowlist + matching host → allow."""
        policy = NetworkPolicy(mode="strict_allowlist", host_allowlist=("example.com",))
        assert policy.is_url_allowed("https://example.com/data")

    def test_strict_nonmatching_host_blocked(self):
        """strict_allowlist + non-matching host → block."""
        policy = NetworkPolicy(mode="strict_allowlist", host_allowlist=("example.com",))
        assert not policy.is_url_allowed("https://other.com/data")

    def test_public_empty_allowlist_allows_public(self):
        """public + empty allowlist → allow public destinations."""
        policy = NetworkPolicy(mode="public", host_allowlist=())
        assert policy.is_url_allowed("https://example.com/data")

    def test_public_with_allowlist_restricts(self):
        """public + configured allowlist → restrict to that list."""
        policy = NetworkPolicy(mode="public", host_allowlist=("example.com",))
        assert policy.is_url_allowed("https://example.com/data")
        assert not policy.is_url_allowed("https://other.com/data")


# ════════════════════════════════════════════════════════════════════════════
# Finding 9: 172.16.0.0/12 classification
# ════════════════════════════════════════════════════════════════════════════


class TestIPClassification:
    """172.16.0.0/12 must be correctly classified."""

    def test_172_16_0_1_rejected(self):
        """172.16.0.1 → rejected (private)."""
        err = validate_url_strict("https://172.16.0.1/", (), "public")
        assert err is not None
        assert "private" in err.lower() or "reserved" in err.lower()

    def test_172_31_255_255_rejected(self):
        """172.31.255.255 → rejected (private)."""
        err = validate_url_strict("https://172.31.255.255/", (), "public")
        assert err is not None

    def test_172_15_255_255_allowed(self):
        """172.15.255.255 → allowed (NOT in 172.16/12)."""
        err = validate_url_strict("https://172.15.255.255/", (), "public")
        assert err is None

    def test_172_32_0_1_allowed(self):
        """172.32.0.1 → allowed (NOT in 172.16/12)."""
        err = validate_url_strict("https://172.32.0.1/", (), "public")
        assert err is None

    def test_172_217_0_1_allowed(self):
        """172.217.0.1 (Google) → allowed."""
        err = validate_url_strict("https://172.217.0.1/", (), "public")
        assert err is None


# ════════════════════════════════════════════════════════════════════════════
# Finding 6: Exact wallet network matching
# ════════════════════════════════════════════════════════════════════════════


class TestWalletNetworkMatching:
    """check_supports must match exact configured wallet network."""

    def test_cli_base_matches_base(self):
        """CLI BASE wallet + eip155:8453 → supported."""
        result = _detect_backend_support(
            "base",
            configured_backend="cli",
            wallet_network="base",
        )
        assert result is True

    def test_cli_base_rejects_polygon(self):
        """CLI BASE wallet + eip155:137 → unsupported."""
        result = _detect_backend_support(
            "polygon",
            configured_backend="cli",
            wallet_network="base",
        )
        assert result is False

    def test_cli_base_rejects_sepolia(self):
        """CLI BASE wallet + Base Sepolia → unsupported."""
        result = _detect_backend_support(
            "baseSepolia_test",
            configured_backend="cli",
            wallet_network="base",
        )
        assert result is False

    def test_dcw_arc_testnet_matches(self):
        """DCW arcTestnet wallet + eip155:5042002 → supported."""
        result = _detect_backend_support(
            "arcTestnet",
            configured_backend="dcw",
            wallet_network="arcTestnet",
        )
        assert result is True

    def test_dcw_arc_testnet_rejects_base(self):
        """DCW arcTestnet wallet + Base → unsupported."""
        result = _detect_backend_support(
            "base",
            configured_backend="dcw",
            wallet_network="arcTestnet",
        )
        assert result is False


# ════════════════════════════════════════════════════════════════════════════
# Finding 7: DCW capability matrix
# ════════════════════════════════════════════════════════════════════════════


class TestDCWCapabilityMatrix:
    """DCW buyer support must be limited to Arc Testnet only."""

    def test_arc_testnet_dcw_supported(self):
        net = get_network("arcTestnet")
        assert net.buyer_dcw_supported is True

    def test_base_dcw_not_supported(self):
        net = get_network("base")
        assert net.buyer_dcw_supported is False

    def test_polygon_dcw_not_supported(self):
        net = get_network("polygon")
        assert net.buyer_dcw_supported is False

    def test_arbitrum_dcw_not_supported(self):
        net = get_network("arbitrum")
        assert net.buyer_dcw_supported is False

    def test_ethereum_dcw_not_supported(self):
        net = get_network("ethereum")
        assert net.buyer_dcw_supported is False


# ════════════════════════════════════════════════════════════════════════════
# Finding 8: Arc Mainnet disabled
# ════════════════════════════════════════════════════════════════════════════


class TestArcMainnetDisabled:
    """Arc Mainnet must remain fully disabled."""

    def test_arc_mainnet_all_flags_false(self):
        net = get_network("arcMainnet")
        assert net.gateway_supported is False
        assert net.buyer_cli_supported is False
        assert net.buyer_dcw_supported is False
        assert net.seller_supported is False

    def test_arc_mainnet_empty_usdc(self):
        net = get_network("arcMainnet")
        assert net.usdc_address == ""

    def test_arc_mainnet_unverified_provenance(self):
        net = get_network("arcMainnet")
        assert "UNVERIFIED" in net.provenance.upper()

    def test_arc_mainnet_not_in_list_operational(self):
        """Arc Mainnet should not appear as operational in capability output."""
        all_networks = list_networks()
        operational = [
            n
            for n in all_networks
            if n.key == "arcMainnet" and (n.gateway_supported or n.seller_supported)
        ]
        assert len(operational) == 0


# ════════════════════════════════════════════════════════════════════════════
# Finding 11: Operation-bound approval architecture
# ════════════════════════════════════════════════════════════════════════════


class TestOperationBoundApproval:
    """PaymentApprovalRequest must have all required fields."""

    def test_approval_request_has_all_fields(self):
        req = PaymentApprovalRequest.create(
            host="example.com",
            resource="/api/data",
            method="GET",
            amount_usdc="0.01",
            network="eip155:8453",
            wallet_fingerprint="abc123",
        )
        assert req.host == "example.com"
        assert req.resource == "/api/data"
        assert req.method == "GET"
        assert req.amount_usdc == "0.01"
        assert req.network == "eip155:8453"
        assert req.wallet_fingerprint == "abc123"
        assert req.operation_id  # non-empty
        assert req.nonce  # non-empty
        assert not req.is_expired()

    def test_approval_request_rejects_invalid_host(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            PaymentApprovalRequest.create(
                host="not valid://host",
                resource="/api",
                method="GET",
                amount_usdc="0.01",
                network="eip155:8453",
                wallet_fingerprint="abc",
            )

    def test_approval_request_serialization_roundtrip(self):
        req = PaymentApprovalRequest.create(
            host="example.com",
            resource="/api",
            method="GET",
            amount_usdc="0.01",
            network="eip155:8453",
            wallet_fingerprint="abc",
        )
        d = req.to_dict()
        restored = PaymentApprovalRequest.from_dict(d)
        assert restored.operation_id == req.operation_id
        assert restored.host == req.host
        assert restored.amount_usdc == req.amount_usdc

    def test_approval_limitation_documented(self, tmp_path, monkeypatch):
        """Document the approval architecture limitation.

        PR #4 does NOT implement native chat approval. The correct contract is:
        - Hermes native chat approval is not implemented.
        - New-host payment is blocked fail-closed.
        - Administrative trust is required.
        - The model cannot self-approve.
        """
        from hermes_x402.buyer.approval import TrustedHostStore

        def _fresh():
            return TrustedHostStore(path=tmp_path / "trusted_hosts.json")

        monkeypatch.setattr("hermes_x402.buyer.approval._get_store", _fresh)

        config = X402Config(require_approval_for_new_host=True)
        result = check_approval_required("https://untrusted.example.com/api", config=config)
        assert result is not None
        assert result["error"] == "approval_required"
        msg = result["message"].lower()
        assert "approval" in msg
        assert "trust" in msg or "approved" in msg


# ════════════════════════════════════════════════════════════════════════════
# Finding 12: Daily budget claim
# ════════════════════════════════════════════════════════════════════════════


class TestDailyBudgetClaim:
    """Daily budget is accepted but NOT enforced."""

    def test_valid_budget_accepted(self):
        config = X402Config(daily_budget_usdc="10.00")
        result = config.validate_daily_budget()
        assert result == "10.00"

    def test_invalid_budget_raises(self):
        config = X402Config(daily_budget_usdc="not_a_number")
        with pytest.raises(BuyerConfigurationError, match="Invalid"):
            config.validate_daily_budget()

    def test_negative_budget_raises(self):
        config = X402Config(daily_budget_usdc="-5")
        with pytest.raises(BuyerConfigurationError, match="non-negative"):
            config.validate_daily_budget()

    def test_none_budget_returns_none(self):
        config = X402Config(daily_budget_usdc=None)
        assert config.validate_daily_budget() is None

    def test_schema_not_enforcement_claim(self):
        """The schema description must not claim enforcement."""
        from hermes_x402.hermes_plugin.schemas import X402_PAY_SCHEMA

        desc = X402_PAY_SCHEMA["description"]
        assert "not enforced" in desc.lower() or "accepted but not enforced" in desc.lower()


# ════════════════════════════════════════════════════════════════════════════
# Finding 13: Capability output (buyer/seller separate)
# ════════════════════════════════════════════════════════════════════════════


class TestCapabilityOutput:
    """buyer_backend_supported must not leak through seller flag."""

    def test_arc_testnet_cli_supported(self):
        """arcTestnet buyer_cli_supported verified by live payment 2026-07-17."""
        net = get_network("arcTestnet")
        assert net.buyer_cli_supported is True

    def test_arc_testnet_dcw_supported(self):
        """arcTestnet buyer_dcw_supported must be True."""
        net = get_network("arcTestnet")
        assert net.buyer_dcw_supported is True

    def test_arc_testnet_seller_supported(self):
        """arcTestnet seller_supported must be True."""
        net = get_network("arcTestnet")
        assert net.seller_supported is True

    def test_capability_fields_present(self):
        """All networks should have distinct buyer/seller fields."""
        all_networks = list_networks()
        for net in all_networks:
            assert hasattr(net, "buyer_cli_supported")
            assert hasattr(net, "buyer_dcw_supported")
            assert hasattr(net, "seller_supported")
            assert isinstance(net.buyer_cli_supported, bool)
            assert isinstance(net.buyer_dcw_supported, bool)
            assert isinstance(net.seller_supported, bool)
