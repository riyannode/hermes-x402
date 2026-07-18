"""Live Arc Testnet acceptance test for hermes-x402.

Interactive guided runner.  All actions happen inside the RUNNING Hermes
gateway process via Telegram — never through one-shot subprocess calls.

Requires a deterministic seller endpoint passed as CLI arguments.

Usage:
    python -m hermes_x402.live_test \\
        --service-url https://your-arc-testnet-seller.example/x402 \\
        --method GET \\
        --max-payment 0.001

    python -m hermes_x402.install --live-test  (runs install first)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLUGIN_NAME = "hermes-x402"
_EXPECTED_TOOLS = 14
_EXPECTED_HOOKS = 1
_TX_HASH_RE = re.compile(r"0x[a-fA-F0-9]{64}")
_ENV_DEBUG = "HERMES_PLUGINS_DEBUG"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_hermes() -> Path:
    candidate = shutil.which("hermes")
    if candidate:
        return Path(candidate)
    raise RuntimeError("Cannot find 'hermes' on PATH")


def _run_hermes(hermes: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env[_ENV_DEBUG] = "1"
    return subprocess.run(
        [str(hermes)] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _run_circle(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["circle"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _step(label: str, detail: str, ok: bool) -> bool:
    icon = "✅" if ok else "❌"
    print(f"  {icon} {label}: {detail}")
    return ok


def _wait_enter(msg: str = "Press Enter to continue...") -> None:
    input(f"\n  {msg}")


def _decimal(s: str) -> Decimal:
    return Decimal(s)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(hermes: Path, args: argparse.Namespace) -> list[str]:
    """Verify all preflight conditions. Returns errors (empty = OK)."""
    errors: list[str] = []
    _section("PREFLIGHT")

    # 1. Circle CLI session
    cli = shutil.which("circle")
    if not cli:
        errors.append("Circle CLI not on PATH")
        _step("Circle CLI", "not found", False)
    else:
        r = _run_circle(["auth", "status"])
        ok = r.returncode == 0
        _step("Circle CLI session", "valid" if ok else "invalid", ok)
        if not ok:
            errors.append("Circle CLI session invalid — run circle login")

    # 2. Wallet status via circle wallet status --type agent --output json
    r = _run_circle(["wallet", "status", "--type", "agent", "--output", "json"])
    if r.returncode != 0:
        errors.append(f"circle wallet status failed: {r.stderr[:200]}")
        _step("Wallet status", "failed", False)
    else:
        try:
            raw = r.stdout
            # Strip node warnings (lines before JSON)
            json_start = raw.index("{")
            data = json.loads(raw[json_start:])
            testnet = data.get("data", {}).get("testnet", {})
            token_status = testnet.get("tokenStatus", "")
            ok = token_status == "VALID"
            _step("Wallet testnet", f"tokenStatus={token_status}", ok)
            if not ok:
                errors.append(f"Wallet testnet tokenStatus={token_status}, need VALID")
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            errors.append(f"Failed to parse wallet status: {exc}")
            _step("Wallet status parse", str(exc), False)

    # 3. Wallet address
    wallet_addr = os.environ.get("CIRCLE_AGENT_WALLET_ADDRESS", "")
    if not wallet_addr:
        errors.append("CIRCLE_AGENT_WALLET_ADDRESS not set")
        _step("Wallet address", "not set", False)
    else:
        masked = wallet_addr[:6] + "..." + wallet_addr[-4:] if len(wallet_addr) > 10 else "***"
        _step("Wallet address", masked, True)

    # 4. Wallet network
    wallet_net = os.environ.get("CIRCLE_AGENT_WALLET_NETWORK", "")
    if wallet_net.upper() != "ARC-TESTNET":
        errors.append(f"CIRCLE_AGENT_WALLET_NETWORK must be ARC-TESTNET, got '{wallet_net}'")
        _step("Wallet network", wallet_net, False)
    else:
        _step("Wallet network", wallet_net, True)

    # 5. Payment cap — must be Decimal, finite, positive, <= 0.01
    cap_str = os.environ.get("X402_MAX_USDC_PER_PAYMENT", "")
    if not cap_str:
        errors.append("X402_MAX_USDC_PER_PAYMENT not set")
        _step("Payment cap", "not set", False)
    else:
        try:
            cap = _decimal(cap_str)
            if cap.is_nan() or cap.is_infinite():
                raise InvalidOperation("not finite")
            if cap <= 0:
                raise InvalidOperation("not positive")
            if cap > Decimal("0.01"):
                errors.append(f"Payment cap {cap} > 0.01 USDC max for live test")
                _step("Payment cap", f"{cap} USDC (too high)", False)
            else:
                _step("Payment cap", f"{cap} USDC", True)
        except InvalidOperation:
            errors.append(f"X402_MAX_USDC_PER_PAYMENT not a valid Decimal: '{cap_str}'")
            _step("Payment cap", f"invalid: '{cap_str}'", False)

    # 6. YOLO must be off
    yolo = os.environ.get("X402_YOLO", "").lower()
    if yolo in ("true", "1", "yes"):
        errors.append("X402_YOLO must not be enabled")
        _step("YOLO", "ENABLED (danger!)", False)
    else:
        _step("YOLO", "disabled", True)

    # 7. Service URL provided
    if not args.service_url:
        errors.append("--service-url is required")
        _step("Service URL", "not provided", False)
    else:
        _step("Service URL", args.service_url, True)

    # 8. Method
    if args.method not in ("GET", "POST"):
        errors.append(f"--method must be GET or POST, got '{args.method}'")
        _step("Method", args.method, False)
    else:
        _step("Method", args.method, True)

    # 9. Body file if POST
    if args.method == "POST" and args.body_file:
        bp = Path(args.body_file)
        if not bp.exists():
            errors.append(f"Body file not found: {bp}")
            _step("Body file", str(bp), False)
        else:
            _step("Body file", str(bp), True)

    return errors


# ---------------------------------------------------------------------------
# Test A: Verify installed plugin
# ---------------------------------------------------------------------------


def test_a(hermes: Path) -> bool:
    """Verify plugin: enabled, 14 tools, 1 hook, no load error."""
    _section("A. VERIFY INSTALLED PLUGIN")
    ok = True

    # JSON status
    r = _run_hermes(hermes, ["plugins", "list", "--json"])
    if r.returncode != 0:
        _step("plugins list rc", str(r.returncode), False)
        return False

    try:
        records = json.loads(r.stdout)
    except json.JSONDecodeError:
        _step("JSON parse", "failed", False)
        return False

    rec = next((p for p in records if p.get("name") == _PLUGIN_NAME), None)
    if rec is None:
        _step("Plugin found", "no", False)
        return False

    status = rec.get("status", "")
    version = rec.get("version", "")
    ok &= _step("Enabled", status, status == "enabled")
    ok &= _step("Version", version, True)

    # Tools + hooks via Hermes Python
    python = _detect_python(hermes)
    code = (
        "import json\n"
        "class _Ctx:\n"
        "  def __init__(s): s.tools=[]; s.hooks=[]\n"
        "  def register_tool(s,n,ts,sc,h,**k): s.tools.append(n)\n"
        "  def register_hook(s,ht,h): s.hooks.append(ht)\n"
        "from hermes_x402.hermes_plugin.entry import register\n"
        "ctx=_Ctx(); register(ctx)\n"
        "print(json.dumps({'t':len(ctx.tools),'h':len(ctx.hooks)}))\n"
    )
    r2 = subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        _step("Plugin load", f"error: {r2.stderr[:200]}", False)
        return False

    counts = json.loads(r2.stdout.strip())
    ok &= _step("Tools", str(counts["t"]), counts["t"] == _EXPECTED_TOOLS)
    ok &= _step("Hooks", str(counts["h"]), counts["h"] == _EXPECTED_HOOKS)

    # Module path + version
    code2 = (
        "import json, hermes_x402\n"
        "print(json.dumps({\n"
        "  'v': getattr(hermes_x402,'__version__','?'),\n"
        "  'p': hermes_x402.__file__,\n"
        "}))\n"
    )
    r3 = subprocess.run(
        [str(python), "-c", code2],
        capture_output=True,
        text=True,
    )
    if r3.returncode == 0:
        info = json.loads(r3.stdout.strip())
        _step("Module", f"v{info['v']} at {info['p']}", True)

    return ok


def _detect_python(hermes: Path) -> Path:
    first_line = hermes.read_text().splitlines()[0]
    if first_line.startswith("#!"):
        p = Path(first_line[2:].strip())
        if p.exists():
            return p
    p = hermes.parent / "python3"
    if p.exists():
        return p
    raise RuntimeError("Cannot detect Hermes Python")


# ---------------------------------------------------------------------------
# Test B: Read-only tools
# ---------------------------------------------------------------------------


def _hermes_cmd(hermes: Path, prompt: str) -> str:
    """Get the operator to run a Hermes command via Telegram.

    Returns the user-provided output string.
    """
    print(f"\n  📋 Run in Telegram:\n     {prompt}")
    _wait_enter("Paste the response and press Enter...")
    return input("  Response: ").strip()


def test_b(hermes: Path, args: argparse.Namespace) -> bool:
    """Read-only tools via Telegram session."""
    _section("B. READ-ONLY TOOLS (via Telegram)")
    ok = True

    prompts = [
        ("x402_status", "Call the x402_status tool."),
        ("x402_wallet_status", "Call the x402_wallet_status tool."),
        ("x402_wallet_balance", "Call the x402_wallet_balance tool."),
        ("x402_gateway_balance", "Call the x402_gateway_balance tool."),
        ("x402_supports", f"Call x402_supports with url={args.service_url} method={args.method}."),
    ]

    for name, prompt in prompts:
        resp = _hermes_cmd(hermes, prompt)
        has_output = len(resp) > 10
        ok &= _step(name, "response received" if has_output else "empty", has_output)

    return ok


# ---------------------------------------------------------------------------
# Test C: Deny payment
# ---------------------------------------------------------------------------


def test_c(hermes: Path, args: argparse.Namespace) -> bool:
    """Trigger x402_pay, operator Denies, verify no tx."""
    _section("C. DENY PAYMENT")
    print("  ℹ️  Operator will be prompted to DENY in Hermes approval.")

    pre_balance = _hermes_cmd(
        hermes,
        "Call x402_wallet_balance and x402_gateway_balance. Report both numbers.",
    )

    resp = _hermes_cmd(
        hermes,
        f"Pay for {args.service_url} via x402_pay "
        f"method={args.method} max_usdc={args.max_payment}.",
    )

    has_tx = bool(_TX_HASH_RE.search(resp))
    ok = _step(
        "No tx hash after deny", "confirmed" if not has_tx else f"FOUND: {has_tx}", not has_tx
    )

    post_balance = _hermes_cmd(
        hermes,
        "Call x402_wallet_balance and x402_gateway_balance again. Report both numbers.",
    )
    balances_unchanged = pre_balance.strip() == post_balance.strip()
    ok &= _step(
        "Balances unchanged", "confirmed" if balances_unchanged else "CHANGED", balances_unchanged
    )

    return ok


# ---------------------------------------------------------------------------
# Test D: Allow payment
# ---------------------------------------------------------------------------


def test_d(hermes: Path, args: argparse.Namespace) -> bool:
    """Operator Allows once, verify tx + seller response."""
    _section("D. ALLOW PAYMENT")
    print("  ℹ️  Operator will ALLOW the payment in Hermes approval.")

    resp = _hermes_cmd(
        hermes,
        f"Pay for {args.service_url} via x402_pay "
        f"method={args.method} max_usdc={args.max_payment}.",
    )

    tx_match = _TX_HASH_RE.search(resp)
    ok = _step(
        "Transaction hash", tx_match.group(0) if tx_match else "not found", tx_match is not None
    )

    has_seller = len(resp) > 50
    ok &= _step("Seller response", "received" if has_seller else "missing", has_seller)

    if tx_match:
        print(f"  📝 TX: {tx_match.group(0)}")

    post = _hermes_cmd(
        hermes,
        "Call x402_wallet_balance. Report the number.",
    )
    _step("Balance after payment", post, True)

    return ok


# ---------------------------------------------------------------------------
# Test E: Gateway deposit
# ---------------------------------------------------------------------------


def test_e(hermes: Path, args: argparse.Namespace) -> bool:
    """Gateway: preview → execute → replay rejection."""
    _section("E. GATEWAY DEPOSIT")
    ok = True

    # Preview
    preview_resp = _hermes_cmd(
        hermes,
        "Create a Gateway deposit preview for 0.5 USDC. Use x402_gateway_deposit_preview.",
    )

    pid_match = re.search(
        r"preview[_-]?id['\":\s]+['\"]?([a-zA-Z0-9_-]+)",
        preview_resp,
        re.IGNORECASE,
    )
    ok &= _step(
        "Preview created", pid_match.group(1) if pid_match else "not found", pid_match is not None
    )
    if not pid_match:
        return False
    preview_id = pid_match.group(1)

    # Execute
    print(f"\n  ℹ️  Preview ID: {preview_id}")
    print("  ℹ️  Operator will ALLOW the deposit in Hermes approval.")
    exec_resp = _hermes_cmd(
        hermes,
        f"Execute Gateway deposit with preview_id='{preview_id}'.",
    )
    has_op = "operation" in exec_resp.lower() or bool(_TX_HASH_RE.search(exec_resp))
    ok &= _step("Deposit executed", "success" if has_op else "failed", has_op)

    # Replay rejection
    replay_resp = _hermes_cmd(
        hermes,
        f"Try the same Gateway deposit again with preview_id='{preview_id}'.",
    )
    rejected = any(w in replay_resp.lower() for w in ("expired", "consumed", "missing", "invalid"))
    ok &= _step("Replay rejected", "confirmed" if rejected else "UNEXPECTED: accepted", rejected)

    return ok


# ---------------------------------------------------------------------------
# Test F: Final payment with Gateway funds
# ---------------------------------------------------------------------------


def test_f(hermes: Path, args: argparse.Namespace) -> bool:
    """Pay the same endpoint using Gateway funds."""
    _section("F. FINAL PAYMENT (Gateway funds)")

    resp = _hermes_cmd(
        hermes,
        f"Pay for {args.service_url} via x402_pay "
        f"method={args.method} max_usdc={args.max_payment}.",
    )

    tx_match = _TX_HASH_RE.search(resp)
    ok = _step(
        "Transaction hash", tx_match.group(0) if tx_match else "not found", tx_match is not None
    )
    ok &= _step("Seller response", "received" if len(resp) > 50 else "missing", len(resp) > 50)

    if tx_match:
        print(f"  📝 TX: {tx_match.group(0)}")

    post = _hermes_cmd(
        hermes,
        "Call x402_wallet_balance. Report the number.",
    )
    _step("Final balance", post, True)

    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_live_test() -> bool:
    """Run the full live Arc Testnet acceptance test."""
    print("\n" + "🔴" * 30)
    print("  LIVE ARC TESTNET ACCEPTANCE TEST")
    print("  ⚠️  All actions happen in the RUNNING Hermes gateway.")
    print("  ⚠️  Do NOT restart Hermes between steps.")
    print("🔴" * 30 + "\n")

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--method", default="GET", choices=["GET", "POST"])
    parser.add_argument("--max-payment", required=True)
    parser.add_argument("--body-file", default=None)
    # Re-parse from sys.argv (skip install.py args)
    known, _ = parser.parse_known_args()
    args = known

    # Validate max_payment
    try:
        cap = _decimal(args.max_payment)
        if cap.is_nan() or cap.is_infinite() or cap <= 0:
            raise InvalidOperation("bad")
        if cap > Decimal("0.01"):
            print(f"  ❌ max-payment {cap} > 0.01 USDC limit")
            return False
    except InvalidOperation:
        print(f"  ❌ max-payment not a valid positive Decimal: '{args.max_payment}'")
        return False

    hermes = _find_hermes()

    # Preflight
    errors = preflight(hermes, args)
    if errors:
        print(f"\n❌ Preflight failed ({len(errors)} error(s)):")
        for e in errors:
            print(f"   • {e}")
        return False

    # Confirmation
    print("\n" + "─" * 60)
    print("  All preflight checks passed.")
    print("  The next steps require the RUNNING Hermes Telegram gateway.")
    print("  Do NOT restart Hermes between steps.")
    print("─" * 60)
    confirm = input("\n  Type 'YES' to proceed: ").strip()
    if confirm != "YES":
        print("  Aborted.")
        return False

    # Run A-F
    results: dict[str, bool] = {}
    results["A"] = test_a(hermes)
    results["B"] = test_b(hermes, args)
    results["C"] = test_c(hermes, args)
    results["D"] = test_d(hermes, args)
    results["E"] = test_e(hermes, args)
    results["F"] = test_f(hermes, args)

    # Summary
    _section("RESULTS")
    all_ok = True
    for step, passed in results.items():
        icon = "✅" if passed else "❌"
        status = "PASS" if passed else "FAIL"
        print(f"  {icon} Step {step}: {status}")
        if not passed:
            all_ok = False

    verdict = "✅ ALL STEPS PASSED" if all_ok else "❌ INCOMPLETE"
    print(f"\n  {verdict}")

    # Save sanitized report
    report = {
        "results": results,
        "all_passed": all_ok,
        "service_url": args.service_url,
        "method": args.method,
        "max_payment": args.max_payment,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    rpath = Path(__file__).parent.parent / "dist" / "live-test-report.json"
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, indent=2))
    print(f"  Report: {rpath}")

    return all_ok


def main() -> None:
    sys.exit(0 if run_live_test() else 1)


if __name__ == "__main__":
    main()
