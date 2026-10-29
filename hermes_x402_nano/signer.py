"""Signers and chain helpers. Signing is done by ``nanopy`` (block construction, blake2b, ed25519).

Key material is read from the environment (or injected by the operator) and kept in a closure;
it never appears in repr(), logs, exceptions or tool output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import nanopy
import nanopy.ext as _ext
import requests

from .errors import NanoX402Error, SignerUnavailable

_HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")
WORK_PLACEHOLDER = "0000000000000000"


@dataclass(frozen=True)
class AccountState:
    frontier: str
    balance_raw: int
    representative: str


@dataclass(frozen=True)
class SignedBlock:
    block: dict
    hash: str


class NanoSigner(Protocol):
    @property
    def address(self) -> str: ...

    def sign_send(self, state: AccountState, pay_to: str, amount_raw: int, work: str) -> SignedBlock: ...


class NanopySigner:
    """Signs state send blocks with a private key held only inside this object."""

    __slots__ = ("_sign", "_receive", "_address")

    def __init__(self, private_key_hex: str):
        if not isinstance(private_key_hex, str) or not _HEX64.match(private_key_hex):
            raise SignerUnavailable("private key must be 64 hex characters")
        acct = nanopy.Account(sk=private_key_hex.lower())
        address = acct.addr

        def _sign(state: AccountState, pay_to: str, amount_raw: int, work: str) -> SignedBlock:
            a = nanopy.Account(sk=private_key_hex.lower())
            a.frontier = state.frontier.lower()
            a.raw_bal = int(state.balance_raw)
            a.rep = nanopy.Account(addr=state.representative)
            b = a.send(nanopy.Account(addr=pay_to), int(amount_raw), work=work)
            d = dict(b.dict_)
            d["account"] = d["account"].replace("xrb_", "nano_", 1)
            d["representative"] = d["representative"].replace("xrb_", "nano_", 1)
            d["previous"] = d["previous"].upper()
            d["link"] = d["link"].upper()
            d["signature"] = d["signature"].upper()
            return SignedBlock(block=d, hash=b.hash_.upper())

        def _receive(state: Optional[AccountState], send_hash: str, amount_raw: int, representative: str,
                     work: str) -> SignedBlock:
            a = nanopy.Account(sk=private_key_hex.lower())
            if state is not None:
                a.frontier = state.frontier.lower()
                a.raw_bal = int(state.balance_raw)
                a.rep = nanopy.Account(addr=state.representative)
            rep = nanopy.Account(addr=representative) if representative else None
            b = a.receive(send_hash.lower(), int(amount_raw), rep=rep, work=work)
            d = dict(b.dict_)
            for k in ("account", "representative"):
                d[k] = d[k].replace("xrb_", "nano_", 1)
            for k in ("previous", "link", "signature"):
                d[k] = d[k].upper()
            return SignedBlock(block=d, hash=b.hash_.upper())

        self._sign = _sign
        self._receive = _receive
        self._address = address

    @property
    def address(self) -> str:
        return self._address

    def sign_send(self, state: AccountState, pay_to: str, amount_raw: int, work: str) -> SignedBlock:
        return self._sign(state, pay_to, amount_raw, work)

    def sign_receive(self, state: Optional[AccountState], send_hash: str, amount_raw: int,
                     representative: str, work: str) -> SignedBlock:
        """Receive (or, with ``state=None``, open) block. Used only by the operator's receive command."""
        return self._receive(state, send_hash, amount_raw, representative, work)

    @property
    def public_key(self) -> str:
        return nanopy.Account(addr=self._address).pk.upper()

    def __repr__(self) -> str:
        return f"NanopySigner(address={self._address!r}, key=<redacted>)"

    __str__ = __repr__

    def __reduce__(self):  # refuse pickling, which would copy the key out
        raise TypeError("NanopySigner cannot be pickled")

    @classmethod
    def from_seed(cls, seed_hex: str, index: int = 0) -> "NanopySigner":
        if not isinstance(seed_hex, str) or not _HEX64.match(seed_hex):
            raise SignerUnavailable("seed must be 64 hex characters")
        return cls(nanopy.deterministic_key(seed_hex.lower(), int(index)))

    @classmethod
    def from_env(cls, env=None) -> "NanopySigner":
        """NANO_PRIVATE_KEY (64 hex), or NANO_SEED (64 hex) with optional NANO_INDEX (default 0)."""
        env = os.environ if env is None else env
        pk = env.get("NANO_PRIVATE_KEY")
        if pk:
            return cls(pk.strip())
        seed = env.get("NANO_SEED")
        if seed:
            return cls.from_seed(seed.strip(), int(env.get("NANO_INDEX", "0") or 0))
        raise SignerUnavailable("no signer: set NANO_SEED (or NANO_PRIVATE_KEY) in the environment")


def signer_configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("NANO_SEED") or env.get("NANO_PRIVATE_KEY"))


# --------------------------------------------------------------------------- chain state


class AccountInfoSource:
    """Reads the confirmed frontier, balance and representative of the paying account.

    Uses a node RPC when ``rpc_url`` is set (``NANO_RPC``), else a no-node ``/v1/account_info``
    service (``NANO_ACCOUNT_INFO_API``, default https://pursekeeper.dev).
    """

    def __init__(self, rpc_url: Optional[str] = None, api_base: str = "https://pursekeeper.dev",
                 session: Optional[requests.Session] = None, timeout: float = 15.0):
        self.rpc_url = rpc_url
        self.api_base = api_base.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None, session=None) -> "AccountInfoSource":
        env = os.environ if env is None else env
        return cls(env.get("NANO_RPC") or None,
                   env.get("NANO_ACCOUNT_INFO_API", "https://pursekeeper.dev"), session=session)

    def __call__(self, account: str) -> AccountState:
        if self.rpc_url:
            r = self.session.post(self.rpc_url, json={"action": "account_info", "account": account,
                                                      "representative": "true", "include_confirmed": "true"},
                                  timeout=self.timeout, allow_redirects=False)
            j = r.json()
            if "error" in j:
                raise NanoX402Error(f"account_info: {j['error']}")
            frontier = j.get("confirmed_frontier") or j["frontier"]
            if j.get("confirmed_frontier") and j["confirmed_frontier"] != j["frontier"]:
                raise NanoX402Error("account has an unconfirmed block; try again once it is confirmed")
            bal = j.get("confirmed_balance") or j["balance"]
            return AccountState(frontier, int(bal), j["representative"])
        r = self.session.get(f"{self.api_base}/v1/account_info", params={"account": account},
                             timeout=self.timeout, allow_redirects=False)
        if r.status_code != 200:
            raise NanoX402Error(f"account_info HTTP {r.status_code}")
        j = r.json()
        if not j.get("found") or not j.get("open"):
            raise NanoX402Error("paying account has no blocks yet: receive (open) it first")
        if j.get("confirmed_frontier") and j["confirmed_frontier"] != j["frontier"]:
            raise NanoX402Error("account has an unconfirmed block; try again once it is confirmed")
        bal = j.get("confirmed_balance_raw") or j["balance_raw"]
        return AccountState(j.get("confirmed_frontier") or j["frontier"], int(bal), j["representative"])


# --------------------------------------------------------------------------- proof of work


def work_valid(work: str, root: str, threshold: str) -> bool:
    try:
        return bool(_ext.work_validate(int(work, 16), bytes.fromhex(root), int(threshold, 16)))
    except Exception:
        return False


class WorkProvider:
    """Proof of work for ``root`` at ``threshold``: a configured RPC-style work server
    (``NANO_WORK_URL``, ``{"action":"work_generate"}``) if set, else local CPU via nanopy."""

    def __init__(self, work_url: Optional[str] = None, session: Optional[requests.Session] = None,
                 local: bool = True, timeout: float = 60.0):
        self.work_url = work_url
        self.session = session or requests.Session()
        self.local = local
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None, session=None) -> "WorkProvider":
        env = os.environ if env is None else env
        return cls(env.get("NANO_WORK_URL") or None, session=session,
                   local=env.get("NANO_WORK_LOCAL", "1") != "0")

    def __call__(self, root: str, threshold: str) -> str:
        if self.work_url:
            try:
                # node-RPC shape; pursekeeper's POST /v1/work reads the same "hash" key
                r = self.session.post(self.work_url, json={"action": "work_generate", "hash": root,
                                                           "difficulty": threshold},
                                      timeout=self.timeout, allow_redirects=False)
                w = r.json().get("work")
                if isinstance(w, str) and work_valid(w, root, threshold):
                    return w.lower()
            except Exception:
                pass
            if not self.local:
                raise NanoX402Error("work server did not return valid work")
        if not self.local:
            raise NanoX402Error("work is required by the seller and no work source is configured")
        w = _ext.work_generate(bytes.fromhex(root), int(threshold, 16), os.urandom(128))
        return f"{w:016x}"
