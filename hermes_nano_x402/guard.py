"""The payment guard. Every check here runs before anything is signed.

* network must be ``nano:mainnet`` and scheme ``exact`` (enforced in ``quote.select_quote``);
* ``payTo`` must be in the operator's allowlist (an empty allowlist refuses everything);
* the amount must be at or below the per-call cap;
* the amount plus everything already spent or reserved in this process must stay at or
  below the cumulative cap;
* a request key that has already been reserved or paid is never paid again.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, FrozenSet, Iterable, Optional
from urllib.parse import urlsplit

from x402_nano_exact import RAW_PER_XNO, normalize_nano_address

from .errors import PaymentRefused
from .quote import Quote


def xno_to_raw(value) -> int:
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation as e:
        raise ValueError(f"not a decimal XNO amount: {value!r}") from e
    if d < 0:
        raise ValueError("XNO amount must not be negative")
    raw = d * RAW_PER_XNO
    if raw != raw.to_integral_value():
        raise ValueError(f"XNO amount has more than 30 decimals: {value!r}")
    return int(raw)


def raw_to_xno(raw: int) -> str:
    s = format(Decimal(raw) / Decimal(RAW_PER_XNO), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _origin_key(url: str) -> str:
    p = urlsplit(url)
    port = p.port or {"https": 443, "http": 80}.get((p.scheme or "").lower())
    return f"{(p.scheme or '').lower()}://{(p.hostname or '').lower()}:{port}"


@dataclass
class Policy:
    """Operator-set spending policy. Amounts are in raw."""

    allowed_pay_to: FrozenSet[str]
    max_per_call_raw: int
    max_total_raw: int
    allowed_origins: Optional[FrozenSet[str]] = None  # None = any origin (payTo allowlist still applies)

    @classmethod
    def build(
        cls,
        allowed_pay_to: Iterable[str],
        max_per_call_xno="0.001",
        max_total_xno="0.01",
        allowed_origins: Optional[Iterable[str]] = None,
    ) -> "Policy":
        pay_to = frozenset(normalize_nano_address(a) for a in allowed_pay_to if str(a).strip())
        origins = None
        if allowed_origins is not None:
            origins = frozenset(_origin_key(o.strip()) for o in allowed_origins if o.strip())
        return cls(
            allowed_pay_to=pay_to,
            max_per_call_raw=xno_to_raw(max_per_call_xno),
            max_total_raw=xno_to_raw(max_total_xno),
            allowed_origins=origins,
        )

    @classmethod
    def from_env(cls, env=None) -> "Policy":
        """NANO_X402_ALLOWED_PAYTO (comma list, required), NANO_X402_MAX_PER_CALL_XNO (default 0.001),
        NANO_X402_MAX_TOTAL_XNO (default 0.01), NANO_X402_ALLOWED_ORIGINS (optional comma list)."""
        env = os.environ if env is None else env
        split = lambda s: [x for x in (s or "").replace(" ", ",").split(",") if x]
        origins = env.get("NANO_X402_ALLOWED_ORIGINS")
        return cls.build(
            allowed_pay_to=split(env.get("NANO_X402_ALLOWED_PAYTO", "")),
            max_per_call_xno=env.get("NANO_X402_MAX_PER_CALL_XNO", "0.001"),
            max_total_xno=env.get("NANO_X402_MAX_TOTAL_XNO", "0.01"),
            allowed_origins=split(origins) if origins else None,
        )

    def describe(self) -> dict:
        return {
            "allowed_pay_to": sorted(self.allowed_pay_to),
            "max_per_call_xno": raw_to_xno(self.max_per_call_raw),
            "max_total_xno": raw_to_xno(self.max_total_raw),
            "allowed_origins": sorted(self.allowed_origins) if self.allowed_origins is not None else "any",
        }


@dataclass
class _Entry:
    state: str  # "reserved" | "signed"
    amount_raw: int
    block_hash: Optional[str] = None
    status: Optional[int] = None


@dataclass
class PaymentGuard:
    """Holds the policy and the per-process spend ledger. Thread-safe."""

    policy: Policy
    _spent_raw: int = 0
    _reserved_raw: int = 0
    _ledger: Dict[str, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def spent_raw(self) -> int:
        return self._spent_raw

    def check_origin(self, url: str) -> None:
        p = urlsplit(url)
        if p.scheme not in ("https", "http") or not p.hostname:
            raise PaymentRefused("bad_url", "only absolute http(s) URLs can be paid")
        if self.policy.allowed_origins is not None and _origin_key(url) not in self.policy.allowed_origins:
            raise PaymentRefused("origin_not_allowed", f"origin {_origin_key(url)} is not in the allowlist")

    def check_key(self, key: str) -> None:
        with self._lock:
            e = self._ledger.get(key)
            if e is not None:
                raise PaymentRefused(
                    "duplicate_request",
                    f"this request was already {e.state}"
                    + (f" with block {e.block_hash}" if e.block_hash else "")
                    + "; a second block is never signed for it",
                )

    def reserve(self, key: str, quote: Quote) -> None:
        """All guard checks, then an atomic reservation of the amount under ``key``."""
        p = self.policy
        if not p.allowed_pay_to:
            raise PaymentRefused("pay_to_not_allowed", "payTo allowlist is empty; refusing every payment")
        if quote.pay_to not in p.allowed_pay_to:
            raise PaymentRefused("pay_to_not_allowed", f"payTo {quote.pay_to} is not in the allowlist")
        if quote.amount_raw > p.max_per_call_raw:
            raise PaymentRefused(
                "over_per_call_cap",
                f"quoted {raw_to_xno(quote.amount_raw)} XNO is above the per-call cap of "
                f"{raw_to_xno(p.max_per_call_raw)} XNO",
            )
        with self._lock:
            if key in self._ledger:
                e = self._ledger[key]
                raise PaymentRefused(
                    "duplicate_request",
                    f"this request was already {e.state}; a second block is never signed for it",
                )
            committed = self._spent_raw + self._reserved_raw
            if committed + quote.amount_raw > p.max_total_raw:
                raise PaymentRefused(
                    "over_total_cap",
                    f"cumulative cap of {raw_to_xno(p.max_total_raw)} XNO reached "
                    f"({raw_to_xno(committed)} XNO already spent or reserved)",
                )
            self._reserved_raw += quote.amount_raw
            self._ledger[key] = _Entry("reserved", quote.amount_raw)

    def release(self, key: str) -> None:
        """Undo a reservation that never reached signing."""
        with self._lock:
            e = self._ledger.get(key)
            if e is not None and e.state == "reserved":
                self._reserved_raw -= e.amount_raw
                del self._ledger[key]

    def mark_signed(self, key: str, block_hash: str) -> None:
        """Called the moment a block is signed. From here the key can never be paid again and the
        amount counts as spent, whatever happens to the request."""
        with self._lock:
            e = self._ledger[key]
            if e.state != "reserved":
                raise PaymentRefused("duplicate_request", "request already signed")
            self._reserved_raw -= e.amount_raw
            self._spent_raw += e.amount_raw
            e.state = "signed"
            e.block_hash = block_hash

    def record_status(self, key: str, status: Optional[int]) -> None:
        with self._lock:
            if key in self._ledger:
                self._ledger[key].status = status
