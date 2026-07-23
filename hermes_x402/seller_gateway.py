"""Canonical aiohttp seller implementation for Circle x402 v2 Gateway settlement.

This module is the single seller engine.  ``hermes_x402.middleware`` is a
backward-compatible adapter over this code, not a second settlement path.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from aiohttp import web

from hermes_x402.context import reset_payment_context, set_payment_context_token
from hermes_x402.networks import NetworkConfig, NetworkNotFoundError, get_network

logger = logging.getLogger("hermes_x402.seller_gateway")

PAYMENT_SIGNATURE_HEADER = "Payment-Signature"
PAYMENT_REQUIRED_HEADER = "Payment-Required"
PAYMENT_RESPONSE_HEADER = "Payment-Response"

CIRCLE_BATCHING_SCHEME = "exact"
CIRCLE_BATCHING_NAME = "GatewayWalletBatched"
CIRCLE_BATCHING_VERSION = "1"
X402_VERSION = 2
# Circle's seller docs and @circle-fin/x402-batching publish the server-owned
# Gateway requirement as seven days plus a small buffer.
SERVER_MIN_TIMEOUT_SECONDS = 7 * 24 * 60 * 60 + 100
DEFAULT_MAX_TIMEOUT_SECONDS = SERVER_MIN_TIMEOUT_SECONDS
# Circle CLI 0.0.6 normalizes the buyer's selected/accepted requirement to a
# 30-day validity preference before embedding it in the x402 v2 payload. Treat
# that as buyer-side compatibility metadata: it may be >= server minimum, but it
# must not replace the server-owned settlement requirement.
BUYER_MAX_TIMEOUT_SECONDS = 30 * 24 * 60 * 60

_USDC_DECIMALS = 6
_USDC_MULTIPLIER = Decimal(10) ** _USDC_DECIMALS
_MAX_ATOMIC = 10**18
_MAX_ENCODED_PAYMENT_HEADER = 8192
_MAX_DECODED_PAYMENT_PAYLOAD = 32768
_MAX_FACILITATOR_RESPONSE = 65536
_MAX_JSON_DEPTH = 16
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_MAINNET_FACILITATOR = "https://gateway-api.circle.com"
_TESTNET_FACILITATOR = "https://gateway-api-testnet.circle.com"


@dataclass
class PaymentResult:
    """Result of a successful payment settlement."""

    payer: str
    amount: str
    network: str
    transaction: str | None = None


_RequestKey = getattr(web, "RequestKey", None)
_HAS_REQUEST_KEY = _RequestKey is not None

if _HAS_REQUEST_KEY:
    X402_PAYMENT_KEY: Any = _RequestKey("x402_payment", PaymentResult)
    X402_CHALLENGE_KEY: Any = _RequestKey("x402_402", dict)
    X402_ERROR_KEY: Any = _RequestKey("x402_error", dict)
else:  # aiohttp < 3.14 compatibility: RequestKey is unavailable.
    X402_PAYMENT_KEY = "x402_payment"
    X402_CHALLENGE_KEY = "x402_402"
    X402_ERROR_KEY = "x402_error"


def _get_request_state(request: web.Request) -> Mapping[Any, Any]:
    state = getattr(request, "_state", None)
    return state if isinstance(state, Mapping) else {}


def _get_mock_setitem_value(request: web.Request, key: Any) -> Any:
    """Read values captured by unittest mocks without touching storage."""
    for call in reversed(getattr(request.__setitem__, "call_args_list", [])):
        if len(call.args) >= 2 and call.args[0] == key:
            return call.args[1]
    return None


def _get_request_value(request: web.Request, key: Any) -> Any:
    candidate = _get_request_state(request).get(key)
    if candidate is not None:
        return candidate
    candidate = _get_mock_setitem_value(request, key)
    if candidate is not None:
        return candidate
    if _HAS_REQUEST_KEY:
        return None
    try:
        return request[key]
    except Exception:
        return None


def set_x402_payment(request: web.Request, result: PaymentResult) -> None:
    """Store settled payment metadata behind the version-compatible request key."""
    request[X402_PAYMENT_KEY] = result  # type: ignore[index]


def get_x402_payment(request: web.Request) -> PaymentResult | None:
    """Return settled payment metadata from the canonical request-state accessor."""
    candidate = _get_request_value(request, X402_PAYMENT_KEY)
    return candidate if isinstance(candidate, PaymentResult) else None


def set_x402_challenge(request: web.Request, challenge: dict[str, Any]) -> None:
    """Store an unpaid challenge behind the version-compatible request key."""
    request[X402_CHALLENGE_KEY] = challenge  # type: ignore[index]


def get_x402_challenge(request: web.Request) -> dict[str, Any] | None:
    """Return unpaid challenge metadata from the canonical request-state accessor."""
    candidate = _get_request_value(request, X402_CHALLENGE_KEY)
    return candidate if isinstance(candidate, dict) else None


class FacilitatorOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    PAYMENT_REJECTED = "PAYMENT_REJECTED"
    REPLAY_OR_CONFLICT = "REPLAY_OR_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    FACILITATOR_UNAVAILABLE = "FACILITATOR_UNAVAILABLE"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID_FACILITATOR_RESPONSE = "INVALID_FACILITATOR_RESPONSE"


@dataclass(frozen=True)
class FacilitatorSettlementResult:
    outcome: FacilitatorOutcome
    success: bool = False
    transaction: str = ""
    payer: str = ""
    error: str = ""
    http_status: int | None = None
    retry_safe: bool = False


@dataclass
class ReceiptRecord:
    payment_fingerprint: str
    request_fingerprint: str
    route_id: str
    state: str
    settlement: dict[str, Any] = field(default_factory=dict)
    resource_result_reference: str = ""
    response_status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: bytes | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)


@dataclass(frozen=True)
class ReceiptBeginResult:
    action: str  # owner | wait | replay | conflict
    record: ReceiptRecord


class ReceiptStore(Protocol):
    """Pluggable receipt/idempotency seam for seller routes."""

    async def begin(
        self, payment_fingerprint: str, request_fingerprint: str, route_id: str
    ) -> ReceiptBeginResult: ...

    async def wait(self, record: ReceiptRecord) -> ReceiptRecord: ...

    async def mark_settled(self, payment_fingerprint: str, settlement: dict[str, Any]) -> None: ...

    async def mark_rejected(self, payment_fingerprint: str, error: str) -> None: ...

    async def mark_completed(
        self,
        payment_fingerprint: str,
        *,
        response_status: int,
        response_headers: Mapping[str, str],
        response_body: bytes | None,
        resource_result_reference: str = "",
    ) -> None: ...

    async def mark_handler_failed(self, payment_fingerprint: str) -> None: ...

    async def mark_ambiguous(
        self, payment_fingerprint: str, settlement: dict[str, Any]
    ) -> None: ...


class InMemoryReceiptStore:
    """Non-production in-memory receipt store for tests and development only."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[str, ReceiptRecord] = {}

    async def begin(
        self, payment_fingerprint: str, request_fingerprint: str, route_id: str
    ) -> ReceiptBeginResult:
        async with self._lock:
            rec = self._records.get(payment_fingerprint)
            if rec is None:
                rec = ReceiptRecord(
                    payment_fingerprint=payment_fingerprint,
                    request_fingerprint=request_fingerprint,
                    route_id=route_id,
                    state="in_progress",
                )
                self._records[payment_fingerprint] = rec
                return ReceiptBeginResult("owner", rec)
            if rec.request_fingerprint != request_fingerprint or rec.route_id != route_id:
                return ReceiptBeginResult("conflict", rec)
            if rec.state == "completed":
                return ReceiptBeginResult("replay", rec)
            return ReceiptBeginResult("wait", rec)

    async def wait(self, record: ReceiptRecord) -> ReceiptRecord:
        await record._event.wait()
        return record

    async def mark_settled(self, payment_fingerprint: str, settlement: dict[str, Any]) -> None:
        async with self._lock:
            rec = self._records[payment_fingerprint]
            rec.state = "settled"
            rec.settlement = dict(settlement)
            rec.updated_at = time.time()

    async def mark_rejected(self, payment_fingerprint: str, error: str) -> None:
        async with self._lock:
            rec = self._records[payment_fingerprint]
            rec.state = "rejected"
            rec.settlement = {"error": error}
            rec.updated_at = time.time()
            rec._event.set()

    async def mark_completed(
        self,
        payment_fingerprint: str,
        *,
        response_status: int,
        response_headers: Mapping[str, str],
        response_body: bytes | None,
        resource_result_reference: str = "",
    ) -> None:
        async with self._lock:
            rec = self._records[payment_fingerprint]
            rec.state = "completed"
            rec.response_status = response_status
            rec.response_headers = dict(response_headers)
            rec.response_body = response_body
            rec.resource_result_reference = resource_result_reference
            rec.updated_at = time.time()
            rec._event.set()

    async def mark_handler_failed(self, payment_fingerprint: str) -> None:
        async with self._lock:
            rec = self._records[payment_fingerprint]
            rec.state = "handler_failed"
            rec.updated_at = time.time()
            rec._event.set()

    async def mark_ambiguous(self, payment_fingerprint: str, settlement: dict[str, Any]) -> None:
        async with self._lock:
            rec = self._records[payment_fingerprint]
            rec.state = "ambiguous"
            rec.settlement = dict(settlement)
            rec.updated_at = time.time()
            rec._event.set()


class SellerConfigurationError(ValueError):
    """Invalid seller configuration."""


class PaymentParsingError(ValueError):
    """Malformed Payment-Signature header."""

    def __init__(self, message: str, *, status: int = 402) -> None:
        super().__init__(message)
        self.status = status


async def _call_handler(
    handler: Callable[[web.Request], Awaitable[web.Response]], request: web.Request
) -> web.Response:
    return await handler(request)


def _parse_price(price: str | Decimal) -> str:
    """Parse USDC price to exact 6-decimal atomic units as a string."""
    if isinstance(price, bool):
        raise TypeError("price must not be a boolean")
    if callable(price):
        raise TypeError("price must be a string or Decimal, not a callable")
    if not isinstance(price, (str, Decimal)):
        raise TypeError("price must be a string or Decimal")

    raw = str(price).strip()
    if raw.startswith("$"):
        raw = raw[1:].strip()
    elif raw.startswith("-$"):
        raw = "-" + raw[2:].strip()
    elif raw.startswith("+$"):
        raw = raw[2:].strip()
    elif "$" in raw:
        raise ValueError(f"Cannot parse price: {price!r}")
    if not raw:
        raise ValueError("price must not be empty")

    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"Cannot parse price: {price!r}") from None

    if not parsed.is_finite():
        if parsed.is_nan():
            raise ValueError("price must not be NaN")
        raise ValueError("price must not be Infinity")
    if parsed < 0:
        raise ValueError(f"price must not be negative: {price!r}")
    if parsed == 0:
        raise ValueError("price must be greater than zero")

    exponent = parsed.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_USDC_DECIMALS:
        raise ValueError(
            f"price has excess precision (more than {_USDC_DECIMALS} decimals): {price!r}"
        )

    atomic = parsed * _USDC_MULTIPLIER
    if atomic != atomic.to_integral_exact():
        raise ValueError(f"price conversion is not exact: {price!r}")
    atomic_int = int(atomic)
    if atomic_int <= 0:
        raise ValueError(f"price too small (must be at least one atomic USDC unit): {price!r}")
    if atomic_int > _MAX_ATOMIC:
        raise ValueError(f"price exceeds maximum: {price!r}")
    return str(atomic_int)


def _validate_address(address: str, *, field_name: str = "address") -> None:
    if not isinstance(address, str) or not _ADDRESS_RE.match(address):
        raise ValueError(
            f"Invalid {field_name}: {address!r}. Must be 0x followed by 40 hex characters."
        )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _b64_json(value: Any) -> str:
    return base64.b64encode(_canonical_json_bytes(value)).decode("ascii")


def _ensure_json_depth(value: Any, *, max_depth: int = _MAX_JSON_DEPTH) -> None:
    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            raise PaymentParsingError("payment payload exceeds maximum JSON nesting")
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise PaymentParsingError("payment payload contains a non-string object key")
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    walk(value, 0)


def _decode_payment_header(encoded: str) -> dict[str, Any]:
    if not isinstance(encoded, str) or not encoded:
        raise PaymentParsingError("missing payment header")
    if len(encoded) > _MAX_ENCODED_PAYMENT_HEADER:
        raise PaymentParsingError("encoded payment header is too large", status=413)
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise PaymentParsingError("payment header is not strict base64") from None
    if len(raw) > _MAX_DECODED_PAYMENT_PAYLOAD:
        raise PaymentParsingError("decoded payment payload is too large", status=413)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PaymentParsingError("payment payload is not valid UTF-8") from None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        raise PaymentParsingError("payment payload is not valid JSON") from None
    if not isinstance(decoded, dict):
        raise PaymentParsingError("payment payload must be a JSON object")
    _ensure_json_depth(decoded)
    payload = decoded.get("payload")
    if not isinstance(payload, dict):
        raise PaymentParsingError("payment payload is missing payload object")
    if not isinstance(payload.get("authorization"), dict):
        raise PaymentParsingError("payment payload is missing authorization object")
    if not isinstance(payload.get("signature"), str) or not payload.get("signature"):
        raise PaymentParsingError("payment payload is missing signature")
    if not isinstance(decoded.get("accepted"), dict):
        raise PaymentParsingError("payment payload is missing accepted requirement")
    return decoded


def _normalize_path(path_qs: str) -> str:
    if not isinstance(path_qs, str) or not path_qs.startswith("/"):
        path_qs = "/" + str(path_qs or "")
    parts = urlsplit(path_qs)
    path = parts.path
    if ".." in path.split("/"):
        raise PaymentParsingError("path traversal not allowed")
    path = quote(path or "/", safe="/%:@")
    query = parts.query
    return urlunsplit(("", "", path, query, ""))


def _join_public_resource_path(base_path: str, request_path: str) -> str:
    """Join base URL path with request path, preserving the base prefix."""
    base_prefix = "/" + base_path.strip("/") if base_path.strip("/") else ""
    request_suffix = "/" + request_path.lstrip("/")

    combined = f"{base_prefix}{request_suffix}"

    normalized = _posixpath_normpath(combined)
    if not normalized.startswith("/"):
        normalized = "/" + normalized

    # Preserve trailing slash where it is semantically present.
    if combined.endswith("/") and not normalized.endswith("/"):
        normalized += "/"

    return normalized


def _posixpath_normpath(path: str) -> str:
    """Minimal posixpath-style normalization (split on /, collapse . and ..)."""
    parts: list[str] = []
    for segment in path.split("/"):
        if segment == "." or segment == "":
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/" + "/".join(parts)


def _build_resource_url(
    public_base_url: str,
    request: web.Request | None = None,
    path: str | None = None,
) -> str:
    base = urlsplit(public_base_url)

    raw_relative = (
        path if path is not None else getattr(request, "path_qs", getattr(request, "path", "/"))
    )
    relative = _normalize_path(raw_relative)
    relative_parts = urlsplit(relative)

    resource_path = _join_public_resource_path(
        base.path,
        relative_parts.path,
    )

    return urlunsplit(
        (
            base.scheme,
            base.netloc,
            resource_path,
            relative_parts.query,
            "",
        )
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _log_seller_rejection(stage: str, reason: str, **fields: Any) -> None:
    """Log only structural payment validation diagnostics.

    Deliberately excludes the raw payment header, payment payload, signature,
    nonce, authorization object, and request headers.
    """
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key.lower()
        not in {
            "payment-signature",
            "payment_signature",
            "paymentheader",
            "paymentpayload",
            "authorization",
            "signature",
            "nonce",
            "headers",
        }
    }
    logger.info(
        "x402 seller payment rejected stage=%s reason=%s fields=%s",
        stage,
        reason,
        safe_fields,
    )


def _log_timeout_compatibility(
    *, buyer_max_timeout_seconds: int, server_max_timeout_seconds: int
) -> None:
    """Log sanitized timeout compatibility metadata only.

    Do not include payment payload, authorization, signature, nonce, or request
    headers. This exists to diagnose Circle CLI normalization without exposing
    authorization material.
    """
    logger.debug(
        "x402 seller timeout compatibility fields=%s",
        {
            "buyer_maxTimeoutSeconds": buyer_max_timeout_seconds,
            "server_maxTimeoutSeconds": server_max_timeout_seconds,
        },
    )


def _validate_buyer_timeout(raw_timeout: Any, server_min_timeout: int) -> int:
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
        raise ValueError("invalid maxTimeoutSeconds")
    if raw_timeout < server_min_timeout:
        raise ValueError("maxTimeoutSeconds below server minimum")
    if raw_timeout > BUYER_MAX_TIMEOUT_SECONDS:
        raise ValueError("maxTimeoutSeconds above defensive maximum")
    return raw_timeout


def _validation_stage_from_error(exc: ValueError) -> str:
    reason = str(exc)
    if reason == "wrong x402Version":
        return "x402Version mismatch"
    if reason in {"invalid authorization value", "wrong amount"}:
        return "authorization value mismatch"
    if (
        reason.startswith("wrong ")
        or reason.startswith("missing accepted")
        or "maxTimeoutSeconds" in reason
    ):
        return "selected requirement mismatch"
    return "payload schema rejection"


class CircleFacilitatorClient:
    """Typed Circle Gateway facilitator client for direct settle()."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owned_client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._owned_client is None:
            timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
            self._owned_client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                max_redirects=0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._owned_client

    async def settle(
        self, payment_payload: dict[str, Any], payment_requirements: dict[str, Any]
    ) -> FacilitatorSettlementResult:
        url = f"{self.base_url}/v1/x402/settle"
        body = {"paymentPayload": payment_payload, "paymentRequirements": payment_requirements}
        try:
            response = await self._get_client().post(
                url, json=body, headers={"Content-Type": "application/json"}
            )
        except httpx.TimeoutException as exc:
            return FacilitatorSettlementResult(
                FacilitatorOutcome.FACILITATOR_UNAVAILABLE, error=type(exc).__name__
            )
        except httpx.TransportError as exc:
            return FacilitatorSettlementResult(
                FacilitatorOutcome.FACILITATOR_UNAVAILABLE, error=type(exc).__name__
            )

        if response.is_redirect:
            return FacilitatorSettlementResult(
                FacilitatorOutcome.INVALID_FACILITATOR_RESPONSE,
                error="facilitator redirects are not allowed",
                http_status=response.status_code,
            )
        if response.status_code == 429:
            return FacilitatorSettlementResult(
                FacilitatorOutcome.RATE_LIMITED, error="rate limited", http_status=429
            )
        if response.status_code >= 500:
            return FacilitatorSettlementResult(
                FacilitatorOutcome.FACILITATOR_UNAVAILABLE,
                error="facilitator server error",
                http_status=response.status_code,
            )

        raw = response.content
        if len(raw) > _MAX_FACILITATOR_RESPONSE:
            return FacilitatorSettlementResult(
                FacilitatorOutcome.INVALID_FACILITATOR_RESPONSE,
                error="facilitator response too large",
                http_status=response.status_code,
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return FacilitatorSettlementResult(
                FacilitatorOutcome.INVALID_FACILITATOR_RESPONSE,
                error="facilitator response is not valid JSON",
                http_status=response.status_code,
            )
        if not isinstance(data, dict):
            return FacilitatorSettlementResult(
                FacilitatorOutcome.INVALID_FACILITATOR_RESPONSE,
                error="facilitator response must be an object",
                http_status=response.status_code,
            )

        if response.status_code >= 400:
            reason = str(
                data.get("errorReason")
                or data.get("error")
                or data.get("message")
                or "payment rejected"
            )[:200]
            lower = reason.lower()
            if "nonce" in lower or "replay" in lower or "duplicate" in lower or "conflict" in lower:
                return FacilitatorSettlementResult(
                    FacilitatorOutcome.REPLAY_OR_CONFLICT,
                    error=reason,
                    http_status=response.status_code,
                )
            if response.status_code in {408, 425}:
                return FacilitatorSettlementResult(
                    FacilitatorOutcome.AMBIGUOUS, error=reason, http_status=response.status_code
                )
            return FacilitatorSettlementResult(
                FacilitatorOutcome.PAYMENT_REJECTED, error=reason, http_status=response.status_code
            )

        success = data.get("success")
        if success is not True:
            reason = str(data.get("errorReason") or data.get("error") or "payment rejected")[:200]
            lower = reason.lower()
            outcome = (
                FacilitatorOutcome.REPLAY_OR_CONFLICT
                if any(x in lower for x in ("nonce", "replay", "duplicate", "conflict"))
                else FacilitatorOutcome.PAYMENT_REJECTED
            )
            return FacilitatorSettlementResult(
                outcome, error=reason, http_status=response.status_code
            )
        transaction = data.get("transaction") or data.get("txHash") or data.get("transactionHash")
        if not isinstance(transaction, str) or not transaction:
            return FacilitatorSettlementResult(
                FacilitatorOutcome.INVALID_FACILITATOR_RESPONSE,
                error="success response missing transaction",
                http_status=response.status_code,
            )
        payer = data.get("payer") if isinstance(data.get("payer"), str) else ""
        return FacilitatorSettlementResult(
            FacilitatorOutcome.SUCCESS,
            success=True,
            transaction=transaction,
            payer=payer,
            http_status=response.status_code,
            retry_safe=False,
        )


class X402Gateway:
    """Canonical aiohttp gateway/decorator for x402 paid routes."""

    def __init__(
        self,
        seller_address: str,
        networks: list[NetworkConfig | str],
        facilitator_url: str,
        default_description: str,
        *,
        public_base_url: str | None = None,
        allow_http: bool = False,
        receipt_store: ReceiptStore | None = None,
        after_settlement: Callable[[web.Request, PaymentResult], Awaitable[None] | None]
        | None = None,
        on_settlement_ambiguous: Callable[[web.Request, dict[str, Any]], Awaitable[None] | None]
        | None = None,
        on_paid_handler_error: Callable[
            [web.Request, PaymentResult, BaseException], Awaitable[None] | None
        ]
        | None = None,
        facilitator_client: CircleFacilitatorClient | None = None,
    ) -> None:
        _validate_address(seller_address, field_name="seller address")
        resolved_networks = _resolve_seller_networks(networks)
        if not resolved_networks:
            raise SellerConfigurationError("At least one network must be specified")
        self._seller_address = seller_address
        self._networks = resolved_networks
        self._facilitator_url = _validate_facilitator_url(facilitator_url, resolved_networks)
        self._default_description = default_description
        self._public_base_url = _resolve_public_base_url(
            public_base_url,
            allow_insecure_http=allow_http,
        )
        self._allow_http = allow_http
        self._receipt_store = receipt_store
        self._after_settlement = after_settlement
        self._on_settlement_ambiguous = on_settlement_ambiguous
        self._on_paid_handler_error = on_paid_handler_error
        self._facilitator_client = facilitator_client or CircleFacilitatorClient(
            self._facilitator_url
        )
        self._default_accepts = self._build_accepts(resolved_networks, None)

    def require(
        self,
        price: str | Decimal | Callable[[web.Request], str | Decimal] | None = None,
        *,
        networks: list[str] | None = None,
        description: str | None = None,
        route_id: str | None = None,
    ) -> Callable[
        [Callable[[web.Request], Awaitable[web.Response]]],
        Callable[[web.Request], Awaitable[web.Response]],
    ]:
        resolved_networks: list[NetworkConfig] | None = None
        if networks is not None:
            resolved_networks = _resolve_seller_networks(networks)
        route_accepts = self._build_accepts(resolved_networks, None) if resolved_networks else None
        route_desc = description or self._default_description

        def decorator(
            handler: Callable[[web.Request], Awaitable[web.Response]],
        ) -> Callable[[web.Request], Awaitable[web.Response]]:
            return self._wrap_handler(
                handler,
                price=price,
                networks=resolved_networks,
                accepts=route_accepts,
                description=route_desc,
                route_id=route_id,
            )

        return decorator

    def _wrap_handler(
        self,
        handler: Callable[[web.Request], Awaitable[web.Response]],
        price: str | Decimal | Callable[[web.Request], str | Decimal] | None,
        networks: list[NetworkConfig] | None,
        accepts: list[dict[str, Any]] | None,
        description: str,
        route_id: str | None = None,
    ) -> Callable[[web.Request], Awaitable[web.Response]]:
        gateway = self

        async def x402_wrapped(request: web.Request) -> web.Response:
            return await gateway._handle_request(
                request,
                lambda req: _call_handler(handler, req),
                price,
                networks,
                accepts,
                description,
                route_id=route_id,
            )

        x402_wrapped.__wrapped_handler__ = handler  # type: ignore[attr-defined]
        return x402_wrapped

    async def _handle_request(
        self,
        request: web.Request,
        handler_call: Callable[[web.Request], Awaitable[web.Response]],
        price_spec: str | Decimal | Callable[[web.Request], str | Decimal] | None,
        networks: list[NetworkConfig] | None,
        accepts: list[dict[str, Any]] | None,
        description: str,
        *,
        route_id: str | None = None,
        preserve_payment_context: bool = False,
    ) -> web.Response:
        try:
            amount = self._resolve_amount(request, price_spec)
        except (ValueError, TypeError) as exc:
            logger.error(
                "Invalid seller price configuration for route %s: %s",
                getattr(request, "path", ""),
                exc,
            )
            return web.json_response({"error": "invalid_seller_price"}, status=500)

        route_networks = networks if networks is not None else self._networks
        route_accepts = accepts if accepts is not None else self._default_accepts
        challenge = self._build_402_body(
            amount, description, route_networks, route_accepts, request=request
        )
        payment_header = request.headers.get(PAYMENT_SIGNATURE_HEADER)
        if not payment_header:
            _log_seller_rejection("payment header missing", "missing Payment-Signature")
            return self._challenge_response(challenge)

        try:
            decoded = _decode_payment_header(payment_header)
            selected_requirement = self._validate_selected_requirement(
                decoded, amount, route_networks
            )
        except PaymentParsingError as exc:
            if exc.status == 413:
                _log_seller_rejection("payload schema rejection", str(exc))
                return web.json_response({"error": "payment_payload_too_large"}, status=413)
            stage = (
                "strict Base64 rejection"
                if str(exc) == "payment header is not strict base64"
                else "payload schema rejection"
            )
            _log_seller_rejection(stage, str(exc))
            return self._challenge_response(challenge)
        except ValueError as exc:
            _log_seller_rejection(_validation_stage_from_error(exc), str(exc))
            return self._challenge_response(challenge)

        payment_fingerprint = _fingerprint(decoded)
        raw_method = getattr(request, "method", "GET")
        method = raw_method if isinstance(raw_method, str) else "GET"
        request_fingerprint = _fingerprint(
            {
                "method": method,
                "resource_url": challenge["resource"]["url"],
                "amount": amount,
                "network": selected_requirement["network"],
            }
        )
        route_hint = getattr(request, "match_info", None)
        route_value = ""
        if isinstance(route_hint, dict):
            route_value = str(route_hint.get("route", ""))
        raw_path = getattr(request, "path", "")
        path_value = raw_path if isinstance(raw_path, str) else ""
        effective_route_id = route_id or route_value or path_value
        begin: ReceiptBeginResult | None = None
        if self._receipt_store is not None:
            begin = await self._receipt_store.begin(
                payment_fingerprint, request_fingerprint, effective_route_id
            )
            if begin.action == "conflict":
                return web.json_response(
                    {"error": "payment_reused_for_different_request"}, status=409
                )
            if begin.action in {"replay", "wait"}:
                rec = (
                    begin.record
                    if begin.action == "replay"
                    else await self._receipt_store.wait(begin.record)
                )
                if rec.state == "completed" and rec.response_status is not None:
                    return web.Response(
                        status=rec.response_status,
                        body=rec.response_body,
                        headers=rec.response_headers,
                    )
                if rec.state == "handler_failed":
                    return web.json_response(
                        {"error": "paid_handler_failed", "retry_safe": False}, status=500
                    )
                if rec.state == "ambiguous":
                    return web.json_response(
                        {"error": "settlement_outcome_unknown", "retry_safe": False}, status=503
                    )
                if rec.state == "rejected":
                    return self._challenge_response(challenge)
                return web.json_response({"error": "settlement_state_conflict"}, status=409)

        requirements = self._build_settle_requirements(
            amount, selected_requirement["network"], route_networks
        )
        try:
            settle_result = await self._settle(decoded, requirements)
        except Exception as exc:
            logger.error("Settle failed: %s", type(exc).__name__)
            if self._receipt_store is not None:
                await self._receipt_store.mark_ambiguous(
                    payment_fingerprint, {"error": type(exc).__name__}
                )
            return web.json_response({"error": "facilitator_unavailable"}, status=503)
        if isinstance(settle_result, dict):
            settle_result = _coerce_legacy_settle_result(settle_result)

        if settle_result.outcome == FacilitatorOutcome.AMBIGUOUS:
            if self._receipt_store is not None:
                await self._receipt_store.mark_ambiguous(
                    payment_fingerprint, {"error": settle_result.error}
                )
            await _maybe_await(
                self._on_settlement_ambiguous, request, {"error": settle_result.error}
            )
            return web.json_response(
                {"error": "settlement_outcome_unknown", "retry_safe": False}, status=503
            )
        if settle_result.outcome == FacilitatorOutcome.REPLAY_OR_CONFLICT:
            if self._receipt_store is not None:
                await self._receipt_store.mark_ambiguous(
                    payment_fingerprint, {"error": settle_result.error}
                )
            return web.json_response({"error": "payment_replay_or_conflict"}, status=409)
        if settle_result.outcome in {
            FacilitatorOutcome.RATE_LIMITED,
            FacilitatorOutcome.FACILITATOR_UNAVAILABLE,
            FacilitatorOutcome.INVALID_FACILITATOR_RESPONSE,
        }:
            if self._receipt_store is not None:
                await self._receipt_store.mark_ambiguous(
                    payment_fingerprint, {"error": settle_result.error}
                )
            return web.json_response(
                {"error": "facilitator_unavailable"},
                status=503
                if settle_result.outcome != FacilitatorOutcome.INVALID_FACILITATOR_RESPONSE
                else 502,
            )
        if not settle_result.success:
            _log_seller_rejection("facilitator rejection", settle_result.error)
            if self._receipt_store is not None:
                await self._receipt_store.mark_rejected(payment_fingerprint, settle_result.error)
            return self._challenge_response(challenge)

        authorization = decoded["payload"]["authorization"]
        payer = settle_result.payer or authorization.get("from", "")
        result = PaymentResult(
            payer=payer,
            amount=amount,
            network=selected_requirement["network"],
            transaction=settle_result.transaction,
        )
        set_x402_payment(request, result)
        if self._receipt_store is not None:
            await self._receipt_store.mark_settled(
                payment_fingerprint,
                {
                    "transaction": result.transaction,
                    "payer": result.payer,
                    "network": result.network,
                },
            )
        await _maybe_await(self._after_settlement, request, result)

        token = set_payment_context_token(
            payer=result.payer,
            amount=result.amount,
            network=result.network,
            transaction=result.transaction,
        )
        try:
            response = await handler_call(request)
        except Exception as exc:
            if self._receipt_store is not None:
                await self._receipt_store.mark_handler_failed(payment_fingerprint)
            await _maybe_await(self._on_paid_handler_error, request, result, exc)
            logger.error("Paid handler failed after settlement: %s", type(exc).__name__)
            return web.json_response(
                {"error": "paid_handler_failed", "retry_safe": False}, status=500
            )
        finally:
            if not preserve_payment_context:
                reset_payment_context(token)

        if isinstance(response, web.Response):
            response.headers[PAYMENT_RESPONSE_HEADER] = _b64_json(
                {
                    "success": True,
                    "transaction": result.transaction or "",
                    "network": result.network,
                    "payer": result.payer,
                }
            )
        if self._receipt_store is not None:
            body = getattr(response, "body", None)
            await self._receipt_store.mark_completed(
                payment_fingerprint,
                response_status=response.status,
                response_headers=response.headers,
                response_body=body if isinstance(body, bytes) else None,
            )
        return response

    def _resolve_amount(
        self,
        request: web.Request,
        price_spec: str | Decimal | Callable[[web.Request], str | Decimal] | None,
    ) -> str:
        if callable(price_spec):
            return _parse_price(price_spec(request))
        if price_spec is None:
            raise ValueError("No price specified for require()")
        return _parse_price(price_spec)

    def _build_accepts(
        self, networks: list[NetworkConfig] | None, override_amount: str | None
    ) -> list[dict[str, Any]]:
        accepts: list[dict[str, Any]] = []
        for net in networks or []:
            accepts.append(
                {
                    "scheme": CIRCLE_BATCHING_SCHEME,
                    "network": net.caip2,
                    "asset": net.usdc_address,
                    "amount": override_amount or "0",
                    "payTo": self._seller_address,
                    "maxTimeoutSeconds": DEFAULT_MAX_TIMEOUT_SECONDS,
                    "extra": {
                        "name": CIRCLE_BATCHING_NAME,
                        "version": CIRCLE_BATCHING_VERSION,
                        "verifyingContract": net.gateway_wallet,
                    },
                }
            )
        return accepts

    def _build_402_body(
        self,
        amount: str,
        description_or_path: str,
        networks_or_description: list[NetworkConfig] | str,
        accepts_or_networks: list[dict[str, Any]] | list[NetworkConfig] | None = None,
        *,
        request: web.Request | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Build x402 v2 challenge body.

        Supports the old private signature ``(amount, path, description,
        networks, accepts=None)`` sufficiently for compatibility tests and the
        new canonical call shape used by this class.
        """
        if isinstance(networks_or_description, str):
            path = description_or_path
            description = networks_or_description
            networks = (
                accepts_or_networks if isinstance(accepts_or_networks, list) else self._networks
            )
            accepts = self._build_accepts(networks, amount)  # type: ignore[arg-type]
        else:
            description = description_or_path
            networks = networks_or_description
            accepts = (
                copy.deepcopy(accepts_or_networks)
                if accepts_or_networks is not None
                else self._build_accepts(networks, amount)
            )  # type: ignore[arg-type]
            for entry in accepts:
                entry["amount"] = amount
        resource_url = _build_resource_url(self._public_base_url, request=request, path=path)
        return {
            "x402Version": X402_VERSION,
            "resource": {
                "url": resource_url,
                "description": description,
                "mimeType": "application/json",
            },
            "accepts": accepts,
        }

    def _build_settle_requirements(
        self, amount: str, network: str, networks: list[NetworkConfig]
    ) -> dict[str, Any]:
        for net in networks:
            if net.caip2 == network:
                return {
                    "scheme": CIRCLE_BATCHING_SCHEME,
                    "network": net.caip2,
                    "asset": net.usdc_address,
                    "amount": amount,
                    "payTo": self._seller_address,
                    "maxTimeoutSeconds": DEFAULT_MAX_TIMEOUT_SECONDS,
                    "extra": {
                        "name": CIRCLE_BATCHING_NAME,
                        "version": CIRCLE_BATCHING_VERSION,
                        "verifyingContract": net.gateway_wallet,
                    },
                }
        raise ValueError(f"Network {network!r} not in accepted networks")

    def _validate_selected_requirement(
        self, decoded: dict[str, Any], amount: str, networks: list[NetworkConfig]
    ) -> dict[str, Any]:
        if decoded.get("x402Version") != X402_VERSION:
            raise ValueError("wrong x402Version")
        accepted = decoded["accepted"]
        authorization = decoded["payload"]["authorization"]
        signature = decoded["payload"].get("signature")
        if not isinstance(signature, str) or not signature:
            raise ValueError("missing signature")
        if not isinstance(authorization.get("value"), (str, int)):
            raise ValueError("invalid authorization value")
        if str(authorization.get("value")) != amount:
            raise ValueError("wrong amount")
        payer = authorization.get("from")
        _validate_address(payer, field_name="payer")

        expected = self._build_settle_requirements(
            str(amount), str(accepted.get("network", "")), networks
        )
        for key in ("scheme", "network", "asset", "amount", "payTo"):
            if accepted.get(key) != expected.get(key):
                raise ValueError(f"wrong {key}")
        buyer_timeout = _validate_buyer_timeout(
            accepted.get("maxTimeoutSeconds"), expected["maxTimeoutSeconds"]
        )
        if buyer_timeout != expected["maxTimeoutSeconds"]:
            _log_timeout_compatibility(
                buyer_max_timeout_seconds=buyer_timeout,
                server_max_timeout_seconds=expected["maxTimeoutSeconds"],
            )
        extra = accepted.get("extra")
        if not isinstance(extra, dict):
            raise ValueError("missing accepted extra")
        expected_extra = expected["extra"]
        for key in ("name", "version", "verifyingContract"):
            if extra.get(key) != expected_extra.get(key):
                raise ValueError(f"wrong extra.{key}")
        _validate_address(accepted["asset"], field_name="asset")
        _validate_address(accepted["payTo"], field_name="payTo")
        _validate_address(extra["verifyingContract"], field_name="verifyingContract")
        return accepted

    async def _settle(
        self, payload: dict[str, Any], requirements: dict[str, Any]
    ) -> FacilitatorSettlementResult:
        return await self._facilitator_client.settle(payload, requirements)

    def _challenge_response(self, body: dict[str, Any]) -> web.Response:
        return web.json_response(
            body, status=402, headers={PAYMENT_REQUIRED_HEADER: _b64_json(body)}
        )


async def _maybe_await(fn: Callable[..., Awaitable[None] | None] | None, *args: Any) -> None:
    if fn is None:
        return
    result = fn(*args)
    if hasattr(result, "__await__"):
        await result  # type: ignore[misc]


def _coerce_legacy_settle_result(value: dict[str, Any]) -> FacilitatorSettlementResult:
    if value.get("success") is True:
        tx = value.get("transaction") or value.get("txHash") or value.get("transactionHash") or ""
        return FacilitatorSettlementResult(
            FacilitatorOutcome.SUCCESS,
            success=True,
            transaction=str(tx),
            payer=str(value.get("payer") or ""),
        )
    reason = str(value.get("errorReason") or value.get("error") or "payment rejected")
    lower = reason.lower()
    if any(term in lower for term in ("nonce", "replay", "duplicate", "conflict")):
        return FacilitatorSettlementResult(FacilitatorOutcome.REPLAY_OR_CONFLICT, error=reason)
    return FacilitatorSettlementResult(FacilitatorOutcome.PAYMENT_REJECTED, error=reason)


def _validate_public_base_url(value: str, *, allow_http: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SellerConfigurationError("X402_PUBLIC_BASE_URL is required for seller mode")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise SellerConfigurationError("X402_PUBLIC_BASE_URL must be an absolute URL")
    if parsed.scheme != "https" and not allow_http:
        raise SellerConfigurationError("X402_PUBLIC_BASE_URL must use HTTPS unless allow_http=True")
    if parsed.query:
        raise SellerConfigurationError("X402_PUBLIC_BASE_URL must not contain a query string")
    if parsed.fragment:
        raise SellerConfigurationError("X402_PUBLIC_BASE_URL must not contain a fragment")
    if parsed.username or parsed.password:
        raise SellerConfigurationError("X402_PUBLIC_BASE_URL must not contain userinfo")
    # Preserve the path prefix — only strip trailing slash.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _resolve_public_base_url(
    explicit: str | None,
    *,
    allow_insecure_http: bool = False,
) -> str:
    """Resolve and validate the public base URL.

    Uses *explicit* first, then ``X402_PUBLIC_BASE_URL`` env var.
    Raises ``SellerConfigurationError`` when neither is set.
    """
    value = explicit or os.environ.get("X402_PUBLIC_BASE_URL")

    if not value:
        raise SellerConfigurationError(
            "A public seller URL is required. Pass public_base_url=... or set X402_PUBLIC_BASE_URL."
        )

    return _validate_public_base_url(
        value,
        allow_http=allow_insecure_http,
    )


def _validate_facilitator_url(value: str, networks: list[NetworkConfig]) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme != "https" or not parsed.netloc:
        raise SellerConfigurationError("facilitator_url must be an HTTPS absolute URL")
    envs = {n.environment for n in networks}
    if len(envs) != 1:
        raise SellerConfigurationError("Cannot mix mainnet and testnet seller networks")
    normalized = value.rstrip("/")
    if envs == {"testnet"} and normalized == _MAINNET_FACILITATOR:
        raise SellerConfigurationError("testnet seller networks must not use mainnet facilitator")
    if envs == {"mainnet"} and normalized == _TESTNET_FACILITATOR:
        raise SellerConfigurationError("mainnet seller networks must not use testnet facilitator")
    return normalized


def _validate_seller_network(cfg: NetworkConfig) -> NetworkConfig:
    """Validate a single network config for seller capability."""
    if not cfg.seller_supported:
        raise SellerConfigurationError(f"Network {cfg.key!r} is not supported for seller mode")
    missing: list[str] = []
    if not cfg.caip2:
        missing.append("caip2")
    if not cfg.usdc_address:
        missing.append("usdc_address")
    if not cfg.gateway_wallet:
        missing.append("gateway_wallet")
    if not cfg.facilitator_url:
        missing.append("facilitator_url")
    if not cfg.environment:
        missing.append("environment")
    if not cfg.gateway_supported:
        missing.append("gateway_supported")
    if missing:
        raise SellerConfigurationError(
            f"Network {cfg.key!r} has incomplete seller configuration: " + ", ".join(missing)
        )
    return cfg


def _resolve_seller_networks(
    networks: list[str | NetworkConfig] | None,
) -> list[NetworkConfig]:
    """Resolve seller networks from string keys or NetworkConfig objects.

    Defaults to Arc Testnet when *networks* is ``None``.  Accepts mixed
    lists of strings and ``NetworkConfig`` instances — all inputs pass
    through the same capability validation.
    """
    requested = networks if networks is not None else ["arcTestnet"]

    if not requested:
        raise SellerConfigurationError("At least one seller network must be configured")

    resolved: list[NetworkConfig] = []
    seen: set[str] = set()

    for item in requested:
        if isinstance(item, str):
            try:
                cfg = get_network(item)
            except NetworkNotFoundError as exc:
                raise SellerConfigurationError(f"Unknown seller network: {item!r}") from exc
        elif isinstance(item, NetworkConfig):
            cfg = item
        else:
            raise SellerConfigurationError(
                "Seller networks must contain only network names or NetworkConfig instances"
            )

        cfg = _validate_seller_network(cfg)

        if cfg.key not in seen:
            resolved.append(cfg)
            seen.add(cfg.key)

    environments = {cfg.environment for cfg in resolved}
    if len(environments) != 1:
        raise SellerConfigurationError(
            "Seller networks cannot mix mainnet and testnet environments"
        )

    return resolved


def create_aiohttp_gateway(
    seller_address: str,
    networks: list[str] | None = None,
    facilitator_url: str | None = None,
    default_description: str = "Paid resource",
    *,
    public_base_url: str | None = None,
    allow_http: bool = False,
    receipt_store: ReceiptStore | None = None,
    after_settlement: Callable[[web.Request, PaymentResult], Awaitable[None] | None] | None = None,
    on_settlement_ambiguous: Callable[[web.Request, dict[str, Any]], Awaitable[None] | None]
    | None = None,
    on_paid_handler_error: Callable[
        [web.Request, PaymentResult, BaseException], Awaitable[None] | None
    ]
    | None = None,
    facilitator_client: CircleFacilitatorClient | None = None,
) -> X402Gateway:
    """Create the canonical aiohttp x402 seller gateway.

    Defaults to Arc Testnet.  Seller mode intentionally rejects unsupported or
    unverified networks instead of silently falling back to mainnet.
    """
    resolved = _resolve_seller_networks(networks)
    if facilitator_url is None:
        facilitator_url = resolved[0].facilitator_url
    return X402Gateway(
        seller_address=seller_address,
        networks=resolved,
        facilitator_url=facilitator_url,
        default_description=default_description,
        public_base_url=public_base_url,
        allow_http=allow_http,
        receipt_store=receipt_store,
        after_settlement=after_settlement,
        on_settlement_ambiguous=on_settlement_ambiguous,
        on_paid_handler_error=on_paid_handler_error,
        facilitator_client=facilitator_client,
    )
