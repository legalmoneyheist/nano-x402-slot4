"""Offline fixtures: no network, a mocked x402 seller and a fixed test signer."""

import socket

import pytest

from hermes_nano_x402 import NanoX402Client, Policy
from hermes_nano_x402.signer import AccountState
from hermes_nano_x402.tests.helpers import BALANCE, FRONTIER, PAY_TO, REP, CountingSigner, FakeSession, Seller


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
