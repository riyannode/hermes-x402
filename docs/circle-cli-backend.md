# Circle CLI Agent Wallet Buyer Backend

## Inspected sources

| Source | Inspected revision/version | Purpose |
|---|---:|---|
| `riyannode/hermes-x402` | `4ced9211b300f5c3592ae2451507f9461c849e27` | PR base (`origin/main`) |
| `circlefin/agent-stack-starter-kits` | `fb4f4c71c198a7ad32db30b4edad2869fa4b8872` | Circle reference integration patterns |
| `NousResearch/hermes-agent` | `a6d9d1d2cf2a72e2c1e60fef973f95b90a18bfd7` | future plugin API inspection |
| `@circle-fin/cli` | `0.0.6`, package `circle`, Node `>=20.18.2` | authoritative executable contract |

Official Circle sources inspected: [Agent Wallet setup](https://agents.circle.com/skills/setup.md), [wallet login](https://agents.circle.com/skills/wallet-login.md), [x402 payment](https://agents.circle.com/skills/wallet-pay.md), [service discovery](https://agents.circle.com/skills/discover-services.md), and the Circle-owned `@circle-fin/cli` package distribution. Current CLI command behavior wins if a starter kit differs.

## Verified CLI contract

Every JSON command uses the envelope `{ "data": ... }`; errors use a nonzero exit plus an error envelope/diagnostic. The adapter parses JSON only for commands documented with `--output json`.

| Purpose | Verified command | Relevant JSON `data` shape |
|---|---|---|
| Version | `circle --version` | text semantic version |
| Supported chains | `circle blockchain list --output json` | `{ "blockchains": [{ "blockchain", "name", "evmChainId", "rpcUrl" }] }` |
| Session status | `circle wallet status --type agent --output json` | `{ "type":"agent", "mainnet": {"email?", "tokenStatus"}, "testnet": {...} }` |
| Agent wallets | `circle wallet list --chain <CHAIN> --type agent --output json` | `{ "wallets": [{ "type":"agent", "address", "blockchain", "createDate?" }] }` |
| Balance | `circle wallet balance --address <ADDRESS> --chain <CHAIN> --output json` | `{ "balances": [{ "amount", "token": {"symbol", "tokenAddress?"} }] }` |
| Service search | `circle services search [query] --output json` | discovery response object (not exposed by this PR) |
| Service inspect | `circle services inspect <url> --output json` | inspected service/challenge object (not exposed by this PR) |
| Managed x402 payment | `circle services pay <url> --address <ADDRESS> --chain <CHAIN> -X <METHOD> --max-amount <USDC> --output json` | `{ "response": <paid resource>, "payment": {"amount", "chain", "scheme", "seller", "receipt?"} }` |

The payment command makes the initial request, obtains the challenge, chooses the CLI-supported payment option, produces the authorization/header, submits payment, and fetches the paid resource. Therefore the selected backend contract is **managed payment and fetch**, not proof creation. The library intentionally does not reconstruct `Payment-Signature` or cryptographic payloads.

The CLI accepts arbitrary HTTP endpoint URLs in `services pay`; this PR therefore supports generic URL buyer calls subject to the existing common URL, scheme, allowlist, 402, and amount policy. The common service makes an unpaid initial request to enforce those policies and then invokes the CLI once; it never performs a Python protected retry for this backend. The CLI cannot receive an exact x402 accept object. It can also internally select a Gateway alternative, so the backend **fails closed** unless every advertised accept is materially identical and its network exactly matches the configured CLI chain's runtime `evmChainId` (`eip155:<id>`).

### Network and version limits

The backend requires CLI `>=0.0.6` and confirms the configured CLI chain against `circle blockchain list`. It does not hardcode a network support claim. Circle CLI `0.0.6` includes `Arc_Testnet` in its chain configuration, but support is accepted only when `circle blockchain list` returns it at runtime. Its exact CLI spelling must be configured, never inferred from x402 network strings.

Circle’s current x402 payment guidance documents a **known x402 v1 chain-alias mismatch** for some sellers (`base` versus `eip155:8453`) and a manual-sign fallback. This PR does not implement manual signing or private-key/typed-data fallback; such a seller is not guaranteed to work through the CLI backend. x402 version values outside 1/2, non-`exact` schemes, and malformed `amount/network/payTo/asset` inputs are rejected before CLI invocation.

## Authentication and selection

The adapter only inspects status. It never calls login, logout, wallet create, OTP submission, or `circle terms accept`. On absent/expired session or a CLI Terms gate, it raises `CircleCliAuthenticationRequiredError` with human-operator remediation. CLI login has separate mainnet/testnet sessions and may provision wallets on first login; that remains outside this PR.

Selection is address-explicit because `circle wallet list` exposes addresses, not a stable Agent Wallet ID. Before first payment, the backend verifies CLI version, runtime chain support, authenticated session, and that the configured address appears in the agent-only list for the configured chain. Multiple wallets never cause implicit selection.

## Safety and failure semantics

- `CircleCliRunner` uses `asyncio.create_subprocess_exec` with an argv vector, a command allowlist, `stdin=DEVNULL`, a reduced environment, bounded stdout/stderr (256 KiB), and terminate-then-kill timeouts.
- No shell, generic model-provided CLI arguments, OTP persistence, secret command arguments, automatic Terms acceptance, or unrestricted environment propagation is present.
- Read-only operations may be called once; this implementation deliberately has **no automatic retries**.
- Authentication and payment mutations are never retried. A process timeout during `services pay` becomes `CircleCliPaymentOutcomeUnknownError` / `PaymentSubmissionUnknownError`; an in-process fingerprint stays reserved for that ambiguous outcome.
- A zero exit code is insufficient: success requires the documented `data.response` and `data.payment.{amount,chain,scheme,seller}` shape. The CLI does not include the paid HTTP status in this JSON contract, so the normalized status is `None` rather than an invented `200`; the bounded paid response body and receipt-derived transaction hash are retained. Seller and CLI chain are cross-checked against the one validated accept and configured network.
- The seller payout address is independent of CLI authentication and the Agent Wallet address; seller middleware receives neither CLI client nor session material.

## Future native Hermes plugin design (not implemented)

Hermes plugins live under `~/.hermes/plugins/<name>/` (or project/user plugin directories), declare `plugin.yaml`, and register with `register(ctx)`. A plugin should use `ctx.register_tool`; handlers return JSON strings and pass through the normal registry availability (`check_fn`), approval, redaction, budget, and toolset pipeline. Core tools use `registry.register(..., check_fn=...)`; a local Circle plugin must not bypass this by calling the library from an unregistered side channel.

A later plugin can provide read-only `x402_status`, `x402_wallet_status`, `x402_wallet_balance`, `x402_service_search`, and `x402_service_inspect`, plus approval-governed `x402_fetch`/`x402_pay`. Its `check_fn` should report missing executable, incompatible version, session/Terms gate, absent configured wallet, and unsupported chain without disclosing CLI output. Async library calls should run in an async handler when the plugin runtime permits it, or use the documented runtime bridge rather than creating an unmanaged event loop. Do **not** expose authentication, OTP, logout, or Terms acceptance as autonomous tools.
