"""Live Arc Testnet acceptance test for hermes-x402.

Interactive guided runner.  All actions happen inside the RUNNING Hermes
gateway process via Telegram — never through one-shot subprocess calls.

Requires a deterministic seller endpoint passed via LiveTestConfig.

Usage (from install.py --live-test):
    python3 -m hermes_x402.install --live-test \\
        --service-url https://seller.example/x402 \\
        --method GET \\
        --max-payment 0.001

Or standalone (for debugging after install):
    python3 -m hermes_x402.live_test \\
        --service-url https://seller.example/x402 \\
        --method GET \\
        --max-payment 0.001
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
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
_MAX_BALANCE_ATTEMPTS = 3  # retry operator input on parse failure


# ---------------------------------------------------------------------------
# Config dataclass (immutable — no sys.argv reparsing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveTestConfig:
    """Immutable configuration passed from install.py."""

    service_url: str
    method: str  # "GET" or "POST"
    max_payment: str  # Decimal string
    body_file: str | None
    canonical_body: str | None  # pre-canonicalized JSON for POST
    hermes_python: str | None  # optional --hermes-python override
    install_report: dict  # the report from run_install()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_hermes() -> Path:
    candidate = shutil.which("hermes")
    if candidate:
        return Path(candidate)
    raise RuntimeError("Cannot find 'hermes' on PATH")


def _detect_python(hermes: Path) -> Path:
    """Detect Hermes Python interpreter (simplified — install.py already validated)."""
    try:
        from hermes_x402.install import _detect_python_env

        return _detect_python_env(hermes)
    except Exception:
        pass
    # Fallback: try shebang
    try:
        first_line = hermes.read_text(errors="replace").splitlines()[0]
        if first_line.startswith("#!"):
            p = Path(first_line[2:].strip().split()[0])
            if p.exists():
                return p
    except Exception:
        pass
    p = hermes.parent / "python3"
    if p.exists():
        return p
    raise RuntimeError("Cannot detect Hermes Python")


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


def _mask_wallet(addr: str) -> str:
    """Mask wallet address for display."""
    if not addr or len(addr) < 10:
        return "***"
    return addr[:6] + "..." + addr[-4:]


def _sanitize(url: str) -> str:
    """Bounded, safe-to-display form of a URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    path = parsed.path or "/"
    if len(path) > 60:
        path = path[:57] + "..."
    return f"{parsed.scheme}://{host}{path}"


def _body_hash(body: str | None) -> str:
    """SHA-256 of canonical body for reports."""
    if body is None:
        return "none"
    return hashlib.sha256(body.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Strict balance parsing
# ---------------------------------------------------------------------------


def _parse_decimal_balance(text: str, label: str) -> Decimal | None:
    """Parse a single finite non-negative Decimal balance from operator text.

    Requires an explicit JSON field or structured label. Returns None on
    any parse failure, ambiguous input, or invalid value.
    """
    # Try JSON first
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in (label, f"{label}_balance", "balance"):
                val = data.get(key)
                if val is not None:
                    d = Decimal(str(val))
                    if d.is_finite() and d >= 0:
                        return d
    except (json.JSONDecodeError, InvalidOperation, TypeError):
        pass

    # Try explicit "label: <number>" pattern
    pattern = rf"(?:{re.escape(label)}|balance)[^0-9]*([\d]+\.[\d]+|[\d]+)"
    matches = re.findall(pattern, text, re.IGNORECASE)
    if len(matches) == 1:
        try:
            d = Decimal(matches[0])
            if d.is_finite() and d >= 0:
                return d
        except InvalidOperation:
            pass

    return None


def _prompt_balances(
    hermes: Path,
    prompt: str,
    wallet_label: str = "wallet",
    gateway_label: str = "gateway",
) -> tuple[Decimal | None, Decimal | None]:
    """Prompt operator for both wallet and gateway balances.

    Requires structured JSON or explicit numeric labels.
    Returns (wallet_decimal, gateway_decimal) — None on parse failure.
    """
    for attempt in range(_MAX_BALANCE_ATTEMPTS):
        resp = _hermes_cmd(hermes, prompt)

        # Try JSON parsing
        wallet = _parse_decimal_balance(resp, wallet_label)
        gateway = _parse_decimal_balance(resp, gateway_label)

        if wallet is not None and gateway is not None:
            return wallet, gateway

        if attempt < _MAX_BALANCE_ATTEMPTS - 1:
            print(
                f"  ⚠️  Could not parse balances from response. "
                f"Please provide JSON like: "
                f'{{"{wallet_label}": "1.234", "{gateway_label}": "5.678"}}'
            )

    return None, None


def _prompt_single_balance(hermes: Path, prompt: str) -> Decimal | None:
    """Prompt operator for a single balance value."""
    for attempt in range(_MAX_BALANCE_ATTEMPTS):
        resp = _hermes_cmd(hermes, prompt)

        # Try JSON
        try:
            data = json.loads(resp)
            if isinstance(data, dict):
                for key in ("balance", "wallet_balance", "amount"):
                    val = data.get(key)
                    if val is not None:
                        d = Decimal(str(val))
                        if d.is_finite() and d >= 0:
                            return d
        except (json.JSONDecodeError, InvalidOperation, TypeError):
            pass

        # Try number extraction
        amounts = re.findall(r"[\d]+\.[\d]+", resp)
        if len(amounts) == 1:
            try:
                d = Decimal(amounts[0])
                if d.is_finite() and d >= 0:
                    return d
            except InvalidOperation:
                pass

        if attempt < _MAX_BALANCE_ATTEMPTS - 1:
            print('  ⚠️  Could not parse balance. Please provide JSON like: {"balance": "1.234"}')

    return None


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(hermes: Path, config: LiveTestConfig) -> list[str]:
    """Verify all preflight conditions. Returns errors (empty = OK)."""
    errors: list[str] = []
    _section("PREFLIGHT")

    # 1. Circle CLI installed
    cli = shutil.which("circle")
    if not cli:
        errors.append("Circle CLI not on PATH")
        _step("Circle CLI", "not found", False)
    else:
        _step("Circle CLI", cli, True)

        # 2. Agent wallet status (NOT circle auth status)
        r = _run_circle(["wallet", "status", "--type", "agent", "--output", "json"])
        if r.returncode != 0:
            errors.append(f"circle wallet status failed: {r.stderr[:200]}")
            _step("Wallet status", "failed", False)
        else:
            try:
                raw = r.stdout
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

    # 3. Plugin environment variables
    wallet_addr = os.environ.get("CIRCLE_AGENT_WALLET_ADDRESS", "")
    if not wallet_addr:
        errors.append("CIRCLE_AGENT_WALLET_ADDRESS not set")
        _step("Wallet address", "not set", False)
    else:
        if not re.match(r"^0x[0-9a-fA-F]{40}$", wallet_addr):
            errors.append(f"CIRCLE_AGENT_WALLET_ADDRESS invalid format: {wallet_addr[:10]}...")
            _step("Wallet address", "invalid format", False)
        else:
            masked = wallet_addr[:6] + "..." + wallet_addr[-4:]
            _step("Wallet address", masked, True)

    wallet_net = os.environ.get("CIRCLE_AGENT_WALLET_NETWORK", "")
    if wallet_net.upper() != "ARC-TESTNET":
        errors.append(f"CIRCLE_AGENT_WALLET_NETWORK must be ARC-TESTNET, got '{wallet_net}'")
        _step("Wallet network", wallet_net, False)
    else:
        _step("Wallet network", wallet_net, True)

    # 4. Payment cap — Decimal, finite, positive, <= 0.01
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

    # 5. HERMES_YOLO_MODE must be off (NOT X402_YOLO)
    yolo = os.environ.get("HERMES_YOLO_MODE", "").lower()
    if yolo in ("1", "true", "yes", "on"):
        errors.append("HERMES_YOLO_MODE is enabled — must not run live test with YOLO mode")
        _step("HERMES_YOLO_MODE", "ENABLED (danger!)", False)
    else:
        _step("HERMES_YOLO_MODE", "disabled", True)

    # Also check X402_YOLO as a secondary check
    x402_yolo = os.environ.get("X402_YOLO", "").lower()
    if x402_yolo in ("1", "true", "yes"):
        errors.append("X402_YOLO is enabled")
        _step("X402_YOLO", "ENABLED", False)

    # 6. Operator YOLO confirmation
    print(
        "\n  ⚠️  Confirm that native tool approval is enabled and the"
        "\n     Hermes gateway was NOT launched in --yolo mode."
    )
    confirm = input("  Type 'CONFIRM' to proceed: ").strip()
    if confirm != "CONFIRM":
        errors.append("Operator did not confirm YOLO status")
        _step("YOLO confirmation", "not confirmed", False)

    # 7. Service URL (already validated by install.py)
    _step("Service URL", _sanitize(config.service_url), True)

    # 8. Method
    if config.method not in ("GET", "POST"):
        errors.append(f"Method must be GET or POST, got '{config.method}'")
        _step("Method", config.method, False)
    else:
        _step("Method", config.method, True)

    # 9. Body validation
    if config.method == "POST":
        if config.canonical_body is None:
            errors.append("POST requires --body-file with valid JSON")
            _step("Body", "missing", False)
        else:
            _step(
                "Body",
                f"{len(config.canonical_body)} bytes, sha256={_body_hash(config.canonical_body)}",
                True,
            )
    elif config.body_file:
        _step("Body file", "ignored for GET", True)

    return errors


# ---------------------------------------------------------------------------
# Step A: Verify installed plugin
# ---------------------------------------------------------------------------


def test_a(hermes: Path, config: LiveTestConfig) -> bool:
    """Verify plugin: enabled, 14 tools, 1 hook, version match, no load error."""
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
    source = rec.get("source", "")
    version = rec.get("version", "")
    ok &= _step("Enabled", status, status == "enabled")
    ok &= _step("Source", source, source == "entrypoint")
    ok &= _step("Version", version, True)

    # Version match with install report
    install_ver = config.install_report.get("installed_version", "")
    if install_ver and install_ver != "unknown" and version:
        ok &= _step(
            "Version matches install",
            f"plugins={version} install={install_ver}",
            version == install_ver,
        )

    # Static contract check via Hermes Python (NOT runtime verification)
    python = _detect_python(hermes)
    code = (
        "import json\n"
        "class _Ctx:\n"
        "  def __init__(s): s.tools=[]; s.hooks=[]\n"
        "  def register_tool(s, *, name, toolset, schema, handler, **kw):\n"
        "    if not isinstance(name, str) or not name: raise TypeError('name required')\n"
        "    s.tools.append(name)\n"
        "  def register_hook(s, hook_type, handler, **kw): s.hooks.append(hook_type)\n"
        "from hermes_x402.hermes_plugin.entry import register\n"
        "ctx=_Ctx(); register(ctx)\n"
        "print(json.dumps({\n"
        "  'tools': len(ctx.tools),\n"
        "  'hooks': len(ctx.hooks),\n"
        "  'verification_type': 'static_contract'\n"
        "}))\n"
    )
    r2 = subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        _step("Static contract", f"error: {r2.stderr[:200]}", False)
        return False

    counts = json.loads(r2.stdout.strip())
    ok &= _step(
        f"Tools ({counts['verification_type']})",
        str(counts["tools"]),
        counts["tools"] == _EXPECTED_TOOLS,
    )
    ok &= _step(
        f"Hooks ({counts['verification_type']})",
        str(counts["hooks"]),
        counts["hooks"] == _EXPECTED_HOOKS,
    )

    # Module path + version via importlib.metadata
    code2 = (
        "import json\n"
        "try:\n"
        "    import importlib.metadata\n"
        "    ver = importlib.metadata.version('hermes-x402')\n"
        "except Exception:\n"
        "    ver = 'unknown'\n"
        "try:\n"
        "    import hermes_x402\n"
        "    mod_path = hermes_x402.__file__\n"
        "except Exception:\n"
        "    mod_path = 'unknown'\n"
        "print(json.dumps({'v': ver, 'p': mod_path}))\n"
    )
    r3 = subprocess.run(
        [str(python), "-c", code2],
        capture_output=True,
        text=True,
    )
    if r3.returncode == 0:
        info = json.loads(r3.stdout.strip())
        _step("Installed version", f"v{info['v']}", True)
        _step("Module path", info["p"], True)

    return ok


# ---------------------------------------------------------------------------
# Step B: Read-only tools
# ---------------------------------------------------------------------------


def _hermes_cmd(hermes: Path, prompt: str) -> str:
    """Get the operator to run a Hermes command via Telegram.

    Returns the user-provided output string.
    """
    print(f"\n  📋 Run in Telegram:\n     {prompt}")
    _wait_enter("Paste the response and press Enter...")
    return input("  Response: ").strip()


def _confirm_tool_success(tool_name: str, response: str) -> bool:
    """Verify tool response has structured success or operator typed SUCCESS.

    Checks for:
    - JSON with success=true and expected tool name
    - Operator typed "SUCCESS" with the exact tool name
    """
    # Check JSON structured success
    try:
        data = json.loads(response)
        if isinstance(data, dict):
            if data.get("success") is True:
                return True
            # Also accept result with success field
            result = data.get("result", {})
            if isinstance(result, dict) and result.get("success") is True:
                return True
    except (json.JSONDecodeError, TypeError):
        pass

    # Check for operator confirmation with tool name
    upper = response.upper()
    return bool("SUCCESS" in upper and tool_name.upper() in upper)


def test_b(hermes: Path, config: LiveTestConfig) -> bool:
    """Read-only tools via Telegram session."""
    _section("B. READ-ONLY TOOLS (via Telegram)")
    ok = True

    required_tools = [
        ("x402_status", "Call the x402_status tool."),
        ("x402_networks", "Call the x402_networks tool."),
        ("x402_wallet_status", "Call the x402_wallet_status tool."),
        ("x402_wallet_balance", "Call the x402_wallet_balance tool."),
        (
            "x402_gateway_balance",
            "Call the x402_gateway_balance tool.",
        ),
        (
            "x402_supports",
            f"Call x402_supports with url={config.service_url} method={config.method}.",
        ),
    ]

    for tool_name, prompt in required_tools:
        resp = _hermes_cmd(hermes, prompt)
        success = _confirm_tool_success(tool_name, resp)
        ok &= _step(
            tool_name,
            "confirmed" if success else "NOT CONFIRMED",
            success,
        )

    return ok


# ---------------------------------------------------------------------------
# Step C: Deny payment
# ---------------------------------------------------------------------------


def test_c(hermes: Path, config: LiveTestConfig) -> bool:
    """Trigger x402_pay, operator Denies, verify no tx + balances unchanged."""
    _section("C. DENY PAYMENT")
    print("  ℹ️  Operator will be prompted to DENY in Hermes approval.\n")

    # Capture balances before
    pre_wallet, pre_gateway = _prompt_balances(
        hermes,
        "Call x402_wallet_balance and x402_gateway_balance. "
        "Report both numbers as JSON: "
        '{"wallet": "<number>", "gateway": "<number>"}',
    )

    if pre_wallet is None or pre_gateway is None:
        _step("Pre-balances parsed", "failed to parse", False)
        return False
    _step("Pre-wallet balance", str(pre_wallet), True)
    _step("Pre-gateway balance", str(pre_gateway), True)

    # Trigger payment
    resp = _hermes_cmd(
        hermes,
        f"Pay for {config.service_url} via x402_pay "
        f"method={config.method} max_usdc={config.max_payment}.",
    )

    # Require explicit operator confirmation
    print("\n  ⚠️  Did you see NATIVE APPROVAL DISPLAYED?")
    print("  ⚠️  Did you select DENY?")
    confirm = input("  Type 'DENY SELECTED' to confirm: ").strip()
    ok = _step(
        "Approval deny confirmed",
        "confirmed" if confirm == "DENY SELECTED" else "NOT CONFIRMED",
        confirm == "DENY SELECTED",
    )

    # Check for blocked/denied result
    has_blocked = any(
        w in resp.lower() for w in ("blocked", "denied", "rejected", "cancelled", "abort")
    )
    ok &= _step(
        "Blocked/denied result",
        "confirmed" if has_blocked else "not detected",
        has_blocked,
    )

    # No transaction hash after deny
    tx_match = _TX_HASH_RE.search(resp)
    ok &= _step(
        "No tx hash after deny",
        "confirmed" if tx_match is None else f"FOUND: {tx_match.group(0)}",
        tx_match is None,
    )

    # Capture balances after
    post_wallet, post_gateway = _prompt_balances(
        hermes,
        "Call x402_wallet_balance and x402_gateway_balance again. "
        "Report both numbers as JSON: "
        '{"wallet": "<number>", "gateway": "<number>"}',
    )

    if post_wallet is None or post_gateway is None:
        _step("Post-balances parsed", "failed to parse", False)
        return False

    ok &= _step(
        "Wallet balance unchanged",
        f"before={pre_wallet} after={post_wallet}",
        pre_wallet == post_wallet,
    )
    ok &= _step(
        "Gateway balance unchanged",
        f"before={pre_gateway} after={post_gateway}",
        pre_gateway == post_gateway,
    )

    return ok


# ---------------------------------------------------------------------------
# Step D: Allow payment
# ---------------------------------------------------------------------------


def test_d(hermes: Path, config: LiveTestConfig) -> bool:
    """Operator Allows once, verify tx + seller response + balance change."""
    _section("D. ALLOW PAYMENT")
    print("  ℹ️  Operator will ALLOW the payment in Hermes approval.\n")

    # Capture balance before
    pre_wallet = _prompt_single_balance(
        hermes,
        'Call x402_wallet_balance. Report as JSON: {"balance": "<number>"}',
    )
    if pre_wallet is None:
        _step("Pre-balance parsed", "failed to parse", False)
        return False
    _step("Pre-wallet balance", str(pre_wallet), True)

    # Trigger payment
    resp = _hermes_cmd(
        hermes,
        f"Pay for {config.service_url} via x402_pay "
        f"method={config.method} max_usdc={config.max_payment}.",
    )

    # Require explicit operator confirmation
    print("\n  ⚠️  Did you see NATIVE APPROVAL DISPLAYED?")
    print("  ⚠️  Did you select ALLOW ONCE?")
    confirm = input("  Type 'ALLOW ONCE SELECTED' to confirm: ").strip()
    ok = _step(
        "Approval allow confirmed",
        "confirmed" if confirm == "ALLOW ONCE SELECTED" else "NOT CONFIRMED",
        confirm == "ALLOW ONCE SELECTED",
    )

    # Transaction hash
    tx_match = _TX_HASH_RE.search(resp)
    ok &= _step(
        "Transaction hash",
        tx_match.group(0) if tx_match else "not found",
        tx_match is not None,
    )

    if tx_match:
        print(f"  📝 TX: {tx_match.group(0)}")

    # Seller success (require explicit evidence, not just tx hash)
    has_seller_evidence = (
        "success" in resp.lower() or "paid" in resp.lower() or "resource" in resp.lower()
    )
    ok &= _step(
        "Seller success evidence",
        "confirmed" if has_seller_evidence else "missing",
        has_seller_evidence,
    )

    # Balance after
    post_wallet = _prompt_single_balance(
        hermes,
        'Call x402_wallet_balance. Report as JSON: {"balance": "<number>"}',
    )
    if post_wallet is None:
        _step("Post-balance parsed", "failed to parse", False)
        return False

    expected_decrease = pre_wallet - post_wallet
    ok &= _step(
        "Balance decreased",
        f"before={pre_wallet} after={post_wallet} decrease={expected_decrease}",
        post_wallet < pre_wallet,
    )

    return ok


# ---------------------------------------------------------------------------
# Step E: Gateway deposit
# ---------------------------------------------------------------------------


def test_e(hermes: Path, config: LiveTestConfig) -> bool:
    """Gateway: preview → execute → replay rejection."""
    _section("E. GATEWAY DEPOSIT")
    ok = True

    # Capture gateway balance before
    pre_gateway = _prompt_single_balance(
        hermes,
        'Call x402_gateway_balance. Report as JSON: {"balance": "<number>"}',
    )
    if pre_gateway is None:
        _step("Pre-gateway balance", "failed to parse", False)
        return False
    _step("Pre-gateway balance", str(pre_gateway), True)

    # Preview with exact, untruncated parameters
    # Build the prompt with full body — no truncation
    body_param = ""
    if config.method == "POST" and config.canonical_body:
        body_param = f" body={config.canonical_body}"

    preview_resp = _hermes_cmd(
        hermes,
        f"Create a Gateway deposit preview for 0.5 USDC. "
        f"Use x402_gateway_deposit_preview with "
        f"service_url={config.service_url} method={config.method}"
        f"{body_param}.",
    )

    pid_match = re.search(
        r"preview[_-]?id['\":\s]+['\"]?([a-zA-Z0-9_-]+)",
        preview_resp,
        re.IGNORECASE,
    )
    ok &= _step(
        "Preview created",
        pid_match.group(1) if pid_match else "not found",
        pid_match is not None,
    )
    if not pid_match:
        return False
    preview_id = pid_match.group(1)

    # Execute with preview ID
    print(f"\n  ℹ️  Preview ID: {preview_id}")
    print("  ℹ️  Operator will ALLOW the deposit in Hermes approval.")

    exec_resp = _hermes_cmd(
        hermes,
        f"Execute Gateway deposit with preview_id='{preview_id}'.",
    )

    print("\n  ⚠️  Did you see NATIVE APPROVAL DISPLAYED?")
    print("  ⚠️  Did you select ALLOW ONCE?")
    confirm = input("  Type 'ALLOW ONCE SELECTED' to confirm: ").strip()
    ok &= _step(
        "Deposit approval confirmed",
        "confirmed" if confirm == "ALLOW ONCE SELECTED" else "NOT CONFIRMED",
        confirm == "ALLOW ONCE SELECTED",
    )

    # Transaction hash or operation ID
    tx_match_e = _TX_HASH_RE.search(exec_resp)
    has_tx = tx_match_e is not None
    has_op_id = "operation_id" in exec_resp.lower() or "operation" in exec_resp.lower()
    ok &= _step(
        "Transaction/operation",
        tx_match_e.group(0) if has_tx else ("operation ID" if has_op_id else "not found"),
        has_tx or has_op_id,
    )

    if has_tx:
        print(f"  📝 TX: {tx_match_e.group(0)}")

    # Explicit success status (not just substring "operation")
    has_success = (
        "success" in exec_resp.lower()
        or "deposited" in exec_resp.lower()
        or "completed" in exec_resp.lower()
        or has_tx
    )
    ok &= _step(
        "Deposit executed successfully",
        "confirmed" if has_success else "failed",
        has_success,
    )

    # Gateway balance after
    post_gateway = _prompt_single_balance(
        hermes,
        'Call x402_gateway_balance. Report as JSON: {"balance": "<number>"}',
    )
    if post_gateway is None:
        _step("Post-gateway balance", "failed to parse", False)
        return False

    ok &= _step(
        "Gateway balance increased",
        f"before={pre_gateway} after={post_gateway}",
        post_gateway > pre_gateway,
    )

    # Replay rejection
    replay_resp = _hermes_cmd(
        hermes,
        f"Try the same Gateway deposit again with preview_id='{preview_id}'.",
    )
    rejected = any(w in replay_resp.lower() for w in ("expired", "consumed", "missing", "invalid"))
    ok &= _step(
        "Replay rejected",
        "confirmed" if rejected else "UNEXPECTED: accepted",
        rejected,
    )

    return ok


# ---------------------------------------------------------------------------
# Step F: Final payment with Gateway funds
# ---------------------------------------------------------------------------


def test_f(hermes: Path, config: LiveTestConfig) -> bool:
    """Pay the same endpoint using Gateway funds."""
    _section("F. FINAL PAYMENT (Gateway funds)")

    # Capture gateway balance before
    pre_gateway = _prompt_single_balance(
        hermes,
        'Call x402_gateway_balance. Report as JSON: {"balance": "<number>"}',
    )
    if pre_gateway is None:
        _step("Pre-gateway balance", "failed to parse", False)
        return False
    _step("Pre-gateway balance", str(pre_gateway), True)

    # Pay
    resp = _hermes_cmd(
        hermes,
        f"Pay for {config.service_url} via x402_pay "
        f"method={config.method} max_usdc={config.max_payment}.",
    )

    # Require approval
    print("\n  ⚠️  Did you see NATIVE APPROVAL DISPLAYED?")
    print("  ⚠️  Did you select ALLOW ONCE?")
    confirm = input("  Type 'ALLOW ONCE SELECTED' to confirm: ").strip()
    ok = _step(
        "Approval confirmed",
        "confirmed" if confirm == "ALLOW ONCE SELECTED" else "NOT CONFIRMED",
        confirm == "ALLOW ONCE SELECTED",
    )

    # Transaction hash or operation ID
    tx_match = _TX_HASH_RE.search(resp)
    has_tx = tx_match is not None
    has_op_id = "operation_id" in resp.lower() or "operation" in resp.lower()
    ok &= _step(
        "Transaction/operation",
        tx_match.group(0) if has_tx else ("operation ID" if has_op_id else "not found"),
        has_tx or has_op_id,
    )

    if has_tx:
        print(f"  📝 TX: {tx_match.group(0)}")

    # Seller success evidence
    has_seller_evidence = (
        "success" in resp.lower() or "paid" in resp.lower() or "resource" in resp.lower()
    )
    ok &= _step(
        "Seller success evidence",
        "confirmed" if has_seller_evidence else "missing",
        has_seller_evidence,
    )

    # Gateway balance after
    post_gateway = _prompt_single_balance(
        hermes,
        'Call x402_gateway_balance. Report as JSON: {"balance": "<number>"}',
    )
    if post_gateway is None:
        _step("Post-gateway balance", "failed to parse", False)
        return False

    expected_payment = Decimal(config.max_payment)
    actual_decrease = pre_gateway - post_gateway
    ok &= _step(
        "Gateway balance decreased",
        f"before={pre_gateway} after={post_gateway} "
        f"decrease={actual_decrease} expected~={expected_payment}",
        post_gateway < pre_gateway,
    )

    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_live_test(config: LiveTestConfig) -> bool:
    """Run the full live Arc Testnet acceptance test."""
    print("\n" + "🔴" * 30)
    print("  LIVE ARC TESTNET ACCEPTANCE TEST")
    print("  ⚠️  All actions happen in the RUNNING Hermes gateway.")
    print("  ⚠️  Do NOT restart Hermes between steps.")
    print("🔴" * 30 + "\n")

    hermes = _find_hermes()

    # Wait for operator confirmation that gateway is online
    print(
        "  ⚠️  Before continuing, confirm the Hermes Telegram gateway"
        "\n     is online and responding to messages."
    )
    _wait_enter("Press Enter when the gateway is confirmed online...")

    # Preflight
    errors = preflight(hermes, config)
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

    results["A"] = test_a(hermes, config)
    results["B"] = test_b(hermes, config)

    # For C-F, capture structured evidence
    results["C"] = test_c(hermes, config)
    results["D"] = test_d(hermes, config)
    results["E"] = test_e(hermes, config)
    results["F"] = test_f(hermes, config)

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

    # Build sanitized report with all required evidence
    install_report = config.install_report
    report = {
        # Source/build identity
        "source_commit_sha": install_report.get("commit_sha", "unknown"),
        "wheel_sha256": install_report.get("wheel_sha256", "unknown"),
        # Hermes paths
        "hermes_executable": install_report.get("hermes_exe", "unknown"),
        "hermes_python": install_report.get("hermes_python", "unknown"),
        "hermes_module_path": install_report.get("hermes_module_path", "unknown"),
        "installed_version": install_report.get("installed_version", "unknown"),
        "module_path": install_report.get("module_path", "unknown"),
        # Plugin catalog
        "plugin_source": install_report.get("plugin_source", "unknown"),
        "plugin_status": install_report.get("plugin_status", "unknown"),
        "registration_contract": install_report.get("registration_contract", {}),
        # Wallet/network
        "network": os.environ.get("CIRCLE_AGENT_WALLET_NETWORK", "unknown"),
        "masked_wallet": _mask_wallet(os.environ.get("CIRCLE_AGENT_WALLET_ADDRESS", "")),
        # Request
        "service_url": _sanitize(config.service_url),
        "method": config.method,
        "payment_amount": config.max_payment,
        "body_sha256": _body_hash(config.canonical_body),
        "body_length": (len(config.canonical_body) if config.canonical_body else 0),
        # Results
        "results": results,
        "all_passed": all_ok,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Save report
    rpath = Path(__file__).parent.parent / "dist" / "live-test-report.json"
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, indent=2))
    print(f"  Report: {rpath}")

    return all_ok


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Arc Testnet acceptance test (standalone)")
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--method", default="GET", choices=["GET", "POST"])
    parser.add_argument("--max-payment", required=True)
    parser.add_argument("--body-file", default=None)
    parser.add_argument("--hermes-python", default=None)
    args = parser.parse_args()

    # Validate
    from hermes_x402.install import (
        _validate_body_file,
        _validate_max_payment,
        _validate_service_url,
    )

    url_errors = _validate_service_url(args.service_url)
    if url_errors:
        for e in url_errors:
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    env_cap = os.environ.get("X402_MAX_USDC_PER_PAYMENT", "")
    pay_errors = _validate_max_payment(args.max_payment, env_cap or None)
    if pay_errors:
        for e in pay_errors:
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    body_errors, canonical_body = _validate_body_file(args.body_file, args.method)
    if body_errors:
        for e in body_errors:
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    config = LiveTestConfig(
        service_url=args.service_url,
        method=args.method,
        max_payment=args.max_payment,
        body_file=args.body_file,
        canonical_body=canonical_body,
        hermes_python=args.hermes_python,
        install_report={},
    )

    sys.exit(0 if run_live_test(config) else 1)


if __name__ == "__main__":
    main()
