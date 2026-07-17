# hermes-x402

Circle Gateway x402 integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent): an aiohttp seller middleware plus a backend-pluggable buyer.

## Roles and buyer backends

`role` and `buyer_backend` are separate concepts:

| Role | Seller requirement | Buyer requirement |
|---|---|---|
| `seller` | payout address, chain, optional facilitator override | none |
| `buyer` | none | explicitly selected backend |
| `dual` | payout address | explicitly selected backend |

A buyer uses one explicitly selected backend; it either creates a payment proof (DCW) or invokes an official managed payment flow (CLI).

This release implements `dcw` through Circle Developer-Controlled Wallets and `cli` through the official Circle Agent Wallet CLI. The CLI is a **managed payment** backend: Circle CLI—not this library—creates payment authorization, executes payment, and performs the protected fetch. It is intentionally selected only by `buyer_backend="cli"` and never inferred from a logged-in CLI session.

In dual role, seller and buyer wallet addresses are separate. The current safety behavior rejects equal addresses to prevent a Gateway self-transfer.

## Install

```bash
pip install "git+https://github.com/riyannode/hermes-x402.git#egg=hermes-x402[dcw]"
```

## Seller

Seller middleware stays backend-neutral and settles through Circle Gateway exactly as before.

```python
from aiohttp import web
from hermes_x402 import create_aiohttp_middleware

seller = create_aiohttp_middleware(seller_address="0xSeller", chain="arcTestnet")

async def premium(request: web.Request):
    payment = await seller.process_request(request, price="$0.01")
    if payment is None:
        required = request["x402_402"]
        return web.json_response(required["body"], status=402, headers=required["headers"])
    return web.json_response({"data": "premium", "payer": payment.payer})
```

## Buyer with new backend-object API (recommended)

The common buyer service validates the URL/allowlist and amount **before** it asks the backend for a proof. It then retries exactly once with the backend-generated `Payment-Signature` header.

```python
from hermes_x402 import CircleDcwBuyerBackend, create_buyer_tool

backend = CircleDcwBuyerBackend(
    wallet_id="...",
    wallet_address="0xBuyer...",
    entity_secret="...",  # store outside the repository
    api_key="...",
    blockchain="ARC-TESTNET",
    chain="arcTestnet",
)
tool = create_buyer_tool(
    backend=backend,
    max_usdc="0.01",
    host_allowlist=["api.example.com"],
)
result = await tool.pay("https://api.example.com/premium")
```

`CircleDcwBuyerBackend` owns entity-public-key retrieval/caching, fresh RSA-OAEP entity-secret encryption for every signing request, DCW `signTypedData`, signature normalization, and the existing nested EIP-3009/x402 proof wire format. The common buyer layer does not receive or serialize the entity secret.

## Buyer with Circle Agent Wallet CLI

| Backend | Payment model | Credentials in Python | Wallet selection |
|---|---|---|---|
| `dcw` | library creates proof and retries resource | DCW entity secret | DCW wallet ID + address |
| `cli` | Circle CLI discovers, pays, and fetches | none | explicit Agent Wallet address + CLI chain |

Install the official CLI (currently verified against `@circle-fin/cli` **0.0.6**, Node.js **>=20.18.2**):

```bash
npm install -g @circle-fin/cli
circle --version
# Human-operated only: login and any Terms consent must happen outside this library.
circle wallet status --type agent --output json
circle blockchain list --output json
circle wallet list --chain BASE --type agent --output json
circle wallet balance --address 0xBuyer --chain BASE --output json
```

Use a chain returned by `circle blockchain list --output json`; this library does not claim support for a network merely because a configuration value exists. The CLI backend verifies its version, the configured chain, authenticated Agent Wallet session, and the explicitly selected address before its first payment. It never chooses the first wallet or creates a wallet.

```python
from hermes_x402 import X402Config, X402HermesAgent

config = X402Config(
    role="dual",
    seller_address="0xSeller...",       # seller payout address
    buyer_backend="cli",
    circle_cli_wallet_address="0xBuyer...",  # Agent Wallet address; distinct identity
    circle_cli_network="BASE",
    max_usdc_per_payment="0.01",
    host_allowlist=["api.example.com"],
)
agent = X402HermesAgent.from_config(config)
result = await agent.pay("https://api.example.com/premium")
```

Environment equivalents are `X402_BUYER_BACKEND=cli`, `CIRCLE_AGENT_WALLET_ADDRESS`, and `CIRCLE_AGENT_WALLET_NETWORK`. The runtime permits only the official `circle` executable and no custom CLI working directory. CLI configuration requires a non-empty `max_usdc_per_payment`; the library canonicalizes it as decimal USDC and always passes it as `--max-amount`. CLI configuration and DCW credentials are mutually exclusive. CLI configuration is rejected for seller-only role.

> **Payment safety:** URL/host and amount cap validation occur before `circle services pay`. The library passes an explicit `--max-amount` cap and strips caller-supplied payment headers. It never retries login, OTP, Terms, wallet creation, or payment. A process timeout after payment starts is `PaymentSubmissionUnknownError`; inspect Circle CLI/payment state manually rather than retrying.

The backend uses the documented CLI managed command equivalent to:

```bash
circle services pay <url> --address <configured-address> --chain <configured-chain> \
  -X <method> --max-amount <cap> --output json
```

Circle CLI owns `Payment-Signature` creation and the paid fetch. Consequently this backend does **not** expose `create_payment_proof`. See [the focused CLI design note](docs/circle-cli-backend.md) for the exact JSON envelope and known CLI x402 v1 limitation.

## Legacy DCW buyer API

The original API remains supported (and emits a documented `DeprecationWarning`):

```python
from hermes_x402 import create_buyer_tool

tool = create_buyer_tool(
    wallet_id="...",
    wallet_address="0xBuyer...",
    entity_secret="...",
    chain="arcTestnet",
    max_usdc="0.01",
)
```

Callers must use either `backend=...` **or** legacy DCW arguments, not both.

## Dual role

```python
from hermes_x402 import CircleDcwBuyerBackend, X402HermesAgent

buyer_backend = CircleDcwBuyerBackend(
    wallet_id="...",
    wallet_address="0xBuyer...",
    entity_secret="...",
)
agent = X402HermesAgent(
    seller_address="0xSeller...",
    buyer_backend=buyer_backend,
)
```

Seller middleware remains unchanged; downstream `agent.pay(...)` delegates to the buyer service. Buyer credentials and entity secrets are never passed into seller middleware.

## Explicit config roles

```python
from hermes_x402 import X402Config

config = X402Config(
    role="dual",
    seller_address="0xSeller...",
    buyer_backend="dcw",
    wallet_id="...",
    wallet_address="0xBuyer...",
    entity_secret="...",
)
config.validate()
```

- `seller` requires only `seller_address` and rejects buyer configuration.
- `buyer` requires an explicit backend.
- `dual` requires both seller address and backend.
- DCW requires wallet ID, wallet address, and entity secret.

## Buyer result and errors

`BuyerResult.payment_status` distinguishes `not_submitted`, `proof_created` (reserved for future intermediate reporting), `submission_unknown`, `resource_succeeded`, and `resource_failed_after_payment`.

Errors are normalized as `BuyerConfigurationError`, `UnsupportedBuyerBackendError`, `InvalidPaymentChallengeError`, `PaymentPolicyError`, `PaymentNotSubmittedError`, `PaymentSubmissionUnknownError`, `PaymentProofError`, `PaidResourceRequestError`, `DcwSigningError`, and `DcwApiError`.

## Development

```bash
uv venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m build
```

## Non-scope

This version does not implement automatic Circle CLI login, OTP submission, Terms acceptance, seller wallet provisioning, automatic backend detection, native Hermes plugin registration, `plugin.yaml`, Hermes slash commands, seller custody/backends, automatic payment retries, or a database/web UI.
