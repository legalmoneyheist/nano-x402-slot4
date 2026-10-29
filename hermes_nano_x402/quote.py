"""Parse and validate an x402 v2 402 challenge for the ``exact`` scheme on ``nano:mainnet``.

Address checks and constants come from the ``x402-nano-exact`` package (the pursekeeper
reference implementation of the scheme for the x402 Python SDK).
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from x402_nano_exact import (
    ASSET_XNO,
    NETWORK_NANO_MAINNET,
    SCHEME_EXACT,
    SEND_BLOCK_WORK_THRESHOLD,
    normalize_nano_address,
)

from .errors import MalformedQuote, PaymentRefused

MAX_NANO_RAW = (1 << 128) - 1
MAX_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class Quote:
    """One accepted payment option, plus when this client received it."""

    accepted: dict
    resource: Optional[dict]
    x402_version: int
    pay_to: str
    amount_raw: int
    max_timeout_seconds: int
    work_optional: bool
    work_threshold: str
    received_at: float = field(default_factory=time.monotonic)

    def age(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.received_at

    def is_fresh(self, margin: float = 2.0, now: Optional[float] = None) -> bool:
        """True while the quote is inside ``maxTimeoutSeconds`` (minus a safety margin)."""
        return self.age(now) < max(0.0, self.max_timeout_seconds - margin)


def _origin(url: str) -> tuple:
    p = urlsplit(url)
    port = p.port or {"https": 443, "http": 80}.get((p.scheme or "").lower())
    return ((p.scheme or "").lower(), (p.hostname or "").lower(), port)


def same_origin(a: str, b: str) -> bool:
    return _origin(a) == _origin(b)


def _decode_header(value: str) -> Any:
    if len(value) > MAX_HEADER_BYTES:
        raise MalformedQuote("PAYMENT-REQUIRED header too large")
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        try:
            raw = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
        except (binascii.Error, ValueError) as e:
            raise MalformedQuote("PAYMENT-REQUIRED header is not base64") from e
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise MalformedQuote("PAYMENT-REQUIRED header is not base64 JSON") from e


def extract_payment_required(headers, body_text: str) -> dict:
    """Return the x402 v2 PaymentRequired object from a 402 response (header first, body fallback)."""
    hdr = None
    for k, v in headers.items():
        if k.lower() == "payment-required":
            hdr = v
            break
    if hdr:
        doc = _decode_header(hdr)
    else:
        try:
            body = json.loads(body_text or "")
        except json.JSONDecodeError as e:
            raise MalformedQuote("402 carries neither a PAYMENT-REQUIRED header nor a JSON body") from e
        if isinstance(body, dict) and isinstance(body.get("x402"), dict):
            doc = body["x402"]
        else:
            doc = body
    if not isinstance(doc, dict):
        raise MalformedQuote("payment requirements are not a JSON object")
    return doc


def _check_accept(a: Any) -> None:
    if not isinstance(a, dict):
        raise MalformedQuote("accepts entry is not an object")


def select_quote(doc: dict, request_url: str) -> Quote:
    """Validate the PaymentRequired document and pick the exact/nano:mainnet entry.

    Raises ``MalformedQuote`` for any shape problem and ``PaymentRefused('wrong_network')``
    when no entry is ``exact`` on ``nano:mainnet``. Nothing is signed by this function.
    """
    version = doc.get("x402Version")
    if version != 2 or isinstance(version, bool):
        raise MalformedQuote(f"x402Version must be 2, got {version!r}")
    accepts = doc.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise MalformedQuote("accepts must be a non-empty list")
    for a in accepts:
        _check_accept(a)

    resource = doc.get("resource")
    if resource is not None:
        if not isinstance(resource, dict) or not isinstance(resource.get("url"), str) or not resource["url"]:
            raise MalformedQuote("resource must be an object with a url")
        if not same_origin(resource["url"], request_url):
            raise PaymentRefused(
                "resource_origin_mismatch",
                "the 402 names a resource on another origin than the one requested",
            )

    exact = [a for a in accepts if a.get("scheme") == SCHEME_EXACT]
    nano = [a for a in exact if a.get("network") == NETWORK_NANO_MAINNET]
    if not nano:
        offered = sorted({f"{a.get('scheme')}/{a.get('network')}" for a in accepts})
        raise PaymentRefused(
            "wrong_network",
            f"no 'exact' offer on {NETWORK_NANO_MAINNET}; offered: {', '.join(offered)}",
        )
    a = nano[0]

    if str(a.get("asset", "")).upper() != ASSET_XNO:
        raise MalformedQuote(f"asset must be {ASSET_XNO}")
    amount = a.get("amount")
    if not isinstance(amount, str) or not amount.isdigit() or amount.startswith("0") or not amount.isascii():
        raise MalformedQuote("amount must be a positive integer string in raw")
    amount_raw = int(amount)
    if amount_raw > MAX_NANO_RAW:
        raise MalformedQuote("amount exceeds the Nano supply ceiling")
    pay_to_raw = a.get("payTo")
    if not isinstance(pay_to_raw, str):
        raise MalformedQuote("payTo missing")
    try:
        pay_to = normalize_nano_address(pay_to_raw)
    except ValueError as e:
        raise MalformedQuote("payTo is not a valid Nano address") from e
    mts = a.get("maxTimeoutSeconds")
    if not isinstance(mts, int) or isinstance(mts, bool) or mts <= 0 or mts > 3600:
        raise MalformedQuote("maxTimeoutSeconds must be a positive integer (<= 3600)")
    extra = a.get("extra", {})
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise MalformedQuote("extra must be an object")
    wo = extra.get("workOptional", None)
    if wo is not None and not isinstance(wo, bool):
        raise MalformedQuote("extra.workOptional must be a boolean")
    work_optional = wo is True
    threshold = extra.get("workThreshold", SEND_BLOCK_WORK_THRESHOLD)
    if not isinstance(threshold, str) or len(threshold) != 16:
        raise MalformedQuote("extra.workThreshold must be 16 hex characters")
    try:
        int(threshold, 16)
    except ValueError as e:
        raise MalformedQuote("extra.workThreshold must be 16 hex characters") from e
    # Never accept a threshold easier than the protocol's send threshold.
    if int(threshold, 16) < int(SEND_BLOCK_WORK_THRESHOLD, 16):
        threshold = SEND_BLOCK_WORK_THRESHOLD

    return Quote(
        accepted=a,
        resource=resource,
        x402_version=2,
        pay_to=pay_to,
        amount_raw=amount_raw,
        max_timeout_seconds=mts,
        work_optional=work_optional,
        work_threshold=threshold.lower(),
    )
