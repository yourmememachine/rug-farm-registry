"""x402 pay-per-check for the Rug-Farm Registry (v2.18).

Ported from SolanaOS (github.com/x402agent/solanaos, ``pkg/x402``):
HTTP 402 pay-per-call in Solana USDC via the public facilitator
(``facilitator.x402.rs``, ``/verify`` and ``/settle``), default
$0.001 per call. SolanaOS wraps the x402-go SDK; this module
implements the same server-side flow in stdlib + httpx, because
there is no Python x402 SDK in this repo:

    no X-Payment header  -> 402 + payment requirements ("accepts")
    X-Payment header     -> POST /verify, then POST /settle
    settled              -> serve the endpoint, record the payment

Payments are DISABLED unless ``RUGFARM_X402_PAYEE`` is set. No
payee configured means no gating, ever — the API behaves exactly
as today, free. A facilitator outage means the paid endpoints
return 402 with a clear "payment verification unavailable" (or
"payment settlement unavailable") reason — paid content is never
served unverified, and nothing is charged without settling.

Replay protection: a settled payment's transaction signature is
recorded in the SQLite ``payments`` table (UNIQUE per signature,
scoped to the endpoint it paid for). A replayed X-Payment header
whose signature is already recorded for that endpoint is served
immediately, without calling /verify or /settle again — the same
payment is never settled twice.

A payment is never accepted for a different amount, asset or
payee than the requirement: the requirement is sent to the
facilitator with every /verify and /settle call (the facilitator
checks the signed transaction against it), and any amount /
asset / payee fields the payload itself carries are checked
against the requirement before the facilitator is even called.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from fastapi import HTTPException, Request

from . import db as db_mod
from .db import DEFAULT_DB_PATH
from .http_client import make_client

# Solana mainnet USDC (6 decimals), the only asset accepted.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
NETWORK = "solana"

DEFAULT_FACILITATOR_URL = "https://facilitator.x402.rs"
DEFAULT_PRICE_USDC = "0.001"
MAX_TIMEOUT_SECONDS = 60
X402_VERSION = 1

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def usdc_to_base_units(price_usdc: str) -> int:
    """USDC amount ("0.001") -> base units (1000, 6 decimals).

    Raises ValueError on a bad, zero or negative price — a
    misconfigured price must never silently become free or a
    different amount.
    """
    try:
        amount = Decimal(str(price_usdc).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise ValueError(f"invalid x402 price {price_usdc!r}") from exc
    base_units = amount * (10 ** USDC_DECIMALS)
    if base_units <= 0 or base_units != int(base_units):
        raise ValueError(
            f"invalid x402 price {price_usdc!r}: must be a positive "
            f"amount with at most {USDC_DECIMALS} decimals"
        )
    return int(base_units)


def _b58encode(data: bytes) -> str:
    """Base58-encode bytes (Bitcoin alphabet, leading zeros -> '1')."""
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    leading = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading + out


def _parse_payment_header(raw: str) -> dict:
    """Parse an X-Payment header value into a payment payload dict.

    x402 v1 sends the payload as base64-encoded JSON; a plain
    JSON value is accepted too. Anything else is not a payment.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty X-Payment header")
    candidates = [text]
    padded = text + "=" * (-len(text) % 4)
    try:
        candidates.append(base64.b64decode(padded).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        pass
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("X-Payment header is not valid JSON")


def _extract_signature(payload: dict) -> str | None:
    """The transaction signature a payment payload carries, if any.

    Checked in order: an explicit ``signature`` field (top level
    or inside the nested ``payload``), then the first signature of
    a base64-encoded serialized Solana transaction in
    ``payload.transaction`` (one byte count, then 64-byte
    signatures). Returns None when the payload carries no
    recoverable signature — the payment then cannot be replay-
    checked and is deduped only by the settle response.
    """
    for candidate in (payload.get("signature"),):
        if isinstance(candidate, str) and candidate:
            return candidate
    inner = payload.get("payload")
    if isinstance(inner, dict):
        sig = inner.get("signature")
        if isinstance(sig, str) and sig:
            return sig
        tx = inner.get("transaction")
        if isinstance(tx, str) and tx:
            try:
                raw = base64.b64decode(tx + "=" * (-len(tx) % 4))
            except (binascii.Error, ValueError):
                return None
            if len(raw) >= 65 and raw[0] == 1:
                return _b58encode(raw[1:65])
    return None


def _payload_field(payload: dict, field: str) -> str | None:
    """An amount/asset/payee field, top level or nested, if present."""
    value = payload.get(field)
    if isinstance(value, str) and value:
        return value
    inner = payload.get("payload")
    if isinstance(inner, dict):
        value = inner.get(field)
        if isinstance(value, str) and value:
            return value
    return None


class PaymentRequired(Exception):
    """Raised when a paid endpoint cannot be served; carries the 402 body."""

    def __init__(self, body: dict) -> None:
        super().__init__(body.get("error", "Payment required"))
        self.body = body


class X402Gate:
    """x402 gate for the valuable API endpoints.

    ``payee`` is the Solana wallet that receives the USDC. When
    it is None (the default — ``RUGFARM_X402_PAYEE`` unset), the
    gate is disabled and ``authorize`` always passes the request
    through, exactly as the API behaved before v2.18.
    """

    def __init__(
        self,
        *,
        payee: str | None = None,
        price_usdc: str = DEFAULT_PRICE_USDC,
        facilitator: str = DEFAULT_FACILITATOR_URL,
        db_path: str | Path | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.payee = (payee or "").strip() or None
        self.price_usdc = price_usdc
        self.amount_base_units = usdc_to_base_units(price_usdc)
        self.facilitator = (facilitator or DEFAULT_FACILITATOR_URL).rstrip("/")
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self._http_client = http_client

    @classmethod
    def from_env(cls, **overrides) -> "X402Gate":
        """Build a gate from the environment (plus any overrides)."""
        return cls(
            payee=os.environ.get("RUGFARM_X402_PAYEE", ""),
            price_usdc=os.environ.get(
                "RUGFARM_X402_PRICE_USDC", DEFAULT_PRICE_USDC
            ),
            facilitator=os.environ.get(
                "RUGFARM_X402_FACILITATOR", DEFAULT_FACILITATOR_URL
            ),
            **overrides,
        )

    @property
    def enabled(self) -> bool:
        """True only when a payee is configured — no payee, no gating."""
        return self.payee is not None

    # -- requirements -------------------------------------------------

    def requirement(self, endpoint: str) -> dict:
        """The single payment requirement for one endpoint."""
        return {
            "scheme": "exact",
            "network": NETWORK,
            "amount": str(self.amount_base_units),
            "asset": USDC_MINT,
            "payTo": self.payee,
            "payee": self.payee,
            "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
            "description": (
                f"Rug-Farm Registry {endpoint} — "
                f"${self.price_usdc} USDC per check"
            ),
            "mimeType": "application/json",
        }

    def _required_body(self, endpoint: str, reason: str) -> dict:
        """The x402 402 body: version, reason, and the accepts list."""
        return {
            "x402Version": X402_VERSION,
            "error": reason,
            "accepts": [self.requirement(endpoint)],
        }

    # -- authorisation ------------------------------------------------

    def authorize(self, headers, endpoint: str) -> dict:
        """Authorise one request to a paid endpoint.

        Returns ``{"paid": bool, "signature": str | None, ...}``.
        Raises :class:`PaymentRequired` (carrying the 402 body)
        when the request must not be served.
        """
        if not self.enabled:
            return {"paid": False, "endpoint": endpoint, "signature": None}
        raw = headers.get("x-payment")
        if raw is None:
            raise PaymentRequired(
                self._required_body(endpoint, "Payment required")
            )
        try:
            payload = _parse_payment_header(raw)
        except ValueError as exc:
            raise PaymentRequired(
                self._required_body(endpoint, f"Invalid X-Payment header: {exc}")
            ) from exc
        self._check_payload_matches_requirement(payload, endpoint)
        requirement = self.requirement(endpoint)

        # Replay check: a signature already settled for this
        # endpoint is served immediately, without re-verifying or
        # re-settling. A signature recorded for a *different*
        # endpoint does not authorise this one.
        signature = _extract_signature(payload)
        if signature is not None:
            with closing(db_mod.init_db(self.db_path)) as conn:
                record = db_mod.get_payment(conn, signature)
            if record is not None and record.get("endpoint") == endpoint:
                return {
                    "paid": True,
                    "endpoint": endpoint,
                    "signature": signature,
                    "replayed": True,
                    "amount_base_units": record.get("amount_base_units"),
                }

        body = {
            "x402Version": X402_VERSION,
            "paymentPayload": payload,
            "paymentRequirements": requirement,
        }
        verify = self._facilitator_post(
            "/verify",
            body,
            endpoint=endpoint,
            unavailable_reason="payment verification unavailable",
        )
        if not isinstance(verify, dict) or not verify.get("isValid"):
            reason = ""
            if isinstance(verify, dict):
                reason = str(verify.get("invalidReason") or "")
            raise PaymentRequired(
                self._required_body(
                    endpoint, reason or "Payment verification failed"
                )
            )
        settle = self._facilitator_post(
            "/settle",
            body,
            endpoint=endpoint,
            unavailable_reason="payment settlement unavailable",
        )
        if not isinstance(settle, dict) or not settle.get("success"):
            reason = ""
            if isinstance(settle, dict):
                reason = str(settle.get("errorReason") or "")
            raise PaymentRequired(
                self._required_body(
                    endpoint, reason or "Payment settlement failed"
                )
            )
        settled_signature = (
            settle.get("transaction") or settle.get("txHash") or signature
        )
        if settled_signature:
            with closing(db_mod.init_db(self.db_path)) as conn:
                db_mod.record_payment(
                    conn,
                    settled_signature,
                    self.amount_base_units,
                    endpoint,
                    payer=verify.get("payer"),
                )
        return {
            "paid": True,
            "endpoint": endpoint,
            "signature": settled_signature,
            "replayed": False,
            "amount_base_units": self.amount_base_units,
        }

    def require_payment(self, request: Request) -> dict:
        """FastAPI dependency: gate one request, or raise HTTP 402."""
        try:
            return self.authorize(request.headers, request.url.path)
        except PaymentRequired as exc:
            raise HTTPException(status_code=402, detail=exc.body) from exc

    # -- internals ----------------------------------------------------

    def _check_payload_matches_requirement(
        self, payload: dict, endpoint: str
    ) -> None:
        """Reject a payload that disagrees with the requirement.

        A real x402 Solana payload carries no amount/asset/payee
        (the signed transaction does, and the facilitator checks
        it against the requirement) — these checks only fire when
        the payload itself states a value. A mismatch is a 402,
        never a silent accept.
        """
        network = payload.get("network")
        if isinstance(network, str) and network and network != NETWORK:
            raise PaymentRequired(
                self._required_body(
                    endpoint,
                    f"Payment network {network!r} does not match {NETWORK!r}",
                )
            )
        scheme = payload.get("scheme")
        if isinstance(scheme, str) and scheme and scheme != "exact":
            raise PaymentRequired(
                self._required_body(
                    endpoint,
                    f"Payment scheme {scheme!r} does not match 'exact'",
                )
            )
        amount = _payload_field(payload, "amount")
        if amount is not None and amount != str(self.amount_base_units):
            raise PaymentRequired(
                self._required_body(
                    endpoint,
                    "Payment amount does not match the requirement",
                )
            )
        asset = _payload_field(payload, "asset")
        if asset is not None and asset != USDC_MINT:
            raise PaymentRequired(
                self._required_body(
                    endpoint,
                    "Payment asset does not match the requirement",
                )
            )
        payee = _payload_field(payload, "payTo") or _payload_field(
            payload, "payee"
        )
        if payee is not None and payee != self.payee:
            raise PaymentRequired(
                self._required_body(
                    endpoint,
                    "Payment payee does not match the requirement",
                )
            )

    def _facilitator_post(
        self, path: str, body: dict, *, endpoint: str, unavailable_reason: str
    ) -> dict:
        """POST one JSON body to the facilitator; returns its JSON.

        A transport failure, a non-JSON reply, or a non-200 status
        is an outage, not a verdict: the caller gets a 402 with
        the unavailable reason, and paid content is never served
        on the strength of a failed check.
        """
        url = self.facilitator + path
        try:
            if self._http_client is not None:
                resp = self._http_client.post(url, json=body, timeout=30.0)
            else:
                with make_client(timeout=30.0) as client:
                    resp = client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise PaymentRequired(
                self._required_body(endpoint, unavailable_reason)
            ) from exc
        if resp.status_code != 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if isinstance(data, dict):
                reason = str(
                    data.get("invalidReason")
                    or data.get("errorReason")
                    or data.get("error")
                    or ""
                )
                if reason:
                    raise PaymentRequired(self._required_body(endpoint, reason))
            raise PaymentRequired(
                self._required_body(endpoint, unavailable_reason)
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise PaymentRequired(
                self._required_body(endpoint, unavailable_reason)
            ) from exc
        if not isinstance(data, dict):
            raise PaymentRequired(
                self._required_body(endpoint, unavailable_reason)
            )
        return data
