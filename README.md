# hermes-x402

Circle x402 nanopayment integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — seller middleware (aiohttp) + buyer tool (DCW) + dual-role agent.

## What is x402?

[x402](https://x402.org) is an open standard for HTTP 402 "Payment Required" — pay per request using USDC on [Arc](https://www.circle.com/en/arc). This package integrates Circle's x402 implementation into Hermes Agent.

## Features

- **Seller middleware** — aiohttp middleware that settles payments directly via Circle Gateway (no verify step, lower latency)
- **Buyer tool** — Hermes tool definition using Circle DCW signing (no raw private key)
- **Dual-role agent** — Both seller + buyer in one class
- **ContextVar bridge** — Payment proof propagates from middleware → tools
- **Arc presets** — Testnet and mainnet configuration out of the box

## Install

```bash
pip install git+https://github.com/riyannode/hermes-x402.git
```

Or with DCW signing support:

```bash
pip install "git+https://github.com/riyannode/hermes-x402.git#egg=hermes-x402[dcw]"
```

## Quick Start

### Seller (aiohttp)

```python
from hermes_x402 import create_aiohttp_middleware
from aiohttp import web

middleware = create_aiohttp_middleware(
    seller_address="0xYOUR_WALLET_ADDRESS",
    chain="arcTestnet",
)

async def premium_data(request: web.Request):
    result = await middleware.process_request(request, price="$0.01")
    if result is None:
        resp_402 = request["x402_402"]
        return web.json_response(
            resp_402["body"],
            status=resp_402["status"],
            headers=resp_402["headers"],
        )
    return web.json_response({"data": "Premium content", "paid_by": result.payer})

app = web.Application()
app.router.add_get("/premium-data", premium_data)
web.run_app(app, port=8080)
```

### Buyer (tool)

```python
from hermes_x402 import create_buyer_tool
import asyncio

tool = create_buyer_tool(
    wallet_id="...",
    wallet_address="0x...",
    entity_secret="...",
    chain="arcTestnet",
)

async def main():
    result = await tool.pay("http://localhost:8080/premium-data")
    print(f"Status: {result.status}")
    print(f"Data: {result.data}")

asyncio.run(main())
```

### Dual-Role Agent

```python
from hermes_x402 import X402HermesAgent
from aiohttp import web

agent = X402HermesAgent(
    seller_address="0xSeller...",
    buyer_wallet_id="...",
    buyer_wallet_address="0xBuyer...",
    buyer_entity_secret="...",
)

async def analyze(request: web.Request):
    error = await agent.handle_request(request, price="$0.01")
    if error:
        return web.json_response(error["body"], status=error["status"])

    # Pay downstream
    downstream = await agent.pay("https://api.example.com/analysis")
    return web.json_response({"result": "done", "downstream_paid": downstream is not None})
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `X402_SELLER_ADDRESS` | Seller | Your wallet address to receive payments |
| `X402_CHAIN` | No | Chain name (default: `arcTestnet`) |
| `X402_FACILITATOR_URL` | No | Override facilitator URL |
| `CIRCLE_DCW_WALLET_ID` | Buyer | Circle DCW wallet ID |
| `CIRCLE_DCW_WALLET_ADDRESS` | Buyer | Circle DCW wallet address |
| `CIRCLE_ENTITY_SECRET` | Buyer | Circle entity secret for DCW signing |
| `CIRCLE_API_KEY` | Buyer | Circle API key (optional) |
| `X402_MAX_USDC_PER_PAYMENT` | No | Max USDC per single payment |
| `X402_DAILY_BUDGET_USDC` | No | Daily spending cap |
| `X402_HOST_ALLOWLIST` | No | Comma-separated allowed hosts |

### Programmatic Config

```python
from hermes_x402 import X402Config

config = X402Config(
    seller_address="0x...",
    chain="arcTestnet",
    wallet_id="...",
    wallet_address="0x...",
    entity_secret="...",
    max_usdc_per_payment="0.01",
    host_allowlist=["api.example.com"],
)

agent = X402HermesAgent.from_config(config)
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  HERMES GATEWAY                  │
│                                                  │
│  HTTP Request → aiohttp middleware (x402 check)  │
│       ↓                                          │
│  [BLOCKED if no payment] → return 402            │
│       ↓ (payment settled)                        │
│  Gateway Handler → AIAgent → Conversation Loop   │
│       ↓                                          │
│  Tool Execution → x402 buyer tool (optional)     │
│       ↓                                          │
│  Downstream x402 API → pay + get resource        │
└─────────────────────────────────────────────────┘
```

**Seller flow**: aiohttp middleware → decode Payment-Signature → settle via Circle Gateway → return 402 or proceed.

**Buyer flow**: tool makes request → handles 402 → signs via DCW → retries with Payment-Signature.

**Context bridge**: Payment proof (payer, amount, network) propagates from seller middleware to buyer tools via `ContextVar`.

## Circle x402 Flow

```
Client                    Server (Hermes)              Circle Gateway
  │                            │                            │
  │  GET /premium              │                            │
  │ ──────────────────────────>│                            │
  │                            │                            │
  │  402 Payment Required      │                            │
  │ <──────────────────────────│                            │
  │                            │                            │
  │  Sign via DCW              │                            │
  │  (no raw private key)      │                            │
  │                            │                            │
  │  GET /premium              │                            │
  │  + Payment-Signature       │                            │
  │ ──────────────────────────>│                            │
  │                            │  settle(payload, reqs)     │
  │                            │ ──────────────────────────>│
  │                            │  {success: true, tx: 0x..} │
  │                            │ <──────────────────────────│
  │  200 OK + premium data     │                            │
  │ <──────────────────────────│                            │
```

## Dependencies

- `aiohttp` — Hermes HTTP server
- `httpx` — HTTP client for Gateway API
- `pydantic` — Data validation
- `cryptography` — DCW signing (optional)

## Development

```bash
git clone https://github.com/riyannode/hermes-x402.git
cd hermes-x402
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .
ruff format .
```

## License

MIT

## Related

- [x402-header-agent](https://github.com/riyannode/x402-header-agent) — Generic x402 SDK (TypeScript + Python)
- [circle-titanoboa-sdk](https://github.com/vyperlang/circle-titanoboa-sdk) — Python x402 with titanoboa
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — AI agent framework
- [Circle x402](https://developers.circle.com/gateway/nanopayments) — Official docs
