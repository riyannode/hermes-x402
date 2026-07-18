# hermes-x402

> Circle x402 nanopayment integration for Hermes Agent — seller middleware (aiohttp) + buyer tool (DCW/CLI) + dual-role agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## Overview

**hermes-x402** brings [x402](https://www.x402.org/) nanopayments to [Hermes Agent](https://hermes-agent.nousresearch.com/) via the [Circle Gateway](https://developers.circle.com/gateway) protocol. It enables autonomous AI agents to pay for API resources with USDC and to monetize their own endpoints — all through the standard HTTP `402 Payment Required` status code.

- **Sellers** protect aiohttp routes with a decorator; payments settle automatically via Circle Gateway.
- **Buyers** fetch paid resources by signing x402 challenges with Circle Dev-Controlled Wallets (DCW) or the Circle CLI.
- **Dual-role agents** act as both seller and buyer simultaneously.
- **Service discovery** lets agents find new x402 endpoints at runtime through Circle Marketplace.

## Features

| Feature | Description |
|---------|-------------|
| **Seller middleware** | `create_aiohttp_middleware` for low-level request processing |
| **Seller gateway** | `create_aiohttp_gateway` + `@gateway.require` decorator for ergonomic route protection |
| **Buyer backends** | Circle DCW (Developer-Controlled Wallet) or Circle CLI — sign and submit payments |
| **Dual-role agent** | Single process acts as seller and buyer simultaneously |
| **Service discovery** | Search Circle Marketplace for x402-enabled services at runtime |
| **Supports preflight** | Check if a URL supports x402 before committing to a payment |
| **Network policy** | `public` mode (default) or `strict_allowlist` with SSRF protection |
| **Multi-network** | 11 mainnets + 12 testnets from a single centralized registry |
| **Login recovery** | Two-step OTP login via Circle CLI v0.0.6 wallet-scoped commands |
| **Gateway funding** | Gateway balance, deposit preview, deposit execution |
| **Daily budget** | Optional per-day USDC spending cap |
| **Hermes plugin** | Auto-registers 14 tools into Hermes Agent on startup |

## Installation

```bash
pip install hermes-x402
```

For DCW (Developer-Controlled Wallet) buyer support:

```bash
pip install hermes-x402[dcw]
```

For development:

```bash
pip install hermes-x402[dev]
```

### Dependencies

- `aiohttp >= 3.9` — seller middleware
- `httpx >= 0.27` — buyer HTTP client
- `pydantic >= 2.0` — schema validation
- `cryptography >= 42.0` — DCW signing (optional)

## Quick Start

### Seller Mode

Create a gateway that protects routes with x402 payments:

```python
from aiohttp import web
from hermes_x402.seller_gateway import create_aiohttp_gateway

gateway = create_aiohttp_gateway(
    seller_address="0xYourAddress1234567890abcdef1234567890abcdef",
    networks=["base", "polygon"],
)

@gateway.require("$0.01")
async def premium_data(request):
    return web.json_response({"secret": 42})

app = web.Application()
app.router.add_get("/premium", premium_data)
web.run_app(app)
```

### Buyer Mode

Set environment variables and use the tools:

```bash
# Role and backend
export X402_ROLE=buyer
export X402_BUYER_BACKEND=dcw          # or "cli"

# DCW credentials (for dcw backend)
export CIRCLE_DCW_WALLET_ID="your-wallet-id"
export CIRCLE_DCW_WALLET_ADDRESS="0xYourWalletAddress..."
export CIRCLE_ENTITY_SECRET="your-entity-secret"
export CIRCLE_API_KEY="your-api-key"

# Or CLI credentials (for cli backend)
export CIRCLE_AGENT_WALLET_ADDRESS="0xYourAgentWallet..."
export CIRCLE_AGENT_WALLET_NETWORK="ARC-TESTNET"
export X402_MAX_USDC_PER_PAYMENT="0.10"

# Network preference
export X402_NETWORK_PREFERENCE="base,polygon,ethereum"
```

### Hermes Plugin

hermes-x402 is a Hermes Agent plugin. Once installed, it auto-registers at startup via the `hermes_agent.plugins` entry point:

```toml
[project.entry-points."hermes_agent.plugins"]
hermes-x402 = "hermes_x402.hermes_plugin.entry"
```

No manual registration is needed — just `pip install hermes-x402` and all tools become available in your Hermes session.

## Hermes Tools

All 14 tools are registered under the `x402` toolset.

### `x402_status`

Report plugin status and configuration. Shows version, role, backend, network, wallet address (safe form), max payment, and host allowlist.

### `x402_wallet_status`

Aggregate runtime status: CLI installation, authentication, session validity, terms state, wallet existence, on-chain balance, Gateway balance, blockers, and recommended next tool. Read-only. Never exposes entity secret, API key, or signing operations.

### `x402_wallet_balance`

Report configured wallet USDC balance. CLI backend uses the existing typed balance client. DCW backend returns structured `unsupported` capability. Read-only.

### `x402_login_start`

Start Circle Agent Wallet email OTP login. Only runs when no valid session exists. Returns an opaque `login_id` with 5-minute expiry. Presents two choices:

- **Choice A (recommended):** Manual CLI login — the OTP never enters Hermes chat. Complete login through Circle CLI in your terminal.
- **Choice B (optional, disabled by default):** Chat OTP via `x402_login_complete` — requires `X402_ALLOW_CHAT_OTP=true`. The OTP passes through conversation and model/tool context.

Never accepts or stores Circle Terms of Use.

### `x402_login_complete`

Complete Circle Agent Wallet login with OTP via chat. **Disabled by default** — requires `X402_ALLOW_CHAT_OTP=true`. Requires `acknowledge_otp_exposure=true`. OTP exists in memory only for the duration of the call. Never logs or returns OTP. Failed OTP consumes the login — require new `x402_login_start`.

### `x402_networks`

List all supported networks with a capability matrix. Output is filtered based on the active backend (CLI vs DCW) to show only networks you can actually use.

### `x402_service_search`

Search Circle Marketplace for x402-enabled services. Accepts a query string and optional limit. Returns service URLs, descriptions, and pricing metadata.

### `x402_supports`

Check if a specific URL supports x402 payments. Sends a preflight request and reports the x402 challenge without making any payment.

### `x402_service_inspect`

Inspect a service URL without paying. Enforces URL scheme validation, host policy, and URL length limits. Returns normalized service metadata.

### `x402_fetch`

Fetch a resource URL without paying. When HTTP 402 occurs, reports that payment is required but does not pay. Useful for inspecting free endpoints or understanding the payment challenge before committing.

### `x402_pay`

Pay for an x402 resource. **This tool may transfer USDC.** Accepts an optional `max_usdc` caller cap that can reduce but never raise the configured cap. Returns the fetched resource data after successful payment. Ambiguous outcomes return `retry_safe=false` and must not be retried automatically. Must obtain a fresh 402 challenge from the server — never reuse a stale one.

### `x402_gateway_balance`

Report Circle Gateway balance for the active wallet and configured network. Distinguishes Gateway balance from on-chain wallet USDC balance. Read-only.

### `x402_gateway_deposit_preview`

Service-bound Gateway deposit preview. Requires `service_url`, HTTP `method`, and `amount`. Validates URL policy, fetches a fresh 402 challenge to verify the seller advertises a Gateway payment option, checks network compatibility, session, terms, and wallet balance. Returns a short-lived preview ID bound to all parameters. Read-only — must not move USDC.

### `x402_gateway_deposit_execute`

Execute a Gateway deposit using a preview ID from `x402_gateway_deposit_preview`. Do not accept replacement amount, wallet, network, or method. Revalidates session, config, wallet, service (fresh 402 challenge), and preview expiry. Execute exactly once. `retry_safe=false` for ambiguous outcomes.

## /x402 Slash Command

A single Hermes slash command for read-only status, discovery, and safe configuration.

### Read-only commands

```
/x402 status        — Plugin status and configuration
/x402 wallet        — Circle wallet + readiness status
/x402 balance       — Wallet USDC balance
/x402 gateway       — Gateway balance
/x402 networks      — Supported networks
/x402 supports <url> — Check if URL supports x402
```

These dispatch to the corresponding `x402_*` tools via `ctx.dispatch_tool` — no duplicated logic.

### Configuration

```
/x402 configure
```

Shows current configuration state: Circle CLI availability, configured/unconfigured state, missing managed variables.

```
/x402 configure preview buyer cli 0xYourWallet... ARC-TESTNET 0.10
```

Validates arguments and shows proposed managed keys without writing anything.

```
/x402 configure apply buyer cli 0xYourWallet... ARC-TESTNET 0.10
```

Writes managed keys to `$HERMES_HOME/.env`. Returns `restart_required=true` with the restart command.

### Constraints

- Only `buyer` role and `cli` backend are supported
- Only `ARC-TESTNET` network is supported
- Wallet must be `0x` + 40 hex characters
- `max_usdc` must be a positive finite Decimal
- Output masks wallet addresses
- No `/x402 pay`, `/x402 deposit`, or `/x402 login-complete` commands

## Intended Workflow

```
x402_wallet_status
→ x402_login_start / x402_login_complete (when session recovery is required)
→ x402_service_search
→ x402_service_inspect
→ x402_supports
→ x402_gateway_balance
→ x402_gateway_deposit_preview (when Gateway funding is required)
→ show preview to user
→ explicit user approval
→ x402_gateway_deposit_execute
→ wait/check balance
→ fresh x402_pay with a fresh challenge and separate explicit payment approval
```

Discovery remains open. Only fund-moving operations require explicit user approval.

## Configuration

All configuration is via environment variables. No config files required.

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `X402_ROLE` | *(none)* | Agent role: `buyer`, `seller`, or `dual` |
| `X402_BUYER_BACKEND` | *(none)* | Buyer backend: `dcw` or `cli` |
| `X402_SELLER_ADDRESS` | `""` | PayTo address for seller mode (`0x` + 40 hex) |
| `X402_CHAIN` | `arcTestnet` | Chain key (legacy; network registry overrides) |
| `X402_FACILITATOR_URL` | *(auto)* | Circle Gateway facilitator URL (auto-resolved from network) |

### Network Policy

| Variable | Default | Description |
|----------|---------|-------------|
| `X402_NETWORK_POLICY` | `public` | URL validation mode: `public` (default) or `strict_allowlist` |
| `X402_HOST_ALLOWLIST` | `""` | Comma-separated allowed hostnames |
| `X402_ALLOW_HTTP` | `false` | Allow HTTP (non-HTTPS) URLs — dev mode only |

### Discovery & Multi-Network

| Variable | Default | Description |
|----------|---------|-------------|
| `X402_DISCOVERY_PROVIDERS` | `circle_marketplace` | Comma-separated discovery providers |
| `X402_DISCOVERY_HOST_ALLOWLIST` | `""` | Comma-separated hostnames for discovery results |
| `X402_NETWORK_PREFERENCE` | `arctestnet` | Comma-separated preferred network order |

### Buyer Safeguards

| Variable | Default | Description |
|----------|---------|-------------|
| `X402_REQUIRE_GATEWAY_BATCHING` | `true` | Require Circle Gateway batching scheme |
| `X402_REQUIRE_APPROVAL_FOR_NEW_HOST` | `false` | Require user approval before paying new hosts |
| `X402_DAILY_BUDGET_USDC` | *(none)* | Daily USDC spending cap |
| `X402_MAX_USDC_PER_PAYMENT` | *(none)* | Max USDC per single payment (CLI backend requires this) |
| `X402_ALLOW_CHAT_OTP` | `false` | Allow OTP through chat (not secure/private — OTP passes through conversation history) |

### Circle Credentials

| Variable | Used By | Description |
|----------|---------|-------------|
| `CIRCLE_DCW_WALLET_ID` | DCW backend | Wallet ID |
| `CIRCLE_DCW_WALLET_ADDRESS` | DCW backend | Wallet address |
| `CIRCLE_DCW_BLOCKCHAIN` | DCW backend | Blockchain (default: `ARC-TESTNET`) |
| `CIRCLE_ENTITY_SECRET` | DCW backend | Entity secret for signing |
| `CIRCLE_API_KEY` | DCW backend | Circle API key |
| `CIRCLE_AGENT_WALLET_ADDRESS` | CLI backend | Agent wallet address |
| `CIRCLE_AGENT_WALLET_NETWORK` | CLI backend | Agent wallet network |
| `CIRCLE_CLI_EXECUTABLE` | CLI backend | CLI executable name (default: `circle`) |
| `CIRCLE_CLI_CWD` | CLI backend | CLI working directory |

## Network Policy

hermes-x402 enforces a network policy on every outbound URL request (supports, inspect, fetch, pay).

### `public` Mode (Default)

Any public HTTPS destination may be inspected or paid. The following are **always blocked**:

- Private/reserved IP addresses (`10.*`, `172.16-31.*`, `192.168.*`)
- Loopback addresses (`localhost`, `127.0.0.1`, `::1`)
- Link-local addresses (`169.254.*`, `fe80::*`)
- Metadata endpoints (`metadata.google.internal`, `169.254.169.254`)
- URLs with embedded credentials (userinfo)
- HTTP URLs (unless `X402_ALLOW_HTTP=true`)

### `strict_allowlist` Mode (Opt-in)

Only hosts listed in `X402_HOST_ALLOWLIST` are permitted. An empty allowlist means **nothing** is allowed.

### DNS Validation

- Hostnames are validated against the policy **before** DNS resolution.
- Literal IP addresses are checked against private/reserved ranges.
- Blocked hosts are always rejected.

### Redirect Behavior

Redirects (3xx responses) are **never followed automatically**. The tool returns a bounded `redirect_not_followed` error with the `Location` header.

## Security

hermes-x402 is designed with agent-safety principles:

- **No payment on import/registration**: Tool registration performs no network calls, subprocess calls, or payment operations.
- **Public mode by default**: Any public HTTPS host is reachable without configuring an allowlist.
- **Bounded responses**: All tool responses are size-limited (`MAX_OUTPUT_BYTES`) to prevent context overflow.
- **No secret leakage**: Wallet status and balance tools use `safe_wallet_address()` to redact addresses.
- **SSRF protection**: Well-known internal hosts, private IP ranges, and DNS-resolved private destinations are blocked.
- **Redirect prevention**: Redirects are never followed.
- **Payment cap enforcement**: The `max_usdc` caller cap can only reduce, never raise, the configured cap.
- **No credentials in URLs**: URLs containing userinfo are rejected.
- **Terms never auto-accepted**: Circle Terms of Use are never automatically accepted.
- **OTP in memory only**: OTP values exist only in memory for the duration of the login call.
- **Fund-move approval**: Only `x402_pay` and `x402_gateway_deposit_execute` may transfer funds.

## State and Restart Behavior

### Pending Login State

- Pending login state is **memory-only** — stored in a Python dict inside the tool handler closure.
- After process restart, all pending logins are lost. A fresh `x402_login_start` is required.
- The plugin generates an opaque `login_id` that maps to the Circle CLI's internal request ID.
- A failed OTP consumes the Circle request; a new `x402_login_start` is required.

### Gateway Deposit Previews

- Deposit previews are **memory-only** — stored in a Python dict inside the tool handler closure.
- After process restart, all previews are lost. A fresh `x402_gateway_deposit_preview` is required.
- Preview protection (consumed-once, expiry, config fingerprint) is not restart-safe.
- Ambiguous deposit outcomes return `retry_safe=false` and must not be retried automatically.

### Session Persistence

- Circle CLI session state is managed by the CLI itself (on-disk), not by hermes-x402.
- Session persists across Hermes process restarts as long as the Circle CLI's credential storage is intact.

## Installation as Hermes Plugin

### Install from source checkout (recommended)

```bash
git clone https://github.com/riyannode/hermes-x402.git
cd hermes-x402
python3 -m hermes_x402.install \
  --hermes-python /usr/local/lib/hermes-agent/venv/bin/python
```

Then manually restart the gateway:

```bash
/usr/local/lib/hermes-agent/venv/bin/hermes gateway restart
```

Or use the automatic restart flag:

```bash
python3 -m hermes_x402.install \
  --hermes-python /usr/local/lib/hermes-agent/venv/bin/python \
  --restart-gateway
```

The installer:
1. Detects the Hermes executable and Python environment
2. Builds a wheel from the repository using pip (no third-party build package required)
3. Installs the wheel into the Hermes Python environment (--no-deps)
4. Runs `hermes plugins enable hermes-x402 --no-allow-tool-override`
5. Verifies 14 tools, 1 pre_tool_call hook, and 1 slash command via static entry-point contract

### Optional: integrated Circle CLI bootstrap

```bash
python3 -m hermes_x402.install --with-circle-cli
```

When `--with-circle-cli` is passed, the installer:
1. Detects an existing `circle` binary on PATH and validates its version
2. If absent, requires [Bun](https://bun.sh/docs/installation) and runs `bun add -g @circle-fin/cli@0.0.6`
3. Writes `CIRCLE_CLI_EXECUTABLE` to `$HERMES_HOME/.env`

**Requirements:**
- Bun must already be installed (the installer never installs Bun or Node automatically)
- Circle CLI remains an external binary dependency — it is not vendored into the Python wheel
- Circle login and Terms acceptance remain manual steps

If a different Circle CLI version already exists, the installer reports `circle_cli_version_mismatch` and provides a manual remediation command. It never replaces, upgrades, or downgrades an existing installation automatically.

### Verify installation

```bash
python3 -m hermes_x402.install --check
```

### Live Arc Testnet acceptance test

```bash
python3 -m hermes_x402.install --live-test \
  --service-url https://seller.example/x402 \
  --method GET \
  --max-payment 0.001
```

Optional flags:
- `--hermes-python /path/to/python` — override the Hermes Python interpreter
- `--body-file /path/to/body.json` — JSON body for POST requests

⚠️ **Interactive test** — requires manual approval at each step.
Makes real Arc Testnet transactions. Never runs on mainnet.

### Uninstall

```bash
python3 -m hermes_x402.install --uninstall
```

Add `--restart-gateway` to restart the gateway after uninstall.

The uninstall flow:
1. Disables the plugin via `hermes plugins disable hermes-x402`
2. Runs `pip uninstall -y hermes-x402`
3. Verifies the entry-point record is absent
4. Prints the restart command (does not restart by default)

Or manually:

```bash
pip uninstall hermes-x402
hermes plugins disable hermes-x402
```

## Development

```bash
# Clone the repository
git clone https://github.com/riyannode/hermes-x402.git
cd hermes-x402

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type check
ruff check .
ruff format --check .
python -m compileall hermes_x402
```

## License

MIT
