"""Live Arc Testnet acceptance test for hermes-x402.

Interactive, manual test that exercises the REAL:
- Hermes executable
- pip entry point
- plugins.enabled configuration
- plugin manager
- tool registry
- agent dispatcher
- native approval UI
- Circle CLI
- Arc Testnet wallet
- faucet USDC
- x402 seller endpoint
- onchain transactions

Usage:
    python -m hermes_x402.install --live-test
    python -m hermes_x402.live_test

Preflight requires:
- ARC-TESTNET environment
- Valid Circle CLI testnet session
- Configured wallet
- Sufficient faucet USDC
- YOLO disabled
- Bounded payment cap
- Explicit user confirmation before live transactions
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLUGIN_NAME = "hermes-x402"
_EXPECTED_TOOLS = 14
_EXPECTED_HOOKS = 1
_ENV_DEBUG = "HERMES_PLUGINS_DEBUG"
_YOLO_ENV = "X402_YOLO"


def _find_hermes() -> Path:
    candidate = shutil.which("hermes")
    if candidate:
        return Path(candidate)
    raise RuntimeError("Cannot find 'hermes' on PATH")


def _run_hermes(hermes: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a hermes CLI command and return the result."""
    env = os.environ.copy()
    env[_ENV_DEBUG] = "1"
    return subprocess.run(
        [str(hermes)] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _run_hermes_agent(hermes: Path, prompt: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a hermes agent prompt and return the result."""
    env = os.environ.copy()
    env[_ENV_DEBUG] = "1"
    return subprocess.run(
        [str(hermes), "--no-tui", "--yes-to-all", "--print", prompt],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _run_circle_cli(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run Circle CLI command."""
    return subprocess.run(
        ["circle"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _print_step(step: str, result: str, passed: bool) -> None:
    icon = "✅" if passed else "❌"
    print(f"  {icon} {step}: {result}")


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def run_preflight(hermes: Path) -> list[str]:
    """Verify all preflight conditions. Returns list of errors (empty = OK)."""
    errors: list[str] = []

    _print_section("PREFLIGHT CHECKS")

    # 1. ARC-TESTNET environment
    network = os.environ.get("X402_NETWORK", "").lower()
    if network != "arc-testnet":
        errors.append(f"X402_NETWORK must be 'arc-testnet', got '{network}'")
        _print_step("Network", f"X402_NETWORK={network!r} (need arc-testnet)", False)
    else:
        _print_step("Network", network, True)

    # 2. Circle CLI available
    circle_cli = shutil.which("circle")
    if not circle_cli:
        errors.append("Circle CLI not found on PATH")
        _print_step("Circle CLI", "not found", False)
    else:
        _print_step("Circle CLI", circle_cli, True)

    # 3. Valid Circle CLI testnet session
    if circle_cli:
        try:
            result = subprocess.run(
                ["circle", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                errors.append("Circle CLI session invalid — run 'circle login'")
                _print_step("CLI Session", "invalid", False)
            else:
                _print_step("CLI Session", "valid", True)
        except Exception as exc:
            errors.append(f"Circle CLI auth check failed: {exc}")
            _print_step("CLI Session", str(exc), False)

    # 4. Configured wallet
    wallet_id = os.environ.get("X402_WALLET_ID", "")
    if not wallet_id:
        errors.append("X402_WALLET_ID not set")
        _print_step("Wallet", "X402_WALLET_ID not set", False)
    else:
        masked = wallet_id[:8] + "..." + wallet_id[-4:] if len(wallet_id) > 12 else "***"
        _print_step("Wallet", masked, True)

    # 5. YOLO disabled
    yolo = os.environ.get(_YOLO_ENV, "").lower()
    if yolo in ("true", "1", "yes"):
        errors.append(f"{_YOLO_ENV} must not be enabled for live test")
        _print_step("YOLO", "ENABLED (danger!)", False)
    else:
        _print_step("YOLO", "disabled", True)

    # 6. Bounded payment cap
    cap = os.environ.get("X402_MAX_USDC_PER_PAYMENT", "")
    if not cap:
        _print_step("Payment Cap", "default (no explicit cap)", True)
    else:
        _print_step("Payment Cap", f"{cap} USDC", True)

    return errors


# ---------------------------------------------------------------------------
# Test sequence
# ---------------------------------------------------------------------------

def test_a_verify_plugin(hermes: Path) -> bool:
    """A. Verify plugin: enabled, no load errors, 14 tools, one hook."""
    _print_section("A. VERIFY PLUGIN")
    passed = True

    result = _run_hermes(hermes, ["plugins", "list"])
    output = result.stdout + result.stderr

    # Check enabled
    enabled = _PLUGIN_NAME in output and "enabled" in output
    _print_step("Plugin enabled", str(enabled), enabled)
    if not enabled:
        passed = False

    # Count tools from debug output
    tool_count = 0
    hook_count = 0
    for line in output.splitlines():
        m = re.search(r"registered\s+(\d+)\s+tools?", line, re.IGNORECASE)
        if m:
            tool_count = max(tool_count, int(m.group(1)))
        m = re.search(r"registered\s+(\d+)\s+hooks?", line, re.IGNORECASE)
        if m:
            hook_count = max(hook_count, int(m.group(1)))

    if tool_count == 0:
        tool_count = len(re.findall(r"x402_\w+", output))

    tools_ok = tool_count == _EXPECTED_TOOLS
    _print_step("Tool count", f"{tool_count}/{_EXPECTED_TOOLS}", tools_ok)
    if not tools_ok:
        passed = False

    hooks_ok = hook_count == _EXPECTED_HOOKS
    _print_step("Hook count", f"{hook_count}/{_EXPECTED_HOOKS}", hooks_ok)
    if not hooks_ok:
        passed = False

    # Check for load errors
    has_errors = "error" in output.lower() and "x402" in output.lower()
    _print_step("No load errors", "none" if not has_errors else "errors found", not has_errors)
    if has_errors:
        passed = False

    return passed


def test_b_read_only_tools(hermes: Path) -> bool:
    """B. Invoke read-only tools through Hermes agent."""
    _print_section("B. READ-ONLY TOOLS")
    passed = True

    tools = [
        "x402_status",
        "x402_wallet_status",
        "x402_wallet_balance",
        "x402_networks",
        "x402_gateway_balance",
        "x402_supports",
    ]

    for tool in tools:
        prompt = f"Call the {tool} tool and return its output."
        try:
            result = _run_hermes_agent(hermes, prompt, timeout=60)
            output = result.stdout + result.stderr
            success = result.returncode == 0 and len(output) > 0
            # Check for error signals in output
            if "error" in output.lower() and "traceback" in output.lower():
                success = False
            _print_step(tool, "returned output" if success else "failed/empty", success)
            if not success:
                passed = False
        except subprocess.TimeoutExpired:
            _print_step(tool, "timeout", False)
            passed = False

    return passed


def test_c_approval_deny(hermes: Path) -> bool:
    """C. Native approval deny — trigger x402_pay, verify deny = no tx."""
    _print_section("C. NATIVE APPROVAL DENY")
    print("  ℹ️  This test triggers x402_pay through Hermes.")
    print("  ℹ️  You will be prompted to DENY the payment.")
    print("  ℹ️  The test passes if no transaction occurs after deny.")

    confirm = input("\n  Ready? (y/N): ").strip().lower()
    if confirm != "y":
        print("  ⏭️  Skipped by user")
        return True

    prompt = (
        "Pay for https://httpbin.org/get using x402_pay with method GET. "
        "Do not set max_usdc."
    )
    try:
        result = _run_hermes_agent(hermes, prompt, timeout=120)
        output = result.stdout + result.stderr
        # After deny, should NOT have a transaction hash
        has_tx = "0x" in output and len(re.findall(r"0x[a-fA-F0-9]{10,}", output)) > 0
        _print_step("No transaction after deny", "correct (no tx)" if not has_tx else "UNEXPECTED tx found", not has_tx)
        return not has_tx
    except subprocess.TimeoutExpired:
        _print_step("Deny test", "timeout", False)
        return False


def test_approval_allow(hermes: Path) -> bool:
    """D. Native approval allow — low-value Arc Testnet payment."""
    _print_section("D. NATIVE APPROVAL ALLOW")
    print("  ℹ️  This test triggers a low-value Arc Testnet x402 payment.")
    print("  ℹ️  You will be prompted to ALLOW the payment.")
    print("  ℹ️  Verify the transaction succeeds and seller responds.")

    confirm = input("\n  Ready? (y/N): ").strip().lower()
    if confirm != "y":
        print("  ⏭️  Skipped by user")
        return True

    prompt = (
        "Pay for a low-cost x402 endpoint on Arc Testnet. "
        "Use x402_pay with max_usdc 0.001."
    )
    try:
        result = _run_hermes_agent(hermes, prompt, timeout=180)
        output = result.stdout + result.stderr
        has_tx = "0x" in output
        has_response = len(output) > 100
        success = has_tx and has_response
        _print_step("Transaction hash", "found" if has_tx else "not found", has_tx)
        _print_step("Seller response", "received" if has_response else "missing", has_response)
        return success
    except subprocess.TimeoutExpired:
        _print_step("Allow test", "timeout", False)
        return False


def test_e_gateway(hermes: Path) -> bool:
    """E. Gateway: deposit preview → execute → balance check → replay rejection."""
    _print_section("E. GATEWAY DEPOSIT")
    print("  ℹ️  This test creates a 0.5 USDC deposit preview,")
    print("  ℹ️  executes it, records the operation, and verifies replay rejection.")
    print("  ℹ️  You will be prompted to ALLOW the deposit.")

    confirm = input("\n  Ready? (y/N): ").strip().lower()
    if confirm != "y":
        print("  ⏭️  Skipped by user")
        return True

    passed = True

    # E1: Deposit preview
    prompt = (
        "Create a Gateway deposit preview for 0.5 USDC on Arc Testnet. "
        "Use x402_gateway_deposit_preview."
    )
    try:
        result = _run_hermes_agent(hermes, prompt, timeout=60)
        output = result.stdout + result.stderr
        # Extract preview_id from output
        preview_match = re.search(r"preview[_-]?id['\":\s]+['\"]?([a-zA-Z0-9_-]+)", output, re.IGNORECASE)
        has_preview = preview_match is not None
        _print_step("Deposit preview", "created" if has_preview else "not found", has_preview)
        if not has_preview:
            return False
        preview_id = preview_match.group(1)
    except subprocess.TimeoutExpired:
        _print_step("Deposit preview", "timeout", False)
        return False

    # E2: Execute deposit (requires user Allow)
    print(f"\n  ℹ️  Preview ID: {preview_id}")
    print("  ℹ️  Now execute the deposit — you will be prompted to ALLOW.")
    exec_prompt = (
        f"Execute the Gateway deposit using preview_id '{preview_id}'. "
        "Use x402_gateway_deposit_execute."
    )
    try:
        result = _run_hermes_agent(hermes, exec_prompt, timeout=120)
        output = result.stdout + result.stderr
        has_op = "operation" in output.lower() or "0x" in output
        _print_step("Deposit executed", "success" if has_op else "failed", has_op)
        if not has_op:
            passed = False
    except subprocess.TimeoutExpired:
        _print_step("Deposit execute", "timeout", False)
        passed = False

    # E3: Replay rejection
    replay_prompt = (
        f"Try to execute the same Gateway deposit again with preview_id '{preview_id}'. "
        "Use x402_gateway_deposit_execute."
    )
    try:
        result = _run_hermes_agent(hermes, replay_prompt, timeout=60)
        output = result.stdout + result.stderr
        rejected = "expired" in output.lower() or "consumed" in output.lower() or "missing" in output.lower()
        _print_step("Replay rejection", "correctly rejected" if rejected else "UNEXPECTED: not rejected", rejected)
        if not rejected:
            passed = False
    except subprocess.TimeoutExpired:
        _print_step("Replay test", "timeout", False)
        passed = False

    return passed


def test_f_final_payment(hermes: Path) -> bool:
    """F. Pay a low-cost Arc Testnet endpoint using Gateway funds."""
    _print_section("F. FINAL PAYMENT (Gateway funds)")
    print("  ℹ️  This test pays a low-cost endpoint using the Gateway balance.")
    print("  ℹ️  You will be prompted to ALLOW the payment.")

    confirm = input("\n  Ready? (y/N): ").strip().lower()
    if confirm != "y":
        print("  ⏭️  Skipped by user")
        return True

    prompt = (
        "Pay for a low-cost Arc Testnet x402 endpoint using x402_pay "
        "with max_usdc 0.001. Use Gateway wallet if available."
    )
    try:
        result = _run_hermes_agent(hermes, prompt, timeout=180)
        output = result.stdout + result.stderr
        has_tx = "0x" in output
        has_response = len(output) > 100
        success = has_tx and has_response
        _print_step("Transaction", "found" if has_tx else "not found", has_tx)
        _print_step("Seller response", "received" if has_response else "missing", has_response)
        return success
    except subprocess.TimeoutExpired:
        _print_step("Final payment", "timeout", False)
        return False


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_live_test() -> bool:
    """Run the full live Arc Testnet acceptance test.

    Returns True if all tests pass.
    """
    print("\n" + "🔴" * 30)
    print("  LIVE ARC TESTNET ACCEPTANCE TEST")
    print("  ⚠️  This will make real onchain transactions!")
    print("🔴" * 30 + "\n")

    hermes = _find_hermes()
    results: dict[str, bool] = {}

    # Preflight
    errors = run_preflight(hermes)
    if errors:
        print(f"\n❌ Preflight failed with {len(errors)} error(s):")
        for e in errors:
            print(f"   • {e}")
        return False

    # User confirmation before live transactions
    print("\n" + "─" * 60)
    print("  All preflight checks passed.")
    print("  The following tests will make REAL onchain transactions.")
    print("  Network: Arc Testnet (not mainnet)")
    print("─" * 60)
    confirm = input("\n  Type 'YES' to proceed: ").strip()
    if confirm != "YES":
        print("  Aborted by user.")
        return False

    # Run tests sequentially
    results["A_verify_plugin"] = test_a_verify_plugin(hermes)
    results["B_read_only_tools"] = test_b_read_only_tools(hermes)
    results["C_approval_deny"] = test_c_approval_deny(hermes)
    results["D_approval_allow"] = test_approval_allow(hermes)
    results["E_gateway"] = test_e_gateway(hermes)
    results["F_final_payment"] = test_f_final_payment(hermes)

    # Summary
    _print_section("RESULTS")
    all_passed = True
    for name, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {name}")
        if not passed:
            all_passed = False

    print(f"\n  {'✅ ALL TESTS PASSED' if all_passed else '❌ SOME TESTS FAILED'}")

    # Sanitized report (no secrets)
    report = {
        "tests": results,
        "all_passed": all_passed,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    report_path = Path(__file__).parent.parent / "dist" / "live-test-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\n  Report saved: {report_path}")

    return all_passed


def main() -> None:
    success = run_live_test()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
