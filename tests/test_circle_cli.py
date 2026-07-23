"""Offline tests for the verified Circle CLI 0.0.6 managed payment contract.

Fixtures mirror documented output calls in @circle-fin/cli@0.0.6 dist/index.js:
blockchain/list (lines 1117-1159), wallet/list (53831-53957), wallet/status
(54417-54534), wallet/balance (50731-50832), and services/pay (48522-49366).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from hermes_x402 import (
    BuyerConfigurationError,
    CircleCliBuyerBackend,
    CircleCliClient,
    CircleCliOutputError,
    CircleCliPaymentOutcomeUnknownError,
    CircleCliRunner,
    CircleCliTimeoutError,
    CircleCliUnsupportedCapabilityError,
    CircleCliWalletNotFoundError,
    PaymentPolicy,
    PaymentPolicyError,
    X402BuyerService,
    X402Config,
    X402HermesAgent,
)
from hermes_x402.circle_cli.models import CircleCliResult

ADDRESS = "0x1111111111111111111111111111111111111111"
SELLER = "0x2222222222222222222222222222222222222222"


def result(args: tuple[str, ...], data: dict[str, Any], exit_code: int = 0) -> CircleCliResult:
    return CircleCliResult(args, exit_code, json.dumps({"data": data}), "", {"data": data})


class FakeRunner:
    read_timeout_seconds = 1
    payment_timeout_seconds = 1

    def __init__(self, *, pay_result: CircleCliResult | Exception | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.pay_result = pay_result

    async def run_text(self, args, **_: Any):
        self.calls.append(tuple(args))
        return CircleCliResult(tuple(args), 0, "circle 0.0.6\n", "", None)

    async def run_json(self, args, **_: Any):
        args = tuple(args)
        self.calls.append(args)
        if args[:2] == ("blockchain", "list"):
            return result(
                args, {"blockchains": [{"blockchain": "BASE", "name": "Base", "evmChainId": 8453}]}
            )
        if args[:2] == ("wallet", "status"):
            return result(
                args,
                {
                    "type": "agent",
                    "mainnet": {"email": "redacted@example.test", "tokenStatus": "VALID"},
                    "testnet": {"tokenStatus": "NOT_LOGGED_IN"},
                },
            )
        if args[:2] == ("wallet", "list"):
            return result(
                args, {"wallets": [{"type": "agent", "address": ADDRESS, "blockchain": "BASE"}]}
            )
        if args[:2] == ("wallet", "balance"):
            return result(
                args,
                {
                    "balances": [
                        {"amount": "1.00", "token": {"symbol": "USDC", "tokenAddress": "0x3"}}
                    ]
                },
            )
        if args[:2] == ("services", "pay"):
            if isinstance(self.pay_result, Exception):
                raise self.pay_result
            return self.pay_result or result(
                args,
                {
                    "response": {"premium": True},
                    "payment": {
                        "amount": "0.01 USDC",
                        "chain": "eip155:8453",
                        "scheme": "exact",
                        "seller": SELLER,
                        "receipt": "payment-receipt",
                    },
                },
            )
        raise AssertionError(args)


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "circle-fake"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "args = sys.argv[1:]\n"
        "if 'sleep' in args: time.sleep(5)\n"
        "if 'large' in args: print('x' * (300 * 1024)); raise SystemExit(0)\n"
        "if 'badjson' in args: print('not-json'); raise SystemExit(0)\n"
        "if args == ['--version']: print('circle 0.0.6'); raise SystemExit(0)\n"
        "print(json.dumps({'data': {'ok': True}}))\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


class TestCircleCliRunner:
    @pytest.mark.asyncio
    async def test_executes_vector_without_shell_and_uses_controlled_environment(
        self, fake_cli, monkeypatch
    ):
        monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
        runner = CircleCliRunner(executable=str(fake_cli), env={"EXPLICIT": "yes"})
        response = await runner.run_json(
            ("wallet", "status", "--output", "json"), timeout_seconds=1, operation="read"
        )
        assert response.argv == ("wallet", "status", "--output", "json")
        assert response.parsed == {"data": {"ok": True}}
        assert "EXPLICIT" not in runner._environment()
        assert "UNRELATED_SECRET" not in runner._environment()

    @pytest.mark.asyncio
    async def test_rejects_unrestricted_commands(self, fake_cli):
        runner = CircleCliRunner(executable=str(fake_cli))
        with pytest.raises(CircleCliUnsupportedCapabilityError):
            await runner.run_json(("terms", "accept"), timeout_seconds=1, operation="auth")

    @pytest.mark.asyncio
    async def test_timeout_terminates_process(self, fake_cli):
        runner = CircleCliRunner(executable=str(fake_cli))
        with pytest.raises(CircleCliTimeoutError):
            await runner.run_json(
                ("services", "pay", "sleep"), timeout_seconds=0.01, operation="payment"
            )

    @pytest.mark.asyncio
    async def test_timeout_covers_process_exit_after_streams_close(self, monkeypatch):
        import asyncio

        class ProcessThatNeverExits:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.returncode = None
                self.terminated = False
                self.killed = False
                self._exited = asyncio.Event()

            async def wait(self):
                await self._exited.wait()
                return self.returncode

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True
                self.returncode = -9
                self._exited.set()

        process = ProcessThatNeverExits()

        async def spawn(*_, **__):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("hermes_x402.circle_cli.runner._TERMINATE_GRACE_SECONDS", 0.01)
        runner = CircleCliRunner()
        with pytest.raises(CircleCliTimeoutError):
            await runner.run_json(("services", "pay"), timeout_seconds=0.01, operation="payment")
        assert process.terminated
        assert process.killed
        assert process.returncode == -9

    @pytest.mark.asyncio
    async def test_rejects_oversized_and_malformed_json_output(self, fake_cli):
        runner = CircleCliRunner(executable=str(fake_cli))
        with pytest.raises(CircleCliOutputError):
            await runner.run_json(
                ("services", "pay", "large"), timeout_seconds=2, operation="payment"
            )
        with pytest.raises(CircleCliOutputError):
            await runner.run_json(
                ("services", "pay", "badjson"), timeout_seconds=2, operation="payment"
            )


class TestCircleCliClientAndBackend:
    @pytest.mark.asyncio
    async def test_payment_is_managed_once_and_does_not_accept_protected_header(self):
        runner = FakeRunner()
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        service = X402BuyerService(backend=backend, policy=PaymentPolicy(max_usdc="1.00"))
        payment_required = {
            "x402Version": 2,
            "resource": {"url": "/premium"},
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "amount": "10000",
                    "asset": "0x3",
                    "payTo": SELLER,
                }
            ],
        }
        from unittest.mock import patch

        import httpx

        initial = httpx.Response(
            402,
            headers={
                "Payment-Required": __import__("base64")
                .b64encode(json.dumps(payment_required).encode())
                .decode()
            },
        )

        # Reuse the established project stub rather than perform any network request.
        class Stub:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def request(self, **kwargs):
                return initial

        with patch("hermes_x402.buyer.service.httpx.AsyncClient", return_value=Stub()):
            paid = await service.pay(
                "https://allowed.example/premium", headers={"Payment-Signature": "caller"}
            )
        assert paid.payment_status == "resource_succeeded"
        assert [call[:2] for call in runner.calls].count(("services", "pay")) == 1
        pay_args = next(call for call in runner.calls if call[:2] == ("services", "pay"))
        assert "Payment-Signature: caller" not in pay_args
        assert pay_args[pay_args.index("--max-amount") + 1] == "0.01"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("policy_cap", "per_call_cap", "expected_cli_cap"),
        [
            ("0.01", None, "0.01"),
            ("1.00", None, "0.01"),
            ("1.00", "0.02", "0.01"),
        ],
    )
    async def test_services_pay_cap_is_selected_accept_not_broader_cap(
        self, policy_cap, per_call_cap, expected_cli_cap
    ):
        runner = FakeRunner()
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        challenge = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "amount": "10000",
                    "asset": "0x3",
                    "payTo": SELLER,
                }
            ],
        }

        from unittest.mock import patch

        import httpx

        initial = httpx.Response(
            402,
            headers={
                "Payment-Required": __import__("base64")
                .b64encode(json.dumps(challenge).encode())
                .decode()
            },
        )

        class Stub:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def request(self, **kwargs):
                return initial

        service = X402BuyerService(backend=backend, policy=PaymentPolicy(max_usdc=policy_cap))
        with patch("hermes_x402.buyer.service.httpx.AsyncClient", return_value=Stub()):
            await service.pay("https://allowed.example/premium", max_usdc=per_call_cap)

        pay_args = next(call for call in runner.calls if call[:2] == ("services", "pay"))
        assert pay_args[pay_args.index("--max-amount") + 1] == expected_cli_cap

    @pytest.mark.asyncio
    async def test_fresh_cli_price_increase_is_blocked_by_selected_accept_cap(self):
        runner = FakeRunner(
            pay_result=result(
                ("services", "pay"),
                {
                    "response": {"premium": True},
                    "payment": {
                        "amount": "0.02 USDC",
                        "chain": "eip155:8453",
                        "scheme": "exact",
                        "seller": SELLER,
                    },
                },
            )
        )
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        challenge = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "amount": "10000",
                    "asset": "0x3",
                    "payTo": SELLER,
                }
            ],
        }

        from hermes_x402.buyer.errors import PaymentSubmissionUnknownError

        with pytest.raises(PaymentSubmissionUnknownError, match="does not match"):
            await backend.pay_and_fetch(
                url="https://allowed.example/premium",
                method="GET",
                body=None,
                headers={},
                payment_required=challenge,
                max_usdc="1.00",
            )
        pay_args = next(call for call in runner.calls if call[:2] == ("services", "pay"))
        assert pay_args[pay_args.index("--max-amount") + 1] == "0.01"

    @pytest.mark.asyncio
    async def test_multiple_material_accepts_fail_closed_before_services_pay(self):
        runner = FakeRunner()
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        await backend._ensure_ready()
        challenge = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "amount": "1",
                    "asset": "0x3",
                    "payTo": SELLER,
                },
                {
                    "scheme": "exact",
                    "network": "eip155:10",
                    "amount": "999999",
                    "asset": "0x4",
                    "payTo": SELLER,
                },
            ],
        }
        from hermes_x402.buyer.errors import InvalidPaymentChallengeError

        with pytest.raises(InvalidPaymentChallengeError, match="cannot pin an exact accept"):
            await backend.pay_and_fetch(
                url="https://allowed.example/premium",
                method="GET",
                body=None,
                headers={},
                payment_required=challenge,
                max_usdc="0.01",
            )
        assert [call[:2] for call in runner.calls].count(("services", "pay")) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "second_accept",
        [
            {"maxTimeoutSeconds": 30},
            {"sellerDefinedRestriction": "new-constraint"},
        ],
    )
    async def test_material_accepts_include_timeout_and_unknown_fields(self, second_accept):
        runner = FakeRunner()
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        await backend._ensure_ready()
        accept = {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": "10000",
            "asset": "0x3",
            "payTo": SELLER,
            "maxTimeoutSeconds": 10,
            "extra": {"name": "same"},
        }
        from hermes_x402.buyer.errors import InvalidPaymentChallengeError

        with pytest.raises(InvalidPaymentChallengeError, match="multiple materially different"):
            backend._select_safe_accept({"accepts": [accept, {**accept, **second_accept}]})

    @pytest.mark.asyncio
    async def test_exactly_equivalent_duplicate_accepts_are_allowed(self):
        runner = FakeRunner()
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        await backend._ensure_ready()
        accept = {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": "10000",
            "asset": "0x3",
            "payTo": SELLER,
            "maxTimeoutSeconds": 10,
            "extra": {"name": "same"},
        }
        assert backend._select_safe_accept({"accepts": [accept, dict(accept)]}) == accept

    def test_usdc_cap_is_decimal_canonical_and_rejects_precision_loss(self):
        assert PaymentPolicy.normalize_max_usdc("0.010000") == "0.01"
        with pytest.raises(PaymentPolicyError, match="at most 6 decimals"):
            PaymentPolicy.normalize_max_usdc("0.0000001")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"response": {}},
            {
                "payment": {
                    "amount": "0.01 USDC",
                    "chain": "eip155:8453",
                    "scheme": "exact",
                    "seller": SELLER,
                }
            },
            {
                "response": {},
                "payment": {
                    "amount": 1,
                    "chain": "eip155:8453",
                    "scheme": "exact",
                    "seller": SELLER,
                },
            },
            {
                "response": {},
                "payment": {
                    "amount": "0.01 USDC",
                    "chain": "eip155:8453",
                    "scheme": "exact",
                    "seller": SELLER,
                    "receipt": 1,
                },
            },
        ],
    )
    async def test_malformed_success_payment_output_is_ambiguous(self, data):
        runner = FakeRunner(pay_result=result(("services", "pay"), data))
        with pytest.raises(CircleCliPaymentOutcomeUnknownError):
            await CircleCliClient(runner).pay_x402(
                url="https://example.test",
                method="GET",
                body=None,
                headers={},
                wallet_address=ADDRESS,
                network="BASE",
                max_usdc="0.01",
            )

    @pytest.mark.asyncio
    async def test_timeout_is_ambiguous_and_not_retried(self):
        runner = FakeRunner(pay_result=CircleCliTimeoutError("timeout"))
        client = CircleCliClient(runner)
        with pytest.raises(CircleCliPaymentOutcomeUnknownError):
            await client.pay_x402(
                url="https://example.test",
                method="GET",
                body=None,
                headers={},
                wallet_address=ADDRESS,
                network="BASE",
                max_usdc="0.01",
            )
        assert [call[:2] for call in runner.calls].count(("services", "pay")) == 1

    @pytest.mark.asyncio
    async def test_multiple_wallets_do_not_select_implicitly(self):
        runner = FakeRunner()
        client = CircleCliClient(runner)
        with pytest.raises(CircleCliWalletNotFoundError):
            await client.verify_selected_wallet(
                wallet_address="0x9999999999999999999999999999999999999999", network="BASE"
            )

    @pytest.mark.asyncio
    async def test_policy_rejects_before_cli(self):
        runner = FakeRunner()
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        service = X402BuyerService(
            backend=backend, policy=PaymentPolicy(host_allowlist=("allowed.test",))
        )
        with pytest.raises(PaymentPolicyError):
            await service.pay("https://not-allowed.test/premium")
        assert runner.calls == []


class TestCliConfigAndAgent:
    def test_buyer_and_dual_cli_are_explicit_and_seller_is_separate(self):
        buyer = X402Config(
            role="buyer",
            buyer_backend="cli",
            circle_cli_wallet_address=ADDRESS,
            circle_cli_network="BASE",
            max_usdc_per_payment="0.01",
        )
        buyer.validate()
        dual = X402Config(
            role="dual",
            seller_address=SELLER,
            buyer_backend="cli",
            circle_cli_wallet_address=ADDRESS,
            circle_cli_network="BASE",
            max_usdc_per_payment="0.01",
            public_base_url="https://seller.example",
        )
        agent = X402HermesAgent.from_config(dual)
        assert isinstance(agent.buyer.backend, CircleCliBuyerBackend)
        assert agent.seller.seller_address == SELLER
        assert agent.buyer.wallet_address == ADDRESS

    def test_cli_rejects_custom_process_configuration(self):
        common = {
            "role": "buyer",
            "buyer_backend": "cli",
            "circle_cli_wallet_address": ADDRESS,
            "circle_cli_network": "BASE",
            "max_usdc_per_payment": "0.01",
        }
        with pytest.raises(BuyerConfigurationError, match="official 'circle'"):
            X402Config(**common, circle_cli_executable="/tmp/untrusted-circle").validate()
        with pytest.raises(BuyerConfigurationError, match="working directory"):
            X402Config(**common, circle_cli_cwd="/tmp").validate()

    @pytest.mark.parametrize("role", ["seller", "buyer", "dual"])
    def test_cli_and_dcw_credentials_cannot_mix(self, role):
        kwargs = {
            "role": role,
            "buyer_backend": "cli",
            "circle_cli_wallet_address": ADDRESS,
            "circle_cli_network": "BASE",
            "max_usdc_per_payment": "0.01",
            "wallet_id": "dcw",
        }
        if role in {"seller", "dual"}:
            kwargs["seller_address"] = SELLER
        with pytest.raises(BuyerConfigurationError):
            X402Config(**kwargs).validate()


# ---------------------------------------------------------------------------
# Chain-identity comparison: ARC-TESTNET vs eip155:5042002
# ---------------------------------------------------------------------------


class TestChainIdentityComparison:
    """Verify that configured ARC-TESTNET and reported eip155:5042002 compare
    as the same network via the centralized registry CAIP-2 resolution."""

    @staticmethod
    def _arc_testnet_runner(
        *, pay_chain: str = "eip155:5042002", pay_scheme: str = "exact"
    ) -> FakeRunner:
        """Return a FakeRunner whose blockchain list includes ARC-TESTNET."""
        runner = FakeRunner.__new__(FakeRunner)
        runner.read_timeout_seconds = 1
        runner.payment_timeout_seconds = 1
        runner.calls = []
        runner.pay_result = result(
            ("services", "pay"),
            {
                "response": {"ok": True},
                "payment": {
                    "amount": "$0.000003 USDC",
                    "chain": pay_chain,
                    "scheme": pay_scheme,
                    "seller": SELLER,
                },
            },
        )
        # Override run_json to include ARC-TESTNET in blockchain list
        _original_run_json = FakeRunner.run_json

        async def _run_json(self_inner, args, **kw):
            args = tuple(args)
            self_inner.calls.append(args)
            if args[:2] == ("blockchain", "list"):
                return result(
                    args,
                    {
                        "blockchains": [
                            {
                                "blockchain": "ARC-TESTNET",
                                "name": "Arc Testnet",
                                "evmChainId": 5042002,
                            }
                        ]
                    },
                )
            # Delegate other commands to the original
            return await _original_run_json(self_inner, args, **kw)

        runner.run_json = lambda args, **kw: _run_json(runner, args, **kw)
        return runner

    @pytest.mark.asyncio
    async def test_arc_testnet_configured_reported_caip2_accepted(self):
        """Configured ARC-TESTNET + reported eip155:5042002 → accepted."""
        runner = self._arc_testnet_runner()
        backend = CircleCliBuyerBackend(ADDRESS, "ARC-TESTNET", CircleCliClient(runner))
        challenge = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:5042002",
                    "amount": "3",
                    "asset": "0x3600000000000000000000000000000000000000",
                    "payTo": SELLER,
                }
            ],
        }
        result_obj = await backend.pay_and_fetch(
            url="https://paylabs.vercel.app/api/paylabs/brain/run",
            method="POST",
            body=None,
            headers={},
            payment_required=challenge,
            max_usdc="0.01",
        )
        assert result_obj.payment_status == "resource_succeeded"

    @pytest.mark.asyncio
    async def test_arc_alias_configured_reported_caip2_accepted(self):
        """Configured Arc alias + reported eip155:5042002 → accepted."""
        runner = self._arc_testnet_runner()
        # Use lowercase alias that the registry resolves to arcTestnet
        backend = CircleCliBuyerBackend(ADDRESS, "arcTestnet", CircleCliClient(runner))
        challenge = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:5042002",
                    "amount": "3",
                    "asset": "0x3600000000000000000000000000000000000000",
                    "payTo": SELLER,
                }
            ],
        }
        result_obj = await backend.pay_and_fetch(
            url="https://paylabs.vercel.app/api/paylabs/brain/run",
            method="POST",
            body=None,
            headers={},
            payment_required=challenge,
            max_usdc="0.01",
        )
        assert result_obj.payment_status == "resource_succeeded"

    @pytest.mark.asyncio
    async def test_base_configured_reported_arc_rejected(self):
        """Configured Base + reported Arc Testnet → rejected."""
        runner = FakeRunner(
            pay_result=result(
                ("services", "pay"),
                {
                    "response": {"ok": True},
                    "payment": {
                        "amount": "0.01 USDC",
                        "chain": "eip155:5042002",  # Arc Testnet CAIP-2
                        "scheme": "exact",
                        "seller": SELLER,
                    },
                },
            )
        )
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        challenge = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "amount": "10000",
                    "asset": "0x3",
                    "payTo": SELLER,
                }
            ],
        }
        from hermes_x402.buyer.errors import PaymentSubmissionUnknownError

        with pytest.raises(PaymentSubmissionUnknownError, match="does not match"):
            await backend.pay_and_fetch(
                url="https://allowed.example/premium",
                method="GET",
                body=None,
                headers={},
                payment_required=challenge,
                max_usdc="1.00",
            )

    @pytest.mark.asyncio
    async def test_unknown_reported_network_rejected(self):
        """Unknown reported network → rejected."""
        runner = FakeRunner(
            pay_result=result(
                ("services", "pay"),
                {
                    "response": {"ok": True},
                    "payment": {
                        "amount": "0.01 USDC",
                        "chain": "eip155:9999999",  # Unknown chain ID
                        "scheme": "exact",
                        "seller": SELLER,
                    },
                },
            )
        )
        backend = CircleCliBuyerBackend(ADDRESS, "BASE", CircleCliClient(runner))
        challenge = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "amount": "10000",
                    "asset": "0x3",
                    "payTo": SELLER,
                }
            ],
        }
        from hermes_x402.buyer.errors import PaymentSubmissionUnknownError

        with pytest.raises(PaymentSubmissionUnknownError, match="does not match"):
            await backend.pay_and_fetch(
                url="https://allowed.example/premium",
                method="GET",
                body=None,
                headers={},
                payment_required=challenge,
                max_usdc="1.00",
            )

    @pytest.mark.asyncio
    async def test_canonical_caip2_resolved_during_ensure_ready(self):
        """_ensure_ready() resolves canonical CAIP-2 through the registry."""
        runner = self._arc_testnet_runner()
        backend = CircleCliBuyerBackend(ADDRESS, "ARC-TESTNET", CircleCliClient(runner))
        await backend._ensure_ready()
        assert backend._canonical_caip2 == "eip155:5042002"
        assert backend._x402_network == "eip155:5042002"
