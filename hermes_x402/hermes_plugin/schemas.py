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
        "and must not be retried automatically."
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
