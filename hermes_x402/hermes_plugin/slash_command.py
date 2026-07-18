"""Slash command handler for /x402.

Provides read-only status/discovery and safe configuration via a single
Hermes slash command. Dispatches to existing tools for read-only operations.

Supported syntax:
  /x402
  /x402 help
  /x402 status
  /x402 wallet
  /x402 balance
  /x402 gateway
  /x402 networks
  /x402 supports <https-url>
  /x402 configure
  /x402 configure preview buyer cli <wallet> ARC-TESTNET <max_usdc>
  /x402 configure apply buyer cli <wallet> ARC-TESTNET <max_usdc>
"""

from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hermes_x402.hermes_plugin.output import safe_wallet_address

# Wallet address pattern: 0x + 40 hex chars
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Managed keys written by configure apply
_MANAGED_KEYS_ORDER = [
    "X402_ROLE",
    "X402_BUYER_BACKEND",
    "CIRCLE_AGENT_WALLET_ADDRESS",
    "CIRCLE_AGENT_WALLET_NETWORK",
    "X402_MAX_USDC_PER_PAYMENT",
    "X402_NETWORK_POLICY",
    "X402_HOST_ALLOWLIST",
    "X402_REQUIRE_GATEWAY_BATCHING",
    "X402_ALLOW_HTTP",
    "X402_ALLOW_CHAT_OTP",
]

_HELP_TEXT = """\
/x402 — x402 plugin commands

Usage:
  /x402 help                              Show this help
  /x402 status                            Plugin status and configuration
  /x402 wallet                            Circle wallet + readiness status
  /x402 balance                           Wallet USDC balance
  /x402 gateway                           Gateway balance
  /x402 networks                          Supported networks
  /x402 supports <url>                    Check if URL supports x402
  /x402 configure                         Show configuration state
  /x402 configure preview buyer cli <wallet> ARC-TESTNET <max_usdc>
  /x402 configure apply buyer cli <wallet> ARC-TESTNET <max_usdc>

Read-only commands dispatch to existing tools.
Configure is safe — preview never writes, apply writes only managed keys."""


def _mask_wallet(addr: str) -> str:
    """Mask wallet address for safe display."""
    return safe_wallet_address(addr)


def _resolve_hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "")
    if home:
        return Path(home).resolve()
    return Path.home().resolve() / ".hermes"


def _read_managed_keys(env_path: Path) -> dict[str, str]:
    """Read current managed key values from .env."""
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key in _MANAGED_KEYS_ORDER:
                result[key] = value.strip()
    return result


def _check_cli_available() -> dict[str, Any]:
    """Check Circle CLI availability."""
    import shutil
    import subprocess

    circle_path = shutil.which("circle")
    if circle_path is None:
        return {"available": False, "version": None, "executable": None}

    try:
        r = subprocess.run(
            [circle_path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        version = r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        version = None

    return {
        "available": True,
        "version": version,
        "executable": circle_path,
    }


def handle_x402_command(raw_args: str, ctx: Any) -> str:
    """Handle /x402 slash command.

    Args:
        raw_args: Raw argument string after /x402.
        ctx: Hermes plugin context with dispatch_tool.

    Returns:
        Plain-text response string.
    """
    args = raw_args.strip()
    parts = args.split() if args else []
    subcommand = parts[0].lower() if parts else ""

    # --- help / empty ---
    if not subcommand or subcommand == "help":
        return _HELP_TEXT

    # --- status ---
    if subcommand == "status":
        return ctx.dispatch_tool("x402_status", {})

    # --- wallet ---
    if subcommand == "wallet":
        return ctx.dispatch_tool("x402_wallet_status", {})

    # --- balance ---
    if subcommand == "balance":
        return ctx.dispatch_tool("x402_wallet_balance", {})

    # --- gateway ---
    if subcommand == "gateway":
        return ctx.dispatch_tool("x402_gateway_balance", {})

    # --- networks ---
    if subcommand == "networks":
        return ctx.dispatch_tool("x402_networks", {})

    # --- supports ---
    if subcommand == "supports":
        if len(parts) < 2:
            return "Usage: /x402 supports <https-url>"
        url = parts[1]
        if not url.startswith("https://"):
            return "Error: supports requires an HTTPS URL."
        return ctx.dispatch_tool("x402_supports", {"url": url})

    # --- configure ---
    if subcommand == "configure":
        return _handle_configure(parts[1:], ctx)

    return f"Unknown subcommand: {subcommand!r}. Use '/x402 help' for usage."


def _handle_configure(subparts: list[str], ctx: Any) -> str:
    """Handle /x402 configure subcommand."""
    if not subparts:
        return _handle_configure_show()

    action = subparts[0].lower()

    if action == "preview":
        return _handle_configure_preview(subparts[1:])
    if action == "apply":
        return _handle_configure_apply(subparts[1:])

    return f"Unknown configure action: {action!r}. Use 'preview' or 'apply'."


def _handle_configure_show() -> str:
    """Show current configuration state (read-only)."""
    cli_info = _check_cli_available()
    env_path = _resolve_hermes_home() / ".env"
    managed = _read_managed_keys(env_path)

    role = managed.get("X402_ROLE", "")
    backend = managed.get("X402_BUYER_BACKEND", "")
    wallet = managed.get("CIRCLE_AGENT_WALLET_ADDRESS", "")
    network = managed.get("CIRCLE_AGENT_WALLET_NETWORK", "")
    max_usdc = managed.get("X402_MAX_USDC_PER_PAYMENT", "")

    is_configured = bool(role and backend and wallet and network and max_usdc)

    lines = [
        "=== /x402 configure ===",
        "",
        f"Circle CLI: {'available' if cli_info['available'] else 'not found'}",
    ]

    if cli_info["available"]:
        lines.append(f"  Version: {cli_info['version'] or 'unknown'}")
        lines.append(f"  Executable: {cli_info['executable']}")

    lines.append("")
    lines.append(f"Configured: {'yes' if is_configured else 'no'}")

    if is_configured:
        lines.append(f"  Role: {role}")
        lines.append(f"  Backend: {backend}")
        lines.append(f"  Wallet: {_mask_wallet(wallet)}")
        lines.append(f"  Network: {network}")
        lines.append(f"  Max USDC/payment: {max_usdc}")
    else:
        missing = []
        if not role:
            missing.append("X402_ROLE")
        if not backend:
            missing.append("X402_BUYER_BACKEND")
        if not wallet:
            missing.append("CIRCLE_AGENT_WALLET_ADDRESS")
        if not network:
            missing.append("CIRCLE_AGENT_WALLET_NETWORK")
        if not max_usdc:
            missing.append("X402_MAX_USDC_PER_PAYMENT")
        if missing:
            lines.append(f"  Missing: {', '.join(missing)}")

    lines.append("")
    lines.append("Usage:")
    lines.append("  /x402 configure preview buyer cli <wallet> ARC-TESTNET <max_usdc>")
    lines.append("  /x402 configure apply buyer cli <wallet> ARC-TESTNET <max_usdc>")

    return "\n".join(lines)


def _validate_configure_args(
    parts: list[str],
) -> tuple[dict[str, str] | None, str | None]:
    """Validate configure preview/apply arguments.

    Expected: buyer cli <wallet> ARC-TESTNET <max_usdc>
    Returns (validated_params, error_message).
    """
    if len(parts) < 5:
        return None, (
            "Usage: /x402 configure <preview|apply> buyer cli <wallet> ARC-TESTNET <max_usdc>"
        )

    role = parts[0].lower()
    backend = parts[1].lower()
    wallet = parts[2]
    network = parts[3].upper()
    max_usdc_str = parts[4]

    # Validate role
    if role != "buyer":
        return None, f"Invalid role: {role!r}. Only 'buyer' is supported."

    # Validate backend
    if backend != "cli":
        return None, f"Invalid backend: {backend!r}. Only 'cli' is supported."

    # Validate wallet
    if not _WALLET_RE.match(wallet):
        return None, (
            f"Invalid wallet address: {wallet!r}. "
            "Must be 0x + 40 hexadecimal characters."
        )

    # Validate network
    if network != "ARC-TESTNET":
        return None, f"Invalid network: {network!r}. Only 'ARC-TESTNET' is supported."

    # Validate max_usdc
    try:
        max_usdc = Decimal(max_usdc_str)
    except (InvalidOperation, ValueError):
        return None, f"Invalid max_usdc: {max_usdc_str!r}. Must be a valid Decimal."

    if not max_usdc.is_finite() or max_usdc <= 0:
        return None, f"Invalid max_usdc: {max_usdc_str!r}. Must be positive and finite."

    return {
        "role": role,
        "backend": backend,
        "wallet": wallet,
        "network": network,
        "max_usdc": str(max_usdc),
    }, None


def _handle_configure_preview(parts: list[str]) -> str:
    """Show proposed configuration (read-only, never writes)."""
    params, error = _validate_configure_args(parts)
    if error:
        return f"Error: {error}"

    assert params is not None
    lines = [
        "=== Configuration Preview ===",
        "",
        "Proposed managed keys:",
        f"  X402_ROLE={params['role']}",
        f"  X402_BUYER_BACKEND={params['backend']}",
        f"  CIRCLE_AGENT_WALLET_ADDRESS={_mask_wallet(params['wallet'])}",
        f"  CIRCLE_AGENT_WALLET_NETWORK={params['network']}",
        f"  X402_MAX_USDC_PER_PAYMENT={params['max_usdc']}",
        f"  X402_NETWORK_POLICY=public",
        f"  X402_HOST_ALLOWLIST=",
        f"  X402_REQUIRE_GATEWAY_BATCHING=true",
        f"  X402_ALLOW_HTTP=false",
        f"  X402_ALLOW_CHAT_OTP=false",
        "",
        "No changes written. Use '/x402 configure apply ...' to apply.",
    ]
    return "\n".join(lines)


def _handle_configure_apply(parts: list[str]) -> str:
    """Apply configuration — writes managed keys to .env."""
    params, error = _validate_configure_args(parts)
    if error:
        return f"Error: {error}"

    assert params is not None

    from hermes_x402.env_writer import update_env_file

    env_path = _resolve_hermes_home() / ".env"

    managed_keys = {
        "X402_ROLE": params["role"],
        "X402_BUYER_BACKEND": params["backend"],
        "CIRCLE_AGENT_WALLET_ADDRESS": params["wallet"],
        "CIRCLE_AGENT_WALLET_NETWORK": params["network"],
        "X402_MAX_USDC_PER_PAYMENT": params["max_usdc"],
        "X402_NETWORK_POLICY": "public",
        "X402_HOST_ALLOWLIST": "",
        "X402_REQUIRE_GATEWAY_BATCHING": "true",
        "X402_ALLOW_HTTP": "false",
        "X402_ALLOW_CHAT_OTP": "false",
    }

    # Preserve CIRCLE_CLI_EXECUTABLE if a verified path exists
    import shutil
    circle_path = shutil.which("circle")
    if circle_path:
        managed_keys["CIRCLE_CLI_EXECUTABLE"] = circle_path

    try:
        update_env_file(env_path, managed_keys)
    except OSError as exc:
        return f"Error writing configuration: {exc}"

    hermes_exe = shutil.which("hermes") or "hermes"
    lines = [
        "=== Configuration Applied ===",
        "",
        f"Written to: {env_path}",
        f"Wallet: {_mask_wallet(params['wallet'])}",
        f"Network: {params['network']}",
        "",
        "restart_required=true",
        f"Run: {hermes_exe} gateway restart",
    ]
    return "\n".join(lines)
