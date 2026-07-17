"""Aggregate readiness check for the x402 plugin.

Combines plugin configuration, network support, Circle CLI availability,
session status, wallet existence, SCA deployment, on-chain balance,
Gateway balance, payment cap, and public network policy into a single
readiness report.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _check_result(name: str, ok: bool, message: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "message": message}


def _blocker(code: str, next_tool: str) -> dict[str, str]:
    return {"code": code, "next_tool": next_tool}


async def assess_readiness(
    *,
    config: Any,
    cli_client: Any,
    wallet_address: str,
    network: str,
    role: str | None,
    backend_name: str | None,
) -> dict[str, Any]:
    """Build a comprehensive readiness report without performing any mutations."""
    checks: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    ready = True

    # 1. Plugin configuration
    config_ok = config is not None and role is not None
    checks.append(
        _check_result(
            "plugin_config",
            config_ok,
            f"role={role or 'unconfigured'}, backend={backend_name or 'none'}",
        )
    )
    if not config_ok:
        ready = False
        blockers.append(_blocker("not_configured", "x402_status"))

    # 2. Network support
    if config is not None:
        try:
            from hermes_x402.networks import get_network

            net = get_network(network)
            net_ok = net is not None
            checks.append(
                _check_result(
                    "network_support",
                    net_ok,
                    f"{net.display_name} ({net.caip2})"
                    if net_ok
                    else f"Unknown network: {network}",
                )
            )
        except Exception:
            checks.append(_check_result("network_support", False, f"Unknown network: {network}"))
            ready = False
            blockers.append(_blocker("unsupported_network", "x402_networks"))
    else:
        checks.append(_check_result("network_support", False, "No config"))

    # 3. Circle CLI availability
    cli_available = cli_client is not None
    cli_version = "unknown"
    if cli_available:
        try:
            ver = await cli_client.version()
            cli_version = ver.value
        except Exception:
            cli_version = "version_check_failed"
    checks.append(
        _check_result(
            "cli_availability",
            cli_available,
            f"version={cli_version}" if cli_available else "Circle CLI not available",
        )
    )
    if not cli_available and role in {"buyer", "dual", None}:
        ready = False
        blockers.append(_blocker("cli_not_available", "x402_status"))

    # 4. Session status
    session_authenticated = False
    if cli_available:
        try:
            session = await cli_client.session_status()
            session_authenticated = session.authenticated
            email_masked = _mask_email(session.email) if session.email else "N/A"
            checks.append(
                _check_result(
                    "session_status",
                    session_authenticated,
                    f"authenticated={session_authenticated}, email={email_masked}, "
                    f"environment={session.environment}",
                )
            )
            if not session.terms_accepted:
                ready = False
                blockers.append(_blocker("terms_required", "Accept Terms of Use manually"))
            elif not session_authenticated:
                ready = False
                blockers.append(_blocker("session_expired", "x402_login_start"))
        except Exception as exc:
            checks.append(_check_result("session_status", False, f"Error: {exc}"))
            ready = False
            blockers.append(_blocker("session_error", "x402_session_status"))
    else:
        checks.append(_check_result("session_status", False, "CLI not available"))

    # 5. Wallet existence
    wallet_exists = False
    if cli_available and wallet_address:
        try:
            wallets = await cli_client.list_wallets(network=network)
            wallet_exists = any(w.address.lower() == wallet_address.lower() for w in wallets)
            checks.append(
                _check_result(
                    "wallet_exists",
                    wallet_exists,
                    f"address={_mask_address(wallet_address)}, found={wallet_exists}",
                )
            )
            if not wallet_exists:
                ready = False
                blockers.append(_blocker("wallet_not_found", "x402_wallet_list"))
        except Exception as exc:
            checks.append(_check_result("wallet_exists", False, f"Error: {exc}"))
    else:
        checks.append(_check_result("wallet_exists", False, "No wallet address configured"))

    # 6. SCA deployment (placeholder — actual on-chain check requires RPC)
    checks.append(
        _check_result(
            "sca_deployed",
            True,
            "On-chain deployment check not yet implemented",
        )
    )

    # 7. On-chain wallet USDC balance
    if cli_available and wallet_address and wallet_exists:
        try:
            balances = await cli_client.get_balance(wallet_address=wallet_address, network=network)
            usdc_balance = "0"
            for b in balances:
                if b.symbol == "USDC":
                    usdc_balance = b.amount
                    break
            checks.append(
                _check_result(
                    "on_chain_balance",
                    True,
                    f"USDC={usdc_balance}",
                )
            )
        except Exception as exc:
            checks.append(_check_result("on_chain_balance", False, f"Error: {exc}"))
    else:
        checks.append(_check_result("on_chain_balance", False, "Wallet not available"))

    # 8. Gateway balance
    if cli_available and wallet_address and wallet_exists:
        try:
            gw = await cli_client.gateway_balance(wallet_address=wallet_address, network=network)
            checks.append(
                _check_result(
                    "gateway_balance",
                    True,
                    f"USDC={gw.total_usdc}",
                )
            )
        except Exception as exc:
            checks.append(_check_result("gateway_balance", False, f"Error: {exc}"))
    else:
        checks.append(_check_result("gateway_balance", False, "Wallet not available"))

    # 9. Payment cap
    payment_cap = config.max_usdc_per_payment if config else None
    checks.append(
        _check_result(
            "payment_cap",
            payment_cap is not None,
            f"max_usdc_per_payment={payment_cap}" if payment_cap else "No payment cap set",
        )
    )

    # 10. Public network policy
    policy = config.network_policy if config else "strict_allowlist"
    checks.append(
        _check_result(
            "network_policy",
            True,
            f"policy={policy}",
        )
    )

    return {
        "ready": ready,
        "checks": checks,
        "blockers": blockers,
    }


def _mask_email(email: str | None) -> str:
    """Mask email address: show first char and domain."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"


def _mask_address(address: str) -> str:
    """Mask wallet address: show first 6 and last 4 chars."""
    if not address or len(address) < 12:
        return "***"
    return f"{address[:6]}...{address[-4:]}"
