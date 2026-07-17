"""Tool input and output schemas for the x402 plugin."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_VERSION = "0.1.0"

ALLOWED_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}

MAX_URL_LENGTH = 2048
MAX_QUERY_LENGTH = 200
MAX_BODY_SIZE = 65536  # 64 KB
MAX_RESULT_COUNT = 20
MAX_HEADER_COUNT = 10
MAX_HEADER_LENGTH = 1024
MAX_OUTPUT_SIZE = 100_000  # 100 KB before truncation
MAX_OUTPUT_BYTES = 65536  # 64 KB body read limit
MAX_SEARCH_LIMIT = 25
MAX_SEARCH_RESULTS = 25

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

X402_STATUS_SCHEMA: dict[str, Any] = {
    "name": "x402_status",
    "description": (
        "Report x402 plugin status: version, role, backend, network, "
        "wallet address (safe form), max payment, host allowlist, "
        "configuration validity, and runtime availability."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_WALLET_STATUS_SCHEMA: dict[str, Any] = {
    "name": "x402_wallet_status",
    "description": (
        "Report Circle wallet status: CLI installation, authentication, "
        "selected wallet, configured network. Read-only. "
        "Never exposes entity secret, API key, or signing operations."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_WALLET_BALANCE_SCHEMA: dict[str, Any] = {
    "name": "x402_wallet_balance",
    "description": (
        "Report configured wallet USDC balance. CLI backend uses the "
        "existing typed balance client. DCW backend returns structured "
        "unsupported capability. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_NETWORKS_SCHEMA: dict[str, Any] = {
    "name": "x402_networks",
    "description": (
        "List x402 networks supported by the active backend. "
        "Read-only. Returns capability matrix for each network."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_SERVICE_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "x402_service_search",
    "description": (
        "Search the Circle service marketplace for x402-enabled services. "
        "Returns bounded results without payment. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for x402 services.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (1-25, default 10).",
            },
        },
        "required": ["query"],
    },
}

X402_SERVICE_INSPECT_SCHEMA: dict[str, Any] = {
    "name": "x402_service_inspect",
    "description": (
        "Inspect an x402 service URL without paying. Enforces: "
        "supported URL scheme, host allowlist, URL length limits. "
        "Returns normalized service metadata."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL of the x402 service to inspect.",
            },
            "method": {
                "type": "string",
                "description": "HTTP method (default: GET).",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            },
            "body": {
                "description": "Request body (for POST/PUT/PATCH).",
            },
        },
        "required": ["url"],
    },
}

X402_SUPPORTS_SCHEMA: dict[str, Any] = {
    "name": "x402_supports",
    "description": (
        "Check whether a URL supports x402 payments. Read-only preflight. "
        "Never signs, settles, deposits, or pays."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to check for x402 support.",
            },
            "method": {
                "type": "string",
                "description": "HTTP method intended for payment (default: GET).",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            },
            "body": {
                "description": "Request body (for POST/PUT/PATCH).",
            },
        },
        "required": ["url"],
    },
}

X402_FETCH_SCHEMA: dict[str, Any] = {
    "name": "x402_fetch",
    "description": (
        "Fetch a resource URL without paying. When HTTP 402 occurs, "
        "reports that payment is required but does not pay. "
        "Non-paying by default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to fetch.",
            },
            "method": {
                "type": "string",
                "description": "HTTP method (default: GET).",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            },
            "body": {
                "description": "Request body (for POST/PUT/PATCH).",
            },
        },
        "required": ["url"],
    },
}

X402_PAY_SCHEMA: dict[str, Any] = {
    "name": "x402_pay",
    "description": (
        "⚠️ This tool may transfer USDC. Pay for an x402 resource. "
        "Cannot change configured wallet, network, or backend. "
        "Capped by local configuration. Caller cap may reduce but "
        "never raise the configured cap. Protected payment headers "
        "cannot be supplied. Ambiguous outcomes return retry_safe=false "
        "and must not be retried automatically. "
        "Daily budget configuration is accepted but not enforced in this release."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL of the resource to pay for.",
            },
            "method": {
                "type": "string",
                "description": "HTTP method (default: GET).",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            },
            "body": {
                "description": "Request body (for POST/PUT/PATCH).",
            },
            "max_usdc": {
                "type": "string",
                "description": (
                    "Maximum USDC to spend (caller cap). "
                    "Can only reduce the configured cap, never raise it."
                ),
            },
        },
        "required": ["url"],
    },
}

X402_SESSION_STATUS_SCHEMA: dict[str, Any] = {
    "name": "x402_session_status",
    "description": (
        "Report Circle Agent Wallet CLI session status: authenticated, "
        "expired/not logged in, environment, and Terms state. Read-only. "
        "Masked email. Never exposes tokens or credential storage paths. "
        "Returns actionable remediation steps."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_LOGIN_START_SCHEMA: dict[str, Any] = {
    "name": "x402_login_start",
    "description": (
        "Start Circle Agent Wallet email OTP login. Only runs when no valid "
        "session exists. Returns an opaque login request ID. Never accepts "
        "or stores Circle Terms of Use. Apply expiry to pending login."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "email": {
                "type": "string",
                "description": "Email address for Circle Agent Wallet login.",
            },
        },
        "required": ["email"],
    },
}

X402_LOGIN_COMPLETE_SCHEMA: dict[str, Any] = {
    "name": "x402_login_complete",
    "description": (
        "Complete Circle Agent Wallet login with OTP. OTP exists in memory "
        "only for the duration of the call. Never logs or returns OTP. "
        "Failed OTP consumes the Circle request — require new login_start."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": "Opaque request ID from x402_login_start.",
            },
            "otp": {
                "type": "string",
                "description": "One-time password from email.",
            },
        },
        "required": ["request_id", "otp"],
    },
}

X402_LOGOUT_SCHEMA: dict[str, Any] = {
    "name": "x402_logout",
    "description": (
        "Clear Circle Agent Wallet CLI session. Idempotent. "
        "Does not modify wallet or x402 configuration."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_WALLET_LIST_SCHEMA: dict[str, Any] = {
    "name": "x402_wallet_list",
    "description": (
        "List Agent Wallets using Circle CLI. Read-only. "
        "Normalizes address and blockchain metadata. Never exposes secrets."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_WALLET_CREATE_SCHEMA: dict[str, Any] = {
    "name": "x402_wallet_create",
    "description": (
        "Create an Agent Wallet using Circle CLI. Does not silently "
        "replace the configured wallet. Return the new address and require "
        "explicit activation/configuration before use."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_WALLET_DEPLOY_SCHEMA: dict[str, Any] = {
    "name": "x402_wallet_deploy",
    "description": (
        "Deploy the configured Agent Wallet Smart Contract Account on-chain. "
        "Check deployment status first. Idempotent when already deployed. "
        "Never runs automatically as a side effect of x402_pay. "
        "Fail closed on unsupported networks."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_GATEWAY_BALANCE_SCHEMA: dict[str, Any] = {
    "name": "x402_gateway_balance",
    "description": (
        "Report Circle Gateway balance for the active wallet and configured "
        "network. Distinguishes Gateway balance from on-chain wallet USDC "
        "balance. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

X402_GATEWAY_DEPOSIT_PREVIEW_SCHEMA: dict[str, Any] = {
    "name": "x402_gateway_deposit_preview",
    "description": (
        "Preview a Gateway deposit without moving USDC. Accepts amount and "
        "optional service URL. Verifies wallet, session, deployment, and "
        "network support. Returns a short-lived preview ID bound to config. "
        "Read-only — must not move USDC."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "amount": {
                "type": "string",
                "description": "USDC amount to preview depositing.",
            },
            "service_url": {
                "type": "string",
                "description": "Optional service URL to verify Gateway support.",
            },
        },
        "required": ["amount"],
    },
}

X402_GATEWAY_DEPOSIT_EXECUTE_SCHEMA: dict[str, Any] = {
    "name": "x402_gateway_deposit_execute",
    "description": (
        "Execute a Gateway deposit using a preview ID from "
        "x402_gateway_deposit_preview. Do not accept replacement amount, "
        "wallet, network, or method. Revalidates session, config, wallet, "
        "and preview expiry. Execute exactly once. "
        "Mark preview consumed before or atomically with submission. "
        "retry_safe=false for ambiguous outcomes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "preview_id": {
                "type": "string",
                "description": "Preview ID from x402_gateway_deposit_preview.",
            },
        },
        "required": ["preview_id"],
    },
}

X402_READINESS_SCHEMA: dict[str, Any] = {
    "name": "x402_readiness",
    "description": (
        "Aggregate readiness check: plugin configuration, network support, "
        "Circle CLI availability, session status, wallet existence, "
        "SCA deployment, on-chain balance, Gateway balance, payment cap, "
        "and public network policy. Returns ready=true/false with blockers "
        "and next recommended tool. Read-only — never performs login, "
        "wallet creation, deployment, deposit, or payment."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}
