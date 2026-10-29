"""Operator command: pocket (receive / open) what was sent to the paying account.

    hermes-x402-nano-receive            # uses NANO_SEED / NANO_PRIVATE_KEY from the environment

A fresh Nano account cannot send until its first (open) block is confirmed, so fund the address,
run this once, then run the agent. This command only publishes receive/open blocks; it never sends.
It is not exposed to the agent as a tool. Uses pursekeeper.dev's free no-node endpoints by default
(``NANO_ACCOUNT_INFO_API``), or a node when ``NANO_RPC`` is set. Prints only the address and hashes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests

from .guard import raw_to_xno
from .signer import AccountState, NanopySigner, WorkProvider


def _get(session, base, path, **params):
    r = session.get(f"{base}{path}", params=params, timeout=20, allow_redirects=False)
    r.raise_for_status()
    return r.json()


def receive_all(signer, base="https://pursekeeper.dev", session=None, work=None, representative=None,
                limit=10, wait=60.0, log=print):
    session = session or requests.Session()
    base = base.rstrip("/")
    work = work or WorkProvider(os.environ.get("NANO_WORK_URL") or f"{base}/v1/work", session=session)
    addr = signer.address
    pending = _get(session, base, "/v1/receivable", account=addr, min_raw=1)
    blocks = pending.get("blocks") or []
    if not blocks:
        log(f"{addr}: nothing receivable")
        return []
    done = []
    for blk in blocks[:limit]:
        info = _get(session, base, "/v1/account_info", account=addr)
        if info.get("found"):
            if info.get("confirmed_frontier") and info["confirmed_frontier"] != info["frontier"]:
                raise RuntimeError("account has an unconfirmed block; run again in a moment")
            state = AccountState(info["frontier"], int(info["balance_raw"]), info["representative"])
            root, subtype, rep = state.frontier, "receive", representative or state.representative
        else:
            state, root, subtype = None, signer.public_key, "open"
            rep = representative
            if not rep:
                sender = _get(session, base, "/v1/account_info", account=blk["from"])
                rep = sender.get("representative")
            if not rep:
                raise RuntimeError("no representative: set NANO_REPRESENTATIVE")
        w = work(root, "fffffff800000000")
        sb = signer.sign_receive(state, blk["hash"], int(blk["amount_raw"]), rep, w)
        r = session.post(f"{base}/v1/process", json={"block": sb.block, "subtype": subtype},
                         timeout=30, allow_redirects=False)
        j = r.json() if r.content else {}
        if r.status_code != 200 or not j.get("ok", "hash" in j):
            raise RuntimeError(f"process refused the {subtype} block: HTTP {r.status_code} {str(j)[:200]}")
        log(f"{subtype}: {raw_to_xno(int(blk['amount_raw']))} XNO from {blk['from']} -> block {sb.hash}")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            info = _get(session, base, "/v1/account_info", account=addr)
            if (info.get("confirmed_frontier") or "").upper() == sb.hash:
                break
            time.sleep(1.0)
        else:
            raise RuntimeError(f"block {sb.hash} not confirmed after {wait:.0f}s; run again later")
        done.append(sb.hash)
    info = _get(session, base, "/v1/account_info", account=addr)
    log(f"{addr}: balance {raw_to_xno(int(info.get('confirmed_balance_raw') or info.get('balance_raw') or 0))} XNO")
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("NANO_ACCOUNT_INFO_API", "https://pursekeeper.dev"))
    ap.add_argument("--representative", default=os.environ.get("NANO_REPRESENTATIVE"))
    ap.add_argument("--address-only", action="store_true", help="print the account address and exit")
    a = ap.parse_args(argv)
    try:
        signer = NanopySigner.from_env()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.address_only:
        print(signer.address)
        return 0
    try:
        receive_all(signer, a.api, representative=a.representative)
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}"[:500], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
