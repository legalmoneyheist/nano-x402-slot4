"""Guarded x402 ``exact`` / ``nano:mainnet`` client.

Flow per call (one call = at most one signed send block):

1. origin check, duplicate-request check;
2. unsigned request; same-origin redirects are followed (bounded), a cross-origin redirect
   stops the call before anything is signed;
3. a non-402 answer is returned as is (nothing paid);
4. the 402 quote is parsed and validated (malformed or wrong network refused);
5. the guard reserves the amount (payTo allowlist, per-call cap, cumulative cap, request key);
6. account state and, when ``extra.workOptional`` is not true, proof of work are obtained;
7. if the quote is older than ``maxTimeoutSeconds`` it is discarded and re-fetched (and the
   new quote goes through the guard again); a quote is never re-used;
8. exactly one block is signed and presented to the URL that answered 402, with redirects off;
9. whatever comes back is returned. There is no automatic paid retry after a non-2xx.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin

import requests

from .errors import MalformedQuote, NanoX402Error, PaymentRefused
from .guard import PaymentGuard, Policy, raw_to_xno
from .quote import Quote, extract_payment_required, same_origin, select_quote
from .signer import (
    WORK_PLACEHOLDER,
    AccountInfoSource,
    AccountState,
    NanopySigner,
    NanoSigner,
    WorkProvider,
)

MAX_REDIRECTS = 10
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")


@dataclass
class PayResult:
    ok: bool
    status: Optional[int]
    paid: bool
    url: str
    amount_raw: int = 0
    block_hash: Optional[str] = None
    pay_to: Optional[str] = None
    body: str = ""
    content_type: Optional[str] = None
    settlement: Optional[dict] = None
    refused: Optional[str] = None
    message: Optional[str] = None
    spent_total_raw: int = 0

    def to_dict(self, max_body: int = 4000) -> dict:
        d = {
            "ok": self.ok,
            "status": self.status,
            "paid": self.paid,
            "url": self.url,
            "amount_xno": raw_to_xno(self.amount_raw) if self.amount_raw else "0",
            "amount_raw": str(self.amount_raw),
            "block_hash": self.block_hash,
            "pay_to": self.pay_to,
            "content_type": self.content_type,
            "body": self.body[:max_body] + ("…" if len(self.body) > max_body else ""),
            "spent_total_xno": raw_to_xno(self.spent_total_raw) if self.spent_total_raw else "0",
        }
        if self.settlement is not None:
            d["settlement"] = self.settlement
        if self.refused:
            d["refused"] = self.refused
        if self.message:
            d["message"] = self.message
        return d


def request_key(method: str, url: str, body: Optional[bytes], request_id: Optional[str]) -> str:
    """Identity of a request for the no-second-block rule. An explicit ``request_id`` wins;
    otherwise method + URL + body hash."""
    if request_id:
        return "id:" + str(request_id)
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\0")
    h.update(url.encode())
    h.update(b"\0")
    h.update(hashlib.sha256(body or b"").digest())
    return "req:" + h.hexdigest()


def _b64json(obj) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def _decode_settlement(headers) -> Optional[dict]:
    for k, v in headers.items():
        if k.lower() == "payment-response":
            try:
                return json.loads(base64.b64decode(v))
            except Exception:
                return {"raw": v[:200]}
    return None


class NanoX402Client:
    def __init__(
        self,
        policy: Policy,
        signer: Optional[NanoSigner] = None,
        signer_factory: Optional[Callable[[], NanoSigner]] = None,
        session: Optional[requests.Session] = None,
        account_info: Optional[Callable[[str], AccountState]] = None,
        work: Optional[Callable[[str, str], str]] = None,
        timeout: float = 30.0,
        freshness_margin: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.guard = PaymentGuard(policy)
        self._signer = signer
        self._signer_factory = signer_factory or NanopySigner.from_env
        self.session = session or requests.Session()
        self.account_info = account_info or AccountInfoSource.from_env(session=self.session)
        self.work = work or WorkProvider.from_env(session=self.session)
        self.timeout = timeout
        self.freshness_margin = freshness_margin
        self.clock = clock
        self._pay_lock = threading.Lock()  # one writer per account in this process
        self.signed_blocks = 0

    @classmethod
    def from_env(cls, env=None, **kw) -> "NanoX402Client":
        return cls(Policy.from_env(env), **kw)

    @property
    def signer(self) -> NanoSigner:
        if self._signer is None:
            self._signer = self._signer_factory()
        return self._signer

    # ------------------------------------------------------------------ HTTP helpers

    def _send(self, method, url, body, headers):
        return self.session.request(method, url, data=body, headers=headers,
                                    allow_redirects=False, timeout=self.timeout)

    def _probe(self, method, url, body, headers):
        """Unsigned request. Follows same-origin redirects only. Returns (response, final_url)."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self.guard.check_origin(current)
            r = self._send(method, current, body, headers)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if not loc:
                    return r, current
                nxt = urljoin(current, loc)
                if not same_origin(nxt, url):
                    raise PaymentRefused("cross_origin_redirect",
                                         "the request redirected to another origin; it is never paid")
                current = nxt
                continue
            return r, current
        raise PaymentRefused("too_many_redirects", "too many redirects before the 402")

    def _quote(self, method, url, body, headers):
        r, final = self._probe(method, url, body, headers)
        if r.status_code != 402:
            return r, final, None
        doc = extract_payment_required(r.headers, r.text)
        q = select_quote(doc, final)
        # Re-stamp with this client's clock (tests may inject one).
        q = Quote(**{**q.__dict__, "received_at": self.clock()})
        return r, final, (q, doc)

    # ------------------------------------------------------------------ the paid call

    def pay(self, url: str, method: str = "GET", body=None, headers: Optional[dict] = None,
            request_id: Optional[str] = None) -> PayResult:
        method = (method or "GET").upper()
        if method not in ALLOWED_METHODS:
            return PayResult(False, None, False, url, refused="bad_method", message=f"method {method} not allowed")
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = {k: v for k, v in (headers or {}).items()
                   if k.lower() not in ("payment-signature", "x-payment", "x-nano-payment")}
        key = request_key(method, url, body, request_id)
        try:
            self.guard.check_origin(url)
            self.guard.check_key(key)
        except PaymentRefused as e:
            return PayResult(False, None, False, url, refused=e.code, message=str(e),
                             spent_total_raw=self.guard.spent_raw)

        with self._pay_lock:
            return self._pay_locked(url, method, body, headers, key)

    def _pay_locked(self, url, method, body, headers, key) -> PayResult:
        reserved = False
        try:
            self.guard.check_key(key)
            r, final, qd = self._quote(method, url, body, headers)
            if qd is None:
                return PayResult(200 <= r.status_code < 300, r.status_code, False, final, body=r.text,
                                 content_type=r.headers.get("content-type"),
                                 spent_total_raw=self.guard.spent_raw,
                                 message="no payment was asked for")
            quote, doc = qd
            self.guard.reserve(key, quote)
            reserved = True

            signer = self.signer
            state = self.account_info(signer.address)
            if state.balance_raw < quote.amount_raw:
                raise PaymentRefused("insufficient_balance",
                                     f"balance {raw_to_xno(state.balance_raw)} XNO is below the quote")

            work_for = None

            def get_work(q: Quote) -> str:
                nonlocal work_for
                if q.work_optional:
                    return WORK_PLACEHOLDER
                if work_for is None or work_for[1] != q.work_threshold:
                    work_for = (self.work(state.frontier, q.work_threshold), q.work_threshold)
                return work_for[0]

            work = get_work(quote)

            # Freshness: a quote older than maxTimeoutSeconds is never used; fetch a new one.
            if not quote.is_fresh(self.freshness_margin, self.clock()):
                self.guard.release(key)
                reserved = False
                r, final2, qd = self._quote(method, url, body, headers)
                if qd is None:
                    return PayResult(200 <= r.status_code < 300, r.status_code, False, final2, body=r.text,
                                     content_type=r.headers.get("content-type"),
                                     spent_total_raw=self.guard.spent_raw,
                                     message="quote expired; re-fetch asked for no payment")
                if final2 != final:
                    raise PaymentRefused("quote_moved", "re-fetched quote came from a different URL")
                quote, doc = qd
                self.guard.reserve(key, quote)
                reserved = True
                if state.balance_raw < quote.amount_raw:
                    raise PaymentRefused("insufficient_balance", "balance is below the re-fetched quote")
                work = get_work(quote)
                if not quote.is_fresh(self.freshness_margin, self.clock()):
                    raise PaymentRefused("quote_expired", "re-fetched quote expired before signing")

            signed = signer.sign_send(state, quote.pay_to, quote.amount_raw, work)
            self.guard.mark_signed(key, signed.hash)
            reserved = False
            self.signed_blocks += 1

            payload = {"x402Version": 2, "accepted": quote.accepted, "payload": {"block": signed.block}}
            if quote.resource is not None:
                payload["resource"] = quote.resource
            paid_headers = dict(headers)
            paid_headers["PAYMENT-SIGNATURE"] = _b64json(payload)
            try:
                pr = self._send(method, final, body, paid_headers)
            except requests.RequestException as e:
                self.guard.record_status(key, None)
                return PayResult(False, None, True, final, amount_raw=quote.amount_raw,
                                 block_hash=signed.hash, pay_to=quote.pay_to,
                                 spent_total_raw=self.guard.spent_raw,
                                 message=f"no reply after the payment was sent ({type(e).__name__}); "
                                         "check the block hash before doing anything else; not retried")
            self.guard.record_status(key, pr.status_code)
            ok = 200 <= pr.status_code < 300
            return PayResult(ok, pr.status_code, True, final, amount_raw=quote.amount_raw,
                             block_hash=signed.hash, pay_to=quote.pay_to, body=pr.text,
                             content_type=pr.headers.get("content-type"),
                             settlement=_decode_settlement(pr.headers),
                             spent_total_raw=self.guard.spent_raw,
                             message=None if ok else "paid request was not served; not retried automatically")
        except PaymentRefused as e:
            if reserved:
                self.guard.release(key)
            return PayResult(False, None, False, url, refused=e.code, message=str(e),
                             spent_total_raw=self.guard.spent_raw)
        except (NanoX402Error, requests.RequestException, ValueError, KeyError) as e:
            if reserved:
                self.guard.release(key)
            return PayResult(False, None, False, url, refused="error",
                             message=f"{type(e).__name__}: {e}"[:500], spent_total_raw=self.guard.spent_raw)
