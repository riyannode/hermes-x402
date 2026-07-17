"""Tests for network option selection (hermes_x402.buyer.options)."""

from __future__ import annotations

from hermes_x402.buyer.models import PaymentOption
from hermes_x402.buyer.options import (
    _get_default_network_order,
    _is_under_cap,
    parse_network_preference,
    parse_require_gateway,
    select_payment_option,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opt(
    network: str = "eip155:8453",
    amount: str = "10000",
    payment_system: str = "gateway_batching",
) -> PaymentOption:
    """Build a minimal PaymentOption for testing."""
    return PaymentOption(
        scheme="exact",
        payment_system=payment_system,
        network="base",
        network_id=network,
        amount_atomic=amount,
        amount_usdc="0.01",
        asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        supported_by_backend=True,
        pay_to="0xSeller",
        max_timeout_seconds=604900,
    )


# ---------------------------------------------------------------------------
# Invalid challenge options discarded
# ---------------------------------------------------------------------------


class TestInvalidOptionsDiscarded:
    def test_empty_network_id_discarded(self):
        opt = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="",
            network_id="",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        result = select_payment_option((opt,))
        assert result is None

    def test_empty_amount_discarded(self):
        opt = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="base",
            network_id="eip155:8453",
            amount_atomic="",
            amount_usdc="0",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        result = select_payment_option((opt,))
        assert result is None

    def test_negative_amount_discarded(self):
        opt = _opt(amount="-1")
        result = select_payment_option((opt,))
        assert result is None

    def test_non_numeric_amount_discarded(self):
        opt = _opt(amount="not-a-number")
        result = select_payment_option((opt,))
        assert result is None


# ---------------------------------------------------------------------------
# Networks absent from registry discarded
# ---------------------------------------------------------------------------


class TestRegistryFiltering:
    def test_unknown_network_discarded(self):
        opt = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="unknown",
            network_id="eip155:99999999",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        result = select_payment_option((opt,))
        assert result is None

    def test_known_network_accepted(self):
        opt = _opt(network="eip155:8453")
        result = select_payment_option((opt,))
        assert result is not None


# ---------------------------------------------------------------------------
# Non-Gateway options discarded when Gateway required
# ---------------------------------------------------------------------------


class TestGatewayFiltering:
    def test_vanilla_discarded_when_gateway_required(self):
        opt = _opt(payment_system="vanilla")
        result = select_payment_option((opt,), require_gateway=True)
        assert result is None

    def test_gateway_accepted_when_gateway_required(self):
        opt = _opt(payment_system="gateway_batching")
        result = select_payment_option((opt,), require_gateway=True)
        assert result is not None

    def test_vanilla_accepted_when_gateway_not_required(self):
        opt = _opt(payment_system="vanilla")
        result = select_payment_option((opt,), require_gateway=False)
        assert result is not None


# ---------------------------------------------------------------------------
# Options above payment cap discarded
# ---------------------------------------------------------------------------


class TestPaymentCap:
    def test_over_cap_discarded(self):
        opt = _opt(amount="20000")  # $0.02
        result = select_payment_option((opt,), max_usdc="0.01")
        assert result is None

    def test_under_cap_accepted(self):
        opt = _opt(amount="5000")  # $0.005
        result = select_payment_option((opt,), max_usdc="0.01")
        assert result is not None

    def test_exactly_at_cap_accepted(self):
        opt = _opt(amount="10000")  # exactly $0.01
        result = select_payment_option((opt,), max_usdc="0.01")
        assert result is not None


# ---------------------------------------------------------------------------
# First configured preference chosen
# ---------------------------------------------------------------------------


class TestPreferenceOrdering:
    def test_preferred_network_chosen_first(self):
        opt_polygon = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="polygon",
            network_id="eip155:137",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        opt_base = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="base",
            network_id="eip155:8453",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        result = select_payment_option(
            (opt_polygon, opt_base),
            network_preference=("base",),
        )
        assert result is not None
        assert result.network == "base"


# ---------------------------------------------------------------------------
# Deterministic registry order used
# ---------------------------------------------------------------------------


class TestDeterministicOrder:
    def test_registry_order_determines_selection(self):
        """When no preference is set, base (first in registry) should be chosen."""
        opt_polygon = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="polygon",
            network_id="eip155:137",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        opt_base = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="base",
            network_id="eip155:8453",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        result = select_payment_option(
            (opt_polygon, opt_base),
            network_preference=(),
        )
        assert result is not None
        assert result.network == "base"

    def test_get_default_network_order_returns_list(self):
        order = _get_default_network_order()
        assert isinstance(order, list)
        assert len(order) > 0
        assert order[0] == "base"


# ---------------------------------------------------------------------------
# None returned when no option matches
# ---------------------------------------------------------------------------


class TestNoMatch:
    def test_none_for_empty_options(self):
        result = select_payment_option(())
        assert result is None

    def test_none_when_all_discarded(self):
        opt = _opt(amount="-1")
        result = select_payment_option((opt,))
        assert result is None

    def test_none_when_backend_unsupported(self):
        opt = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="sonic",
            network_id="eip155:146",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=False,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        # With configured backend, unsupported networks are discarded
        result = select_payment_option((opt,), configured_backend="cli")
        # sonic has buyer_cli_supported=False, so it should be discarded
        assert result is None


# ---------------------------------------------------------------------------
# parse_network_preference from env
# ---------------------------------------------------------------------------


class TestParseNetworkPreference:
    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_PREFERENCE", "base, polygon, ethereum")
        result = parse_network_preference()
        assert result == ("base", "polygon", "ethereum")

    def test_empty(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_PREFERENCE", "")
        assert parse_network_preference() == ()

    def test_absent(self, monkeypatch):
        monkeypatch.delenv("X402_NETWORK_PREFERENCE", raising=False)
        assert parse_network_preference() == ()


# ---------------------------------------------------------------------------
# parse_require_gateway from env
# ---------------------------------------------------------------------------


class TestParseRequireGateway:
    def test_true_values(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "Yes"):
            monkeypatch.setenv("X402_REQUIRE_GATEWAY_BATCHING", val)
            assert parse_require_gateway() is True

    def test_false_values(self, monkeypatch):
        for val in ("0", "false", "no", "bogus", ""):
            monkeypatch.setenv("X402_REQUIRE_GATEWAY_BATCHING", val)
            assert parse_require_gateway() is False

    def test_absent(self, monkeypatch):
        monkeypatch.delenv("X402_REQUIRE_GATEWAY_BATCHING", raising=False)
        assert parse_require_gateway() is False


# ---------------------------------------------------------------------------
# _is_under_cap
# ---------------------------------------------------------------------------


class TestUnderCap:
    def test_under_cap(self):
        assert _is_under_cap("10000", "0.01") is True

    def test_over_cap(self):
        assert _is_under_cap("20000", "0.01") is False

    def test_invalid_atomic(self):
        assert _is_under_cap("not-a-number", "0.01") is False

    def test_invalid_cap(self):
        assert _is_under_cap("10000", "not-a-number") is False


# ---------------------------------------------------------------------------
# Gateway batching priority
# ---------------------------------------------------------------------------


class TestGatewayPriority:
    def test_gateway_beats_vanilla(self):
        opt_vanilla = PaymentOption(
            scheme="exact",
            payment_system="vanilla",
            network="base",
            network_id="eip155:8453",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        opt_gateway = PaymentOption(
            scheme="exact",
            payment_system="gateway_batching",
            network="base",
            network_id="eip155:8453",
            amount_atomic="10000",
            amount_usdc="0.01",
            asset="0xabc",
            supported_by_backend=True,
            pay_to="0xSeller",
            max_timeout_seconds=604900,
        )
        result = select_payment_option(
            (opt_vanilla, opt_gateway),
            network_preference=(),
        )
        assert result is not None
        assert result.payment_system == "gateway_batching"
