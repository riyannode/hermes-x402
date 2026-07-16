# hermes-x402

Circle Gateway x402 integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent): an aiohttp seller middleware plus a backend-pluggable buyer.

## Roles and buyer backends

`role` and `buyer_backend` are separate concepts:

| Role | Seller requirement | Buyer requirement |
|---|---|---|
| `seller` | payout address, chain, optional facilitator override | none |
| `buyer` | none | explicitly selected backend |
| `dual` | payout address | explicitly selected backend |

A seller only needs a validated **payout address**. It does not need buyer wallet IDs, an entity secret, Circle API credentials, a Circle CLI session, or any buyer-backend selection. A buyer creates a payment proof using one selected backend.

This release implements `dcw` through Circle Developer-Controlled Wallets. `cli` is reserved for a later Circle Agent Wallet / Circle CLI PR and raises `UnsupportedBuyerBackendError`; it does not contain a stub payment implementation.

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

This version does not implement Circle CLI, Agent Wallet provisioning/login, automatic backend detection, native Hermes plugin registration, `plugin.yaml`, Hermes slash commands, seller custody/backends, automatic payment retries, or a database/web UI.
