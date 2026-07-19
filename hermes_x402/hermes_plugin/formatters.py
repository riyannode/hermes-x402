"""Human-readable formatters for /x402 slash command Telegram output.

Parses JSON responses from registered tools and formats them as concise,
readable text. Never dumps raw JSON. Never exposes secrets or full wallet
addresses.
"""

from __future__ import annotations

import json
from typing import Any

# Maximum Telegram output length (below 4096 limit for safety)
_MAX_OUTPUT = 3500


def _safe_json_parse(raw: str) -> dict[str, Any] | None:
    """Safely parse JSON, returning None on failure."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _mask(addr: str) -> str:
    """Mask wallet address for display."""
    if not addr or len(addr) < 10:
        return "***"
    return addr[:6] + "..." + addr[-4:]


def _mask_email(email: str) -> str:
    """Mask email for display."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}{'*' * min(len(local) - 1, 4)}@{domain}"


def _network_display(key: str) -> str:
    """Convert network key to display name."""
    mapping = {
        "ARC-TESTNET": "Arc Testnet",
        "arcTestnet": "Arc Testnet",
        "base": "Base",
        "ethereum": "Ethereum",
        "polygon": "Polygon",
        "arbitrum": "Arbitrum",
        "optimism": "Optimism",
        "avalanche": "Avalanche",
        "sonic": "Sonic",
        "unichain": "Unichain",
        "worldChain": "World Chain",
        "hyperevm": "HyperEVM",
        "sei": "Sei",
        "baseSepolia": "Base Sepolia",
        "ethereumSepolia": "Ethereum Sepolia",
        "polygonAmoy": "Polygon Amoy",
        "arbitrumSepolia": "Arbitrum Sepolia",
        "optimismSepolia": "Optimism Sepolia",
        "avalancheFuji": "Avalanche Fuji",
    }
    return mapping.get(key, key)


def _truncate(text: str) -> str:
    """Truncate to _MAX_OUTPUT."""
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[: _MAX_OUTPUT - 20] + "\n[...truncated...]"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_status(raw: str) -> str:
    """Format x402_status response as readable text."""
    data = _safe_json_parse(raw)
    if data is None:
        return "Status unavailable — tool returned invalid response."

    result = data if "success" not in data else data

    role = result.get("role", "unconfigured")
    backend = result.get("backend", "none")
    network = result.get("network", "none")
    wallet = result.get("wallet_address", "")
    version = result.get("version", "?")
    max_usdc = result.get("max_usdc_per_payment", "")
    policy = result.get("host_allowlist", "")
    configured = result.get("configured", False)
    available = result.get("available", False)

    status = "Ready" if available and configured else "Not ready"

    lines = [
        "**x402 Status**",
        "",
        f"Plugin: hermes-x402 v{version}",
        f"Role: {role.title() if role else 'Unconfigured'}",
        f"Backend: {_backend_label(backend)}",
        f"Network: {_network_display(network)}",
        f"Wallet: {_mask(wallet) if wallet else 'Not set'}",
    ]

    if max_usdc:
        lines.append(f"Max payment: {max_usdc} USDC")

    lines.append(f"Network policy: {policy.title() if policy else 'Public'}")
    lines.append(f"Status: {status}")

    return _truncate("\n".join(lines))


def _backend_label(backend: str) -> str:
    """Human-readable backend label."""
    return {
        "cli": "Circle CLI",
        "dcw": "Circle DCW",
    }.get(backend, backend or "None")


def format_wallet_status(raw: str) -> str:
    """Format x402_wallet_status response as readable text."""
    data = _safe_json_parse(raw)
    if data is None:
        return "Wallet status unavailable — tool returned invalid response."

    wallet = data.get("wallet_address", "")
    network = data.get("network", "")
    data.get("backend", "")

    lines = ["**Circle Wallet**", ""]

    lines.append(f"Wallet: {_mask(wallet) if wallet else 'Not set'}")
    lines.append(f"Network: {_network_display(network)}")

    # CLI info
    cli_ver = data.get("cli_version", "")
    if cli_ver and cli_ver != "version_check_failed":
        lines.append(f"CLI: circle v{cli_ver}")

    # Session
    email = data.get("email_masked", "")
    if email:
        lines.append(f"Account: {_mask_email(email)}")

    session_valid = data.get("session_valid", False)
    session_env = data.get("session_environment", "unknown")
    terms = data.get("terms_accepted", False)

    if session_valid:
        lines.append(f"Session: Active ({session_env})")
    elif session_env != "unknown":
        lines.append("Session: Expired")
    else:
        lines.append("Session: Not authenticated")

    lines.append(f"Terms: {'Accepted' if terms else 'Required'}")

    # Balance
    on_chain = data.get("on_chain_balance", "")
    gateway = data.get("gateway_balance", "")
    if on_chain:
        lines.append(f"On-chain balance: {on_chain} USDC")
    if gateway:
        lines.append(f"Gateway balance: {gateway} USDC")

    # Readiness
    buyer_ready = data.get("buyer_ready", False)
    gateway_ready = data.get("gateway_funding_ready", False)
    lines.append(f"Buyer runtime: {'Ready' if buyer_ready else 'Blocked'}")
    lines.append(f"Gateway funding: {'Ready' if gateway_ready else 'Blocked'}")

    # Next action
    next_action = data.get("next_action", "")
    if next_action:
        lines.append(f"Next action: {next_action}")

    # Blockers
    blockers = data.get("blockers", [])
    if blockers:
        lines.append("")
        lines.append("Blockers:")
        for b in blockers[:5]:
            msg = b.get("message", str(b)) if isinstance(b, dict) else str(b)
            lines.append(f"  • {msg}")

    return _truncate("\n".join(lines))


def format_wallet_balance(raw: str) -> str:
    """Format x402_wallet_balance response with deduplication."""
    data = _safe_json_parse(raw)
    if data is None:
        return "Balance unavailable — tool returned invalid response."

    wallet = data.get("wallet_address", "")
    network = data.get("network", "")

    lines = ["**Wallet Balance**", ""]
    lines.append(f"Wallet: {_mask(wallet) if wallet else 'Not set'}")
    lines.append(f"Network: {_network_display(network)}")

    # Deduplicate balances by (network, token_address, symbol)
    balances = data.get("balances", [])
    if not balances:
        # Try flat format
        amount = data.get("balance") or data.get("amount")
        if amount is not None:
            lines.append(f"USDC: {amount}")
        else:
            lines.append("No balance data")
        return _truncate("\n".join(lines))

    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for b in balances:
        if not isinstance(b, dict):
            continue
        symbol = (b.get("symbol") or b.get("token", {}).get("symbol") or "").upper()
        token_addr = b.get("token_address") or b.get("token", {}).get("token_address") or ""
        amount = b.get("amount") or b.get("balance") or "0"
        key = (network, token_addr.lower(), symbol)
        if key not in seen:
            seen.add(key)
            deduped.append({"symbol": symbol, "amount": amount, "token_address": token_addr})

    for b in deduped:
        symbol = b["symbol"] or "TOKEN"
        lines.append(f"{symbol}: {b['amount']}")

    if not deduped:
        lines.append("No balance data")

    return _truncate("\n".join(lines))


def format_gateway_balance(raw: str) -> str:
    """Format x402_gateway_balance response as readable text."""
    data = _safe_json_parse(raw)
    if data is None:
        return "Gateway balance unavailable — tool returned invalid response."

    wallet = data.get("wallet_address", "")
    network = data.get("network", "")
    total = data.get("total_usdc") or data.get("balance") or "0"

    lines = ["**Gateway Balance**", ""]
    lines.append(f"Wallet: {_mask(wallet) if wallet else 'Not set'}")
    lines.append(f"Network: {_network_display(network)}")
    lines.append(f"Available: {total} USDC")

    # Payment readiness
    ready = data.get("payment_ready", data.get("success", False))
    lines.append(f"Payment ready: {'Yes' if ready else 'No'}")

    return _truncate("\n".join(lines))


def format_networks(raw: str, filter_key: str = "") -> str:
    """Format x402_networks response, filtered and grouped."""
    data = _safe_json_parse(raw)
    if data is None:
        return "Networks unavailable — tool returned invalid response."

    networks = data.get("networks", [])
    if not networks:
        return "No networks available."

    # Filter by active/configured
    active_network = data.get("active_network", "")
    data.get("role", "")
    data.get("backend", "")

    # Determine which networks to show
    show_mainnets = True
    show_testnets = True
    show_active_only = False

    if filter_key == "active":
        show_active_only = True
    elif filter_key == "buyer":
        # Show only networks supported by the buyer's backend
        pass  # already filtered by tool
    elif filter_key == "gateway":
        pass  # show all with gateway support

    # Group by environment
    mainnets: list[dict] = []
    testnets: list[dict] = []
    active_net: dict | None = None

    for n in networks:
        if not isinstance(n, dict):
            continue
        env = n.get("environment", "")
        key = n.get("key", "")
        display = n.get("display_name", key)

        if key == active_network or display == active_network:
            active_net = n

        if env == "mainnet" and show_mainnets:
            mainnets.append(n)
        elif env == "testnet" and show_testnets:
            testnets.append(n)

    lines = ["**Networks**", ""]

    if show_active_only and active_net:
        display = active_net.get("display_name", active_network)
        caip2 = active_net.get("caip2", "")
        lines.append(f"Active: {display}")
        if caip2:
            lines.append(f"CAIP-2: {caip2}")
        return _truncate("\n".join(lines))

    # Active
    if active_net:
        display = active_net.get("display_name", active_network)
        lines.append(f"Active: {display}")

    # Mainnets
    if mainnets:
        lines.append("")
        lines.append("Mainnet:")
        for n in mainnets[:10]:
            display = n.get("display_name", n.get("key", "?"))
            lines.append(f"  • {display}")

    # Testnets
    if testnets:
        lines.append("")
        lines.append("Testnet:")
        for n in testnets[:10]:
            display = n.get("display_name", n.get("key", "?"))
            marker = " ←" if n.get("key") == active_network else ""
            lines.append(f"  • {display}{marker}")

    total = len(mainnets) + len(testnets)
    if total > 20:
        lines.append(f"\n({total} total, use /x402 networks all for full list)")

    return _truncate("\n".join(lines))


def format_supports(raw: str) -> str:
    """Format x402_supports response as readable text."""
    data = _safe_json_parse(raw)
    if data is None:
        return "Support check unavailable — tool returned invalid response."

    url = data.get("url", data.get("resource", ""))
    supported = data.get("supported", data.get("x402_supported", False))
    gateway = data.get("gateway_batching", False)
    version = data.get("x402_version", "")
    preferred = data.get("preferred_network", "")

    lines = ["**x402 Support Check**", ""]
    lines.append(f"Resource: {url}")
    lines.append(f"x402: {'Supported' if supported else 'Not detected'}")

    if supported:
        lines.append(f"Gateway batching: {'Supported' if gateway else 'Not detected'}")
        if version:
            lines.append(f"Version: {version}")
        if preferred:
            lines.append(f"Preferred network: {_network_display(preferred)}")

    return _truncate("\n".join(lines))


def format_configure(managed: dict[str, str], cli_info: dict[str, Any]) -> str:
    """Format /x402 configure output. Does not show executable path."""
    role = managed.get("X402_ROLE", "")
    backend = managed.get("X402_BUYER_BACKEND", "")
    wallet = managed.get("CIRCLE_AGENT_WALLET_ADDRESS", "")
    network = managed.get("CIRCLE_AGENT_WALLET_NETWORK", "")
    max_usdc = managed.get("X402_MAX_USDC_PER_PAYMENT", "")

    is_configured = bool(role and backend and wallet and network and max_usdc)

    lines = ["**x402 Configuration**", ""]

    cli_available = cli_info.get("available", False)
    cli_version = cli_info.get("version", "") or "unknown"
    lines.append(f"Circle CLI: {'Available' if cli_available else 'Not found'}")
    if cli_available:
        lines.append(f"Version: {cli_version}")

    lines.append(f"Configured: {'Yes' if is_configured else 'No'}")

    if is_configured:
        lines.append(f"Role: {role.title()}")
        lines.append(f"Backend: {_backend_label(backend)}")
        lines.append(f"Wallet: {_mask(wallet) if wallet else 'Not set'}")
        lines.append(f"Network: {_network_display(network)}")
        if max_usdc:
            lines.append(f"Max payment: {max_usdc} USDC")
    else:
        missing = []
        if not role:
            missing.append("role")
        if not backend:
            missing.append("backend")
        if not wallet:
            missing.append("wallet")
        if not network:
            missing.append("network")
        if not max_usdc:
            missing.append("max payment")
        if missing:
            lines.append(f"Missing: {', '.join(missing)}")

    return _truncate("\n".join(lines))
