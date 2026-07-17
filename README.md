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
| **Session management** | Two-step OTP login, session status, logout |
| **Wallet readiness** | Wallet list, create, and Smart Contract Account deployment |
| **Gateway readiness** | Gateway balance, deposit preview, deposit execution |
| **Aggregate readiness** | Single readiness check across all subsystems |
| **Daily budget** | Optional per-day USDC spending cap |
| **Hermes plugin** | Auto-registers 20 tools into Hermes Agent on startup |

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

Or use the lower-level middleware:

```python
from hermes_x402.middleware import create_aiohttp_middleware

middleware = create_aiohttp_middleware(
    seller_address="0xYourAddress1234567890abcdef1234567890abcdef",
    chain="arcTestnet",
)

async def handler(request):
    result = await middleware.process_request(request, price="$0.01")
    if result is None:
        resp_402 = request["x402_402"]
        return web.json_response(
            resp_402["body"], status=resp_402["status"], headers=resp_402["headers"]
        )
    # Payment succeeded — result is PaymentResult
    return web.json_response({"data": "premium content"})
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
export CIRCLE_AGENT_WALLET_NETWORK="BASE"
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

All 20 tools are registered under the `x402` toolset.

### `x402_status`

Report plugin status and configuration. Shows version, role, backend, network, wallet address (safe form), max payment, and host allowlist.

### `x402_session_status`

Report Circle Agent Wallet CLI session status: authenticated, expired/not logged in, environment, and Terms state. Read-only. Masked email. Never exposes tokens or credential storage paths.

### `x402_login_start`

Start Circle Agent Wallet email OTP login. Only runs when no valid session exists. Returns an opaque login request ID. Never accepts or stores Circle Terms of Use. Apply expiry to pending login.

### `x402_login_complete`

Complete Circle Agent Wallet login with OTP. OTP exists in memory only for the duration of the call. Never logs or returns OTP. Failed OTP consumes the Circle request — require new login_start.

### `x402_logout`

Clear Circle Agent Wallet CLI session. Idempotent. Does not modify wallet or x402 configuration.

### `x402_wallet_status`

Read-only Circle wallet status. Shows CLI installation, authentication, selected wallet, and configured network. Never exposes entity secret, API key, or signing operations.

### `x402_wallet_balance`

Report configured wallet USDC balance. CLI backend uses the existing typed balance client. DCW backend returns structured `unsupported` capability. Read-only.

### `x402_wallet_list`

List Agent Wallets using Circle CLI. Read-only. Normalizes address and blockchain metadata. Never exposes secrets.

### `x402_wallet_create`

Create an Agent Wallet using Circle CLI. Does not silently replace the configured wallet. Return the new address and require explicit activation/configuration before use.

### `x402_wallet_deploy`

Deploy the configured Agent Wallet Smart Contract Account on-chain. Check deployment status first. Idempotent when already deployed. Never runs automatically as a side effect of `x402_pay`. Fail closed on unsupported networks.

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

Preview a Gateway deposit without moving USDC. Accepts amount and optional service URL. Verifies wallet, session, deployment, and network support. Returns a short-lived preview ID bound to config. Read-only — must not move USDC.

### `x402_gateway_deposit_execute`

Execute a Gateway deposit using a preview ID from `x402_gateway_deposit_preview`. Do not accept replacement amount, wallet, network, or method. Revalidates session, config, wallet, and preview expiry. Execute exactly once. `retry_safe=false` for ambiguous outcomes.

### `x402_readiness`

Aggregate readiness check: plugin configuration, network support, Circle CLI availability, session status, wallet existence, SCA deployment, on-chain balance, Gateway balance, payment cap, and public network policy. Returns `ready=true/false` with blockers and next recommended tool. Read-only — never performs login, wallet creation, deployment, deposit, or payment.

## Intended Workflow

```
x402_readiness
→ x402_login_start / x402_login_complete (when session required)
→ x402_wallet_list / x402_wallet_create (when wallet required)
→ x402_wallet_deploy (when SCA deployment required)
→ x402_service_search
→ x402_service_inspect
→ x402_supports
→ x402_gateway_balance
→ x402_gateway_deposit_preview (when Gateway deposit required)
→ explicit user approval
→ x402_gateway_deposit_execute
→ explicit user approval
→ x402_pay with a fresh challenge
```

Human approval applies only to fund-moving operations: `x402_pay` and `x402_gateway_deposit_execute`. Discovery and read operations are free.

## Seller API

### `create_aiohttp_gateway`

The ergonomic way to protect routes:

```python
from hermes_x402.seller_gateway import create_aiohttp_gateway

gateway = create_aiohttp_gateway(
    seller_address="0x1234...abcd",
    networks=["base", "polygon"],
    facilitator_url="https://gateway-api.circle.com",  # optional, auto-resolved
    default_description="Premium API",  # optional
)
```

### `@gateway.require` Decorator

Protect any aiohttp handler with a single decorator:

```python
@gateway.require("$0.01")
async def my_handler(request):
    return web.json_response({"ok": True})
```

### Static Price

```python
@gateway.require("$0.05")
async def premium_data(request):
    return web.json_response({"data": "secret"})
```

### Multi-Network

Accept payments on multiple networks:

```python
gateway = create_aiohttp_gateway(
    seller_address="0x...",
    networks=["base", "polygon", "ethereum", "arbitrum"],
)

@gateway.require(
    price="$0.01",
    networks=["base", "polygon"],  # override per-route
)
async def handler(request):
    return web.json_response({"data": "content"})
```

### Dynamic Price

Pass a callable for per-request pricing:

```python
def compute_price(request):
    # Price based on request parameters
    user_tier = request.query.get("tier", "basic")
    prices = {"basic": "$0.001", "pro": "$0.01", "enterprise": "$0.10"}
    return prices.get(user_tier, "$0.01")

@gateway.require(price=compute_price)
async def dynamic_pricing(request):
    return web.json_response({"data": "tier-specific content"})
```

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
| `X402_NETWORK_PREFERENCE` | `base` | Comma-separated preferred network order |

### Buyer Safeguards

| Variable | Default | Description |
|----------|---------|-------------|
| `X402_REQUIRE_GATEWAY_BATCHING` | `true` | Require Circle Gateway batching scheme |
| `X402_REQUIRE_APPROVAL_FOR_NEW_HOST` | `false` | Require user approval before paying new hosts |
| `X402_DAILY_BUDGET_USDC` | *(none)* | Daily USDC spending cap |
| `X402_MAX_USDC_PER_PAYMENT` | *(none)* | Max USDC per single payment (CLI backend requires this) |

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

## Network Support Matrix

All network metadata is sourced from the centralized registry in `hermes_x402/networks.py`. Every entry records its provenance (npm package, Circle docs, CLI output) and retrieval date.

### Mainnets

| Network | Key | Chain ID | CAIP-2 | CLI Backend | DCW Backend |
|---------|-----|----------|--------|:-----------:|:-----------:|
| Base | `base` | 8453 | `eip155:8453` | ✅ | ✅ |
| Ethereum | `ethereum` | 1 | `eip155:1` | ✅ | ✅ |
| Polygon | `polygon` | 137 | `eip155:137` | ✅ | ✅ |
| Arbitrum | `arbitrum` | 42161 | `eip155:42161` | ✅ | ✅ |
| Optimism | `optimism` | 10 | `eip155:10` | ✅ | ✅ |
| Avalanche | `avalanche` | 43114 | `eip155:43114` | ✅ | ✅ |
| Sonic | `sonic` | 146 | `eip155:146` | ❌ | ✅ |
| Unichain | `unichain` | 130 | `eip155:130` | ❌ | ✅ |
| World Chain | `worldChain` | 480 | `eip155:480` | ❌ | ✅ |
| HyperEVM | `hyperevm` | 998 | `eip155:998` | ❌ | ✅ |
| Sei | `sei` | 1329 | `eip155:1329` | ❌ | ✅ |
| Arc Mainnet ⚠️ | `arcMainnet` | 5042001 | `eip155:5042001` | ❌ | ✅ |

> ⚠️ **Arc Mainnet**: USDC address is unverified. Not recommended for production use.

### Testnets

| Network | Key | Chain ID | CAIP-2 | CLI Backend | DCW Backend |
|---------|-----|----------|--------|:-----------:|:-----------:|
| Base Sepolia | `baseSepolia` | 84532 | `eip155:84532` | ✅ | ✅ |
| Ethereum Sepolia | `ethereumSepolia` | 11155111 | `eip155:11155111` | ✅ | ✅ |
| Polygon Amoy | `polygonAmoy` | 80002 | `eip155:80002` | ✅ | ✅ |
| Arbitrum Sepolia | `arbitrumSepolia` | 421614 | `eip155:421614` | ✅ | ✅ |
| Optimism Sepolia | `optimismSepolia` | 11155420 | `eip155:11155420` | ✅ | ✅ |
| Avalanche Fuji | `avalancheFuji` | 43113 | `eip155:43113` | ✅ | ✅ |
| Arc Testnet | `arcTestnet` | 5042002 | `eip155:5042002` | ✅ | ✅ |
| Sonic Testnet | `sonicTestnet` | 64165 | `eip155:64165` | ❌ | ✅ |
| Unichain Sepolia | `unichainSepolia` | 1301 | `eip155:1301` | ❌ | ✅ |
| World Chain Sepolia | `worldChainSepolia` | 4801 | `eip155:4801` | ❌ | ✅ |
| HyperEVM Testnet | `hyperevmTestnet` | 999 | `eip155:999` | ❌ | ✅ |
| Sei Atlantic | `seiAtlantic` | 1328 | `eip155:1328` | ❌ | ✅ |

## Backend Capability Matrix

| Backend | Networks | Payment Signing | Balance Query |
|---------|----------|----------------|---------------|
| **CLI** (`circle` CLI) | Networks with `cli_chain` set (6 mainnets + 7 testnets) | Circle CLI `pay` command | ✅ `circle balance` |
| **DCW** (Developer-Controlled Wallet) | All 23 networks | Local cryptographic signing via `cryptography` | ❌ (not supported by DCW API) |

The `x402_networks` tool automatically filters the network list based on your active backend, so you only see networks you can actually use.

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

```bash
export X402_NETWORK_POLICY=public  # default — no allowlist needed
```

> **Note**: `public` mode is not unrestricted. SSRF targets, private IPs, and credential-bearing URLs are always rejected regardless of mode. DNS destination validation is always enforced.

### `strict_allowlist` Mode (Opt-in)

Only hosts listed in `X402_HOST_ALLOWLIST` are permitted. An empty allowlist means **nothing** is allowed. This is the safest mode for locked-down deployments.

```bash
export X402_NETWORK_POLICY=strict_allowlist
export X402_HOST_ALLOWLIST=api.example.com,data.service.io
```

### DNS Validation

- Hostnames are validated against the policy **before** DNS resolution.
- Literal IP addresses are checked against private/reserved ranges.
- Blocked hosts (`localhost`, `127.0.0.1`, `0.0.0.0`, `::1`, `metadata.google.internal`, `169.254.169.254`) are always rejected.

### Redirect Behavior

Redirects (3xx responses) are **never followed automatically**. The tool returns a bounded `redirect_not_followed` error with the `Location` header, preventing open-redirect abuse and SSRF via redirects.

## Security

hermes-x402 is designed with agent-safety principles:

- **No payment on import/registration**: Tool registration performs no network calls, subprocess calls, or payment operations. All tools are registered as pure functions.
- **Public mode by default**: Any public HTTPS host is reachable without configuring an allowlist. Host approval is not required in the normal workflow.
- **Bounded responses**: All tool responses are size-limited (`MAX_OUTPUT_BYTES`) to prevent context overflow. Large responses are truncated with metadata.
- **No secret leakage**: Wallet status and balance tools use `safe_wallet_address()` to redact addresses. Entity secrets, API keys, and signing operations are never exposed in tool output.
- **SSRF protection**: Well-known internal hosts, private IP ranges, and DNS-resolved private destinations are blocked in all modes.
- **Redirect prevention**: Redirects are never followed, preventing redirect-based SSRF.
- **Payment cap enforcement**: The `max_usdc` caller cap can only reduce, never raise, the configured cap.
- **No credentials in URLs**: URLs containing userinfo (username/password) are rejected.
- **Terms never auto-accepted**: Circle Terms of Use are never automatically accepted. If Terms are required, the tool returns `terms_action_required` and stops.
- **OTP in memory only**: OTP values exist only in memory for the duration of the login call. They are never logged, returned, or persisted.
- **Fund-move approval**: Only `x402_pay` and `x402_gateway_deposit_execute` may transfer funds. All other tools are read-only.

## State and Restart Behavior

### Pending Login State

- Pending login state (request IDs, expiry) is **memory-only** — stored in a Python dict inside the tool handler closure.
- After process restart, all pending logins are lost. A fresh `x402_login_start` is required.
- The request ID is the Circle CLI's own opaque request ID — not wrapped in a plugin-generated ID.
- A failed OTP consumes the Circle request; the pending login is removed and a new `x402_login_start` is required.

### Gateway Deposit Previews

- Deposit previews are **memory-only** — stored in a Python dict inside the tool handler closure.
- After process restart, all previews are lost. A fresh `x402_gateway_deposit_preview` is required.
- Preview protection (consumed-once, expiry, config fingerprint) is not restart-safe.
- Ambiguous deposit outcomes return `retry_safe=false` and must not be retried automatically.

### Session Persistence

- Circle CLI session state is managed by the CLI itself (on-disk), not by hermes-x402.
- Session persists across Hermes process restarts as long as the Circle CLI's credential storage is intact.

## Development

```bash
# Clone the repository
git clone https://github.com/riyannode/hermes-x402.git
cd hermes-x402

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check hermes_x402/ tests/
```

### Toolchain

- **Python**: 3.10, 3.11, 3.12
- **Testing**: pytest + pytest-asyncio
- **Linting**: ruff
- **Build**: hatchling

## Source References and Provenance

Network registry entries and protocol constants are sourced from:

| Source | Description | Retrieval Date |
|--------|-------------|----------------|
| [npm @circle-fin/x402-batching](https://www.npmjs.com/package/@circle-fin/x402-batching) | Network list, USDC addresses, gateway wallets, facilitator URLs | 2026-07-17 |
| [Circle Gateway API](https://developers.circle.com/gateway) | Gateway API endpoints, settle protocol, batching scheme | 2026-07-17 |
| [Circle USDC Docs](https://developers.circle.com/stablecoin/docs/usdc-on-other-networks) | USDC contract addresses per network | 2026-07-17 |
| [Circle CLI](https://developers.circle.com/wallet-sdk/reference/circle-cli) | `circle blockchain list` output, CLI chain identifiers | 2026-07-17 |
| [Circle Agent Stack Starter Kits](https://github.com/nicobailon/agent-stack-starter-kits) | Agent integration patterns | 2026-07-17 |

Each sensitive value (chain ID, USDC address, CAIP-2 identifier, gateway wallet) in `networks.py` records its source and retrieval date in the `provenance` field.

## License

MIT — see [LICENSE](LICENSE) for details.
