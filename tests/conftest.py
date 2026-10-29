"""Offline fixtures: a mocked x402 seller, a fixed test signer and no network at all."""

import base64
import json
import socket

import pytest
import requests

import nanopy

from hermes_nano_x402 import NanoX402Client, NanopySigner, Policy
from hermes_nano_x402.signer import AccountState

# Public test-only seed (all zeros). Never holds funds; used only to make signatures offline.
TEST_SEED = "0" * 64
PAY_TO = "nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"
OTHER = nanopy.Account(sk=nanopy.deterministic_key("1" * 64, 0)).addr
ONE_MILLI_RAW = 10**27  # Ӿ0.001
FRONTIER = "A" * 64
REP = "nano_3arg3asgtigae3xckabaaewkx3bzsh7nwz7jkmjos79ihyaxwphhm6qgjps4"
BALANCE = 10**28  # Ӿ0.01


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a non-loopback socket fails the test."""
    real = socket.socket.connect

    def guarded(self, addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach the network: {addr!r}")
        return real(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    for k in ("NANO_SEED", "NANO_PRIVATE_KEY", "NANO_RPC", "NANO_WORK_URL"):
        monkeypatch.delenv(k, raising=False)


def b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def make_quote(url="https://seller.test/v1/hash", amount=str(ONE_MILLI_RAW), pay_to=PAY_TO,
               network="nano:mainnet", scheme="exact", mts=60, work_optional=True, **extra_over):
    extra = {"workOptional": work_optional, "workThreshold": "fffffff800000000"}
    extra.update(extra_over)
    return {
        "x402Version": 2,
        "resource": {"url": url, "description": "test", "mimeType": "application/json"},
        "accepts": [{"scheme": scheme, "network": network, "amount": amount, "asset": "XNO",
                     "payTo": pay_to, "maxTimeoutSeconds": mts, "extra": extra}],
    }


def response(status, body="", headers=None, url=""):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    r.headers.update(headers or {})
    r.url = url
    r.encoding = "utf-8"
    return r


class Seller:
    """Mocked x402 seller. ``routes[url]`` is a callable(method, headers, body) -> Response."""

    def __init__(self):
        self.calls = []  # (method, url, headers)
        self.routes = {}
        self.paid_status = 200
        self.quote_factory = lambda url: make_quote(url)

    def x402(self, url):
        def handler(method, headers, body):
            sig = headers.get("PAYMENT-SIGNATURE")
            if not sig:
                return response(402, json.dumps({"error": "payment required"}),
                                {"PAYMENT-REQUIRED": b64(self.quote_factory(url))}, url)
            settle = b64({"success": True, "network": "nano:mainnet"})
            return response(self.paid_status, json.dumps({"hash": "ok"}),
                            {"PAYMENT-RESPONSE": settle, "content-type": "application/json"}, url)
        self.routes[url] = handler
        return url

    def paid_calls(self):
        return [c for c in self.calls if "PAYMENT-SIGNATURE" in c[2]]


class FakeSession:
    def __init__(self, seller):
        self.seller = seller

    def request(self, method, url, data=None, headers=None, allow_redirects=True, timeout=None):
        assert allow_redirects is False, "client must never let requests follow redirects"
        headers = dict(headers or {})
        self.seller.calls.append((method, url, headers))
        h = self.seller.routes.get(url)
        if h is None:
            return response(404, "not found", url=url)
        return h(method, headers, data)


class CountingSigner:
    """Wraps the real nanopy signer and counts signatures."""

    def __init__(self):
        self.inner = NanopySigner.from_seed(TEST_SEED, 0)
        self.count = 0
        self.blocks = []

    @property
    def address(self):
        return self.inner.address

    def sign_send(self, state, pay_to, amount_raw, work):
        self.count += 1
        sb = self.inner.sign_send(state, pay_to, amount_raw, work)
        self.blocks.append(sb)
        return sb


@pytest.fixture
def seller():
    return Seller()


@pytest.fixture
def signer():
    return CountingSigner()


@pytest.fixture
def make_client(seller, signer):
    def _make(allowed=(PAY_TO,), per_call="0.001", total="0.01", work=None, clock=None, **kw):
        policy = Policy.build(allowed, per_call, total)
        extra = {}
        if clock is not None:
            extra["clock"] = clock
        return NanoX402Client(
            policy,
            signer=signer,
            session=FakeSession(seller),
            account_info=lambda addr: AccountState(FRONTIER, BALANCE, REP),
            work=work or (lambda root, thr: (_ for _ in ()).throw(AssertionError("work not expected"))),
            **extra,
            **kw,
        )
    return _make
