# Circle CLI Buyer Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production-safe Circle Agent Wallet buyer backend that uses the official Circle CLI managed x402 payment flow while retaining all DCW and seller behavior.

**Architecture:** Keep `X402BuyerService` as the policy and challenge authority. Preserve DCW as a proof-producing backend and add a typed managed-payment capability for the Circle CLI, whose `services pay` command discovers the challenge, signs, submits, and fetches the resource itself. Place all subprocess, parsing, session, wallet, and CLI error handling behind a small typed `circle_cli` client.

**Tech Stack:** Python 3.10–3.12, `asyncio.create_subprocess_exec`, `httpx`, `pytest`, Ruff, Circle CLI `@circle-fin/cli@0.0.6`.

## Global Constraints

- No shell command strings, `shell=True`, unrestricted CLI arguments, automatic login, OTP handling, Terms acceptance, wallet creation, or payment retries.
- Use `circle services pay <url> --address <addr> --chain <chain> -X <method> --output json` as the verified managed payment contract.
- CLI success JSON is `{ "data": { "response": ..., "payment": { "amount", "chain", "scheme", "seller", "receipt" } } }`.
- Use explicit `buyer_backend="cli"`, `circle_cli_wallet_address`, and `circle_cli_network`; reject mixed CLI/DCW configuration.
- The seller payout address remains separate from the Agent Wallet address.

### Task 1: Typed, restricted Circle CLI boundary

**Files:**
- Create: `hermes_x402/circle_cli/models.py`, `hermes_x402/circle_cli/errors.py`, `hermes_x402/circle_cli/runner.py`, `hermes_x402/circle_cli/client.py`, `hermes_x402/circle_cli/__init__.py`
- Test: `tests/test_circle_cli.py`

- [ ] Write deterministic fake-executable tests for argument separation, timeout termination, output bounding, error sanitization, malformed JSON, and rejected command verbs.
- [ ] Implement an asynchronous subprocess runner using `asyncio.create_subprocess_exec`, a controlled environment, explicit command allowlist, output limits, and terminate-then-kill timeout cleanup.
- [ ] Implement immutable version, status, wallet, balance, and managed-payment models plus a typed client that only emits documented commands.
- [ ] Verify runner tests pass.

### Task 2: Backend capability and common policy dispatch

**Files:**
- Modify: `hermes_x402/buyer/backend.py`, `hermes_x402/buyer/models.py`, `hermes_x402/buyer/service.py`, `hermes_x402/buyer/errors.py`
- Create: `hermes_x402/backends/circle_cli.py`
- Test: `tests/test_circle_cli.py`, `tests/test_hermes_x402.py`

- [ ] Write tests that prove URL/allowlist/amount validation occurs before CLI execution, caller payment headers are removed, payment timeout is ambiguous, and a CLI command is issued once.
- [ ] Add `ManagedPaymentBackend` and `ManagedPaymentResult`; preserve the proof interface and DCW path exactly.
- [ ] Dispatch managed backends only after common initial 402/challenge/policy validation, then return the normalized managed response without a second Python paid retry.
- [ ] Implement explicit address/network selection, authenticated-session validation, selected-wallet matching, verified CLI version, network support inspection, and in-flight/ambiguous fingerprint protection.
- [ ] Verify old buyer tests and new managed-payment tests pass.

### Task 3: Explicit config, agent, public exports, and docs

**Files:**
- Modify: `hermes_x402/config.py`, `hermes_x402/agent.py`, `hermes_x402/backends/__init__.py`, `hermes_x402/buyer/__init__.py`, `hermes_x402/__init__.py`, `README.md`, `.github/workflows/ci.yml`
- Create: `docs/circle-cli-backend.md`
- Test: `tests/test_hermes_x402.py`, `tests/test_circle_cli.py`

- [ ] Write config and agent tests for buyer/dual CLI construction, seller rejection, mixed-credential rejection, explicit backend requirements, and seller/CLI isolation.
- [ ] Add CLI-only configuration fields/environment mappings; do not change legacy or explicit DCW configuration behavior.
- [ ] Export the backend/client; extend import smoke coverage while retaining the Python-only CI matrix.
- [ ] Document verified CLI contract, source versions, failure/retry semantics, CLI prerequisites, and a later Hermes plugin design that uses plugin tools/check functions rather than core registration.
- [ ] Run focused test suites.

### Task 4: Full verification and PR

**Files:**
- Verify all changed paths

- [ ] Run `pytest`, `ruff check .`, `ruff format --check .`, `python -m build`, editable-install smoke, and import smoke.
- [ ] Run a source scan for shell execution, secret leaks, OTP/Terms automation, payment retries, and unbounded output.
- [ ] Review the final diff, commit once with Riyan’s Git identity, push `feat/circle-cli-buyer-backend`, create the PR without merging, and monitor the final SHA’s CI.
