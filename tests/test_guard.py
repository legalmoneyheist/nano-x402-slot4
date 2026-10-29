"""Every guard rule from the bounty page, offline against a mocked 402."""

import base64
import json

import nanopy
import pytest

from conftest import BALANCE, FRONTIER, ONE_MILLI_RAW, OTHER, PAY_TO, REP, b64, make_quote, response

URL = "https://seller.test/v1/hash"


def decode_sig(headers):
    return json.loads(base64.b64decode(headers["PAYMENT-SIGNATURE"]))


# --------------------------------------------------------------- happy path / one send per call

def test_pays_once_and_builds_a_correct_block(seller, signer, make_client):
    seller.x402(URL)
    c = make_client()
    r = c.pay(URL)
    assert r.ok and r.paid and r.status == 200
    assert signer.count == 1 and len(seller.paid_calls()) == 1
    sent = decode_sig(seller.paid_calls()[0][2])
    assert sent["x402Version"] == 2
    assert sent["accepted"]["network"] == "nano:mainnet" and sent["accepted"]["scheme"] == "exact"
    assert sent["resource"]["url"] == URL
    blk = sent["payload"]["block"]
    assert blk["type"] == "state"
    assert blk["previous"] == FRONTIER
    assert int(blk["balance"]) == BALANCE - ONE_MILLI_RAW
    assert blk["link"] == nanopy.Account(addr=PAY_TO).pk.upper()
    assert blk["representative"] == REP
    assert blk["work"] == "0000000000000000"  # workOptional true
    assert r.block_hash == signer.blocks[0].hash
    assert r.settlement == {"success": True, "network": "nano:mainnet"}
    # signature verifies against the block hash and the paying account's public key
    pk = bytes.fromhex(nanopy.Account(addr=blk["account"]).pk)
    import hashlib
    h = hashlib.blake2b(digest_size=32)
    for part in (b"\x00" * 31 + b"\x06", pk, bytes.fromhex(blk["previous"]),
                 bytes.fromhex(nanopy.Account(addr=blk["representative"]).pk),
                 int(blk["balance"]).to_bytes(16, "big"), bytes.fromhex(blk["link"])):
        h.update(part)
    assert h.hexdigest().upper() == r.block_hash
    assert nanopy.ext.verify_signature(bytes.fromhex(blk["signature"]), pk, bytes.fromhex(r.block_hash))
    assert blk["account"] == signer.address


def test_one_send_block_per_call(seller, signer, make_client):
    seller.x402(URL)
    c = make_client()
    for i in range(3):
        r = c.pay(URL, request_id=f"call-{i}")
        assert r.paid
    assert signer.count == 3 and len(seller.paid_calls()) == 3
    assert c.guard.spent_raw == 3 * ONE_MILLI_RAW


# --------------------------------------------------------------- refusals before signing

def test_wrong_network_refused(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, network="nano:testnet")
    seller.x402(URL)
    r = make_client().pay(URL)
    assert r.refused == "wrong_network" and not r.paid
    assert signer.count == 0 and not seller.paid_calls()


def test_wrong_scheme_refused(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, scheme="upto")
    seller.x402(URL)
    r = make_client().pay(URL)
    assert r.refused == "wrong_network" and signer.count == 0


def test_evm_network_refused(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, network="eip155:8453")
    seller.x402(URL)
    assert make_client().pay(URL).refused == "wrong_network"
    assert signer.count == 0


def test_pay_to_not_in_allowlist_refused(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, pay_to=OTHER)
    seller.x402(URL)
    r = make_client().pay(URL)
    assert r.refused == "pay_to_not_allowed" and signer.count == 0 and not seller.paid_calls()


def test_empty_allowlist_fails_closed(seller, signer, make_client):
    seller.x402(URL)
    r = make_client(allowed=()).pay(URL)
    assert r.refused == "pay_to_not_allowed" and signer.count == 0


def test_amount_above_per_call_cap_refused(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, amount=str(ONE_MILLI_RAW + 1))
    seller.x402(URL)
    r = make_client(per_call="0.001").pay(URL)
    assert r.refused == "over_per_call_cap" and signer.count == 0


def test_cumulative_cap_refused_once_reached(seller, signer, make_client):
    seller.x402(URL)
    c = make_client(per_call="0.001", total="0.002")
    assert c.pay(URL, request_id="a").paid
    assert c.pay(URL, request_id="b").paid
    r = c.pay(URL, request_id="c")
    assert r.refused == "over_total_cap" and not r.paid
    assert signer.count == 2 and len(seller.paid_calls()) == 2


def test_cumulative_cap_counts_failed_paid_calls(seller, signer, make_client):
    """A block that was signed counts as spent even if the seller then fails."""
    seller.paid_status = 500
    seller.x402(URL)
    c = make_client(per_call="0.001", total="0.001")
    r1 = c.pay(URL, request_id="a")
    assert r1.paid and not r1.ok
    assert c.pay(URL, request_id="b").refused == "over_total_cap"
    assert signer.count == 1


def test_second_invocation_same_request_never_signs_again(seller, signer, make_client):
    seller.x402(URL)
    c = make_client()
    assert c.pay(URL).paid
    r = c.pay(URL)
    assert r.refused == "duplicate_request" and not r.paid
    assert c.pay(URL, method="GET").refused == "duplicate_request"
    assert signer.count == 1 and len(seller.paid_calls()) == 1


def test_same_request_id_never_signs_again_even_after_failure(seller, signer, make_client):
    seller.paid_status = 502
    seller.x402(URL)
    c = make_client()
    assert c.pay(URL, request_id="job-1").paid
    seller.paid_status = 200
    assert c.pay(URL, request_id="job-1").refused == "duplicate_request"
    assert signer.count == 1


def test_concurrent_same_request_signs_once(seller, signer, make_client):
    import threading
    seller.x402(URL)
    c = make_client()
    results = []
    ts = [threading.Thread(target=lambda: results.append(c.pay(URL))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(r.paid for r in results) == 1 and signer.count == 1


def test_cross_origin_redirect_never_paid(seller, signer, make_client):
    seller.routes[URL] = lambda m, h, b: response(302, "", {"Location": "https://evil.test/pay"}, URL)
    seller.x402("https://evil.test/pay")
    r = make_client().pay(URL)
    assert r.refused == "cross_origin_redirect" and not r.paid
    assert signer.count == 0
    assert not any(u.startswith("https://evil.test") for _, u, _ in seller.calls)


def test_scheme_downgrade_redirect_is_cross_origin(seller, signer, make_client):
    seller.routes[URL] = lambda m, h, b: response(301, "", {"Location": "http://seller.test/v1/hash"}, URL)
    seller.x402("http://seller.test/v1/hash")
    assert make_client().pay(URL).refused == "cross_origin_redirect"
    assert signer.count == 0


def test_same_origin_redirect_followed_and_paid_at_402_url(seller, signer, make_client):
    final = "https://seller.test/v2/hash"
    seller.routes[URL] = lambda m, h, b: response(307, "", {"Location": "/v2/hash"}, URL)
    seller.x402(final)
    r = make_client().pay(URL)
    assert r.paid and r.url == final
    assert seller.paid_calls()[0][1] == final and signer.count == 1


def test_redirect_after_payment_is_not_followed(seller, signer, make_client):
    def h(m, headers, b):
        if "PAYMENT-SIGNATURE" in headers:
            return response(302, "", {"Location": "https://evil.test/x"}, URL)
        return response(402, "", {"PAYMENT-REQUIRED": b64(make_quote(URL))}, URL)
    seller.routes[URL] = h
    r = make_client().pay(URL)
    assert r.paid and r.status == 302 and not r.ok
    assert not any(u.startswith("https://evil.test") for _, u, _ in seller.calls)
    assert signer.count == 1


def test_resource_on_other_origin_refused(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote("https://evil.test/v1/hash")
    seller.x402(URL)
    assert make_client().pay(URL).refused == "resource_origin_mismatch"
    assert signer.count == 0


@pytest.mark.parametrize("mutate", [
    lambda q: q.update(x402Version=1),
    lambda q: q.update(accepts=[]),
    lambda q: q.update(accepts="nope"),
    lambda q: q["accepts"][0].update(amount="-5"),
    lambda q: q["accepts"][0].update(amount="0"),
    lambda q: q["accepts"][0].update(amount="1e27"),
    lambda q: q["accepts"][0].update(amount=10**27),
    lambda q: q["accepts"][0].update(amount=str(2**128)),
    lambda q: q["accepts"][0].update(asset="USDC"),
    lambda q: q["accepts"][0].update(payTo="nano_1notanaddress"),
    lambda q: q["accepts"][0].pop("payTo"),
    lambda q: q["accepts"][0].update(maxTimeoutSeconds="60"),
    lambda q: q["accepts"][0].update(maxTimeoutSeconds=0),
    lambda q: q["accepts"][0].update(extra="x"),
    lambda q: q["accepts"][0]["extra"].update(workOptional="yes"),
    lambda q: q["accepts"][0]["extra"].update(workThreshold="zz"),
])
def test_malformed_quote_refused(seller, signer, make_client, mutate):
    q = make_quote(URL)
    mutate(q)
    seller.quote_factory = lambda url: q
    seller.x402(URL)
    r = make_client().pay(URL)
    assert r.refused == "malformed_quote", r.message
    assert signer.count == 0 and not seller.paid_calls()


@pytest.mark.parametrize("header", ["not base64 !!", base64.b64encode(b"{not json").decode(),
                                    base64.b64encode(b"[1,2]").decode()])
def test_undecodable_header_refused(seller, signer, make_client, header):
    seller.routes[URL] = lambda m, h, b: response(402, "", {"PAYMENT-REQUIRED": header}, URL)
    assert make_client().pay(URL).refused == "malformed_quote"
    assert signer.count == 0


def test_402_without_quote_refused(seller, signer, make_client):
    seller.routes[URL] = lambda m, h, b: response(402, "pay me", {}, URL)
    assert make_client().pay(URL).refused == "malformed_quote"
    assert signer.count == 0


def test_quote_in_body_x402_field_is_accepted(seller, signer, make_client):
    def h(m, headers, b):
        if "PAYMENT-SIGNATURE" in headers:
            return response(200, "ok", {}, URL)
        return response(402, json.dumps({"x402": make_quote(URL)}), {}, URL)
    seller.routes[URL] = h
    assert make_client().pay(URL).paid and signer.count == 1


# --------------------------------------------------------------- freshness

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_stale_quote_is_refetched_never_reused(seller, signer, make_client):
    clock = Clock()
    n = {"quotes": 0}

    def factory(url):
        n["quotes"] += 1
        q = make_quote(url, work_optional=False, session=f"s{n['quotes']}")
        return q
    seller.quote_factory = factory
    seller.x402(URL)

    def slow_work(root, thr):
        clock.t += 120  # work took longer than maxTimeoutSeconds (60)
        return "1" * 16
    c = make_client(work=slow_work, clock=clock)
    r = c.pay(URL)
    assert r.paid and signer.count == 1
    assert n["quotes"] == 2
    sent = decode_sig(seller.paid_calls()[0][2])
    assert sent["accepted"]["extra"]["session"] == "s2"  # the fresh quote, not the stale one


def test_refetched_quote_goes_through_guard_again(seller, signer, make_client):
    clock = Clock()
    n = {"quotes": 0}

    def factory(url):
        n["quotes"] += 1
        amt = ONE_MILLI_RAW if n["quotes"] == 1 else ONE_MILLI_RAW * 5
        return make_quote(url, amount=str(amt), work_optional=False)
    seller.quote_factory = factory
    seller.x402(URL)

    def slow_work(root, thr):
        clock.t += 120
        return "1" * 16
    r = make_client(work=slow_work, clock=clock).pay(URL)
    assert r.refused == "over_per_call_cap" and signer.count == 0


def test_refetched_quote_also_stale_is_refused(seller, signer, make_client):
    clock = Clock()
    thresholds = []

    def work(root, thr):
        thresholds.append(thr)
        clock.t += 120
        return "1" * 16
    # a different threshold forces fresh work, so the second quote also goes stale
    n = {"q": 0}

    def factory(url):
        n["q"] += 1
        return make_quote(url, work_optional=False, workThreshold="fffffff8" + f"{n['q']:08x}")
    seller.quote_factory = factory
    seller.x402(URL)
    r = make_client(work=work, clock=clock).pay(URL)
    assert r.refused == "quote_expired" and signer.count == 0


def test_fresh_quote_used_once(seller, signer, make_client):
    n = {"quotes": 0}

    def factory(url):
        n["quotes"] += 1
        return make_quote(url)
    seller.quote_factory = factory
    seller.x402(URL)
    assert make_client().pay(URL).paid
    assert n["quotes"] == 1


# --------------------------------------------------------------- PoW

def test_work_attached_when_work_not_optional(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, work_optional=False)
    seller.x402(URL)
    asked = []

    def work(root, thr):
        asked.append((root, thr))
        return "abcdef0123456789"
    r = make_client(work=work).pay(URL)
    assert r.paid
    assert asked == [(FRONTIER, "fffffff800000000")]
    assert decode_sig(seller.paid_calls()[0][2])["payload"]["block"]["work"] == "abcdef0123456789"


def test_work_attached_when_work_optional_absent(seller, signer, make_client):
    q = make_quote(URL)
    del q["accepts"][0]["extra"]["workOptional"]
    seller.quote_factory = lambda url: q
    seller.x402(URL)
    asked = []
    r = make_client(work=lambda root, thr: asked.append(thr) or "abcdef0123456789").pay(URL)
    assert r.paid and asked


def test_easier_threshold_clamped_to_send_threshold(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, work_optional=False, workThreshold="0000000000000001")
    seller.x402(URL)
    asked = []
    make_client(work=lambda root, thr: asked.append(thr) or "abcdef0123456789").pay(URL)
    assert asked == ["fffffff800000000"]


def test_work_failure_signs_nothing(seller, signer, make_client):
    from hermes_nano_x402.errors import NanoX402Error
    seller.quote_factory = lambda url: make_quote(url, work_optional=False)
    seller.x402(URL)

    def bad(root, thr):
        raise NanoX402Error("no work")
    c = make_client(work=bad)
    r = c.pay(URL)
    assert not r.paid and signer.count == 0
    assert c.guard.spent_raw == 0  # reservation released


def test_local_work_provider_produces_valid_work():
    from hermes_nano_x402.signer import WorkProvider, work_valid
    root = "B" * 64
    easy = "0000000000000001"
    w = WorkProvider(local=True)(root, easy)
    assert len(w) == 16 and work_valid(w, root, easy)


# --------------------------------------------------------------- no automatic paid retry

@pytest.mark.parametrize("status", [400, 402, 409, 500, 503])
def test_no_automatic_paid_retry_after_non_2xx(seller, signer, make_client, status):
    seller.paid_status = status
    seller.x402(URL)
    r = make_client().pay(URL)
    assert r.paid and not r.ok and r.status == status
    assert signer.count == 1 and len(seller.paid_calls()) == 1
    assert "not retried" in r.message


def test_network_error_after_payment_not_retried(seller, signer, make_client):
    import requests

    def h(m, headers, b):
        if "PAYMENT-SIGNATURE" in headers:
            raise requests.ConnectionError("reset")
        return response(402, "", {"PAYMENT-REQUIRED": b64(make_quote(URL))}, URL)
    seller.routes[URL] = h
    r = make_client().pay(URL)
    assert r.paid and r.status is None and r.block_hash
    assert signer.count == 1 and len(seller.paid_calls()) == 1


# --------------------------------------------------------------- non-402 and other

def test_non_402_is_returned_without_paying(seller, signer, make_client):
    seller.routes[URL] = lambda m, h, b: response(200, "free", {}, URL)
    r = make_client().pay(URL)
    assert r.ok and not r.paid and r.body == "free" and signer.count == 0


def test_insufficient_balance_refused(seller, signer, make_client):
    seller.quote_factory = lambda url: make_quote(url, amount=str(BALANCE + 1))
    seller.x402(URL)
    r = make_client(per_call="1", total="1").pay(URL)
    assert r.refused == "insufficient_balance" and signer.count == 0


def test_non_http_url_refused(seller, signer, make_client):
    assert make_client().pay("file:///etc/passwd").refused == "bad_url"
    assert signer.count == 0


def test_caller_cannot_inject_payment_header(seller, signer, make_client):
    seller.x402(URL)
    c = make_client()
    r = c.pay(URL, headers={"PAYMENT-SIGNATURE": "forged"}, request_id="x")
    assert r.paid
    assert seller.calls[0][2].get("PAYMENT-SIGNATURE") is None
