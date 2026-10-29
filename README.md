# hermes-nano-x402

A [Hermes Agent](https://pypi.org/project/hermes-agent/) plugin that lets the agent pay for HTTP
resources with **x402 `exact` on `nano:mainnet`** (XNO). A payment guard runs before anything is signed.

It adds one tool, `nano_x402_pay`, in the toolset `nano-x402`. It is a pip entry-point plugin
(`hermes_agent.plugins` → `nano-x402`) that registers through `ctx.register_tool`.

* Pinned: `hermes-agent==0.19.0`, `nanopy==28.0.1.post26` (block building, blake2b, ed25519, CPU work),
  `x402-nano-exact==0.1.2` (scheme constants and address validation), `requests==2.33.0`. Python 3.11–3.13.
* Wire format: reads the base64 `PAYMENT-REQUIRED` header (or the body's `x402` field). It answers with
  `PAYMENT-SIGNATURE` = base64 `{x402Version: 2, resource, accepted, payload: {block}}` and decodes
  `PAYMENT-RESPONSE`.
* Proof of work: when `extra.workOptional` is not `true` (false or absent), work for the block's
  `previous` at `extra.workThreshold` is attached. The threshold is never easier than the send threshold.
  Work comes from `NANO_WORK_URL` (node-RPC `work_generate` shape) if set, else is computed locally.
  When `workOptional` is `true`, the placeholder `0000000000000000` is sent.

## Install (one command, no install scripts)

```sh
pip install --only-binary :all: "hermes-nano-x402[test]==0.1.0"
```

`--only-binary :all:` installs wheels only, so no package build or install script runs. Every
dependency ships a wheel. With uv, the equivalent is `uv pip install --no-build "hermes-nano-x402[test]==0.1.0"`.

To use it in your own Hermes setup: `hermes plugins enable nano-x402` (entry-point plugins are opt-in).

## Configure (environment only; nothing secret is in the package)

| variable | meaning |
|---|---|
| `NANO_SEED` (+ `NANO_INDEX`, default 0) or `NANO_PRIVATE_KEY` | the paying account. Read lazily, never logged, printed or returned |
| `NANO_X402_ALLOWED_PAYTO` | comma list of `payTo` addresses that may be paid. **Empty = every payment refused** |
| `NANO_X402_MAX_PER_CALL_XNO` | per-call cap, default `0.001` |
| `NANO_X402_MAX_TOTAL_XNO` | cumulative cap for this process, default `0.01` |
| `NANO_X402_ALLOWED_ORIGINS` | optional comma list of origins that may be paid |
| `NANO_RPC` / `NANO_ACCOUNT_INFO_API` | where the confirmed frontier and balance are read: a node RPC, else `https://pursekeeper.dev/v1/account_info` |
| `NANO_WORK_URL` | optional work server; local CPU otherwise (`NANO_WORK_LOCAL=0` disables local work) |

You can inject a signer instead of using the environment: build `NanoX402Client(policy, signer=...)` with
any object that has `.address` and `.sign_send(state, pay_to, amount_raw, work)`, then pass it to
`hermes_nano_x402.plugin.set_client(...)`.

A fresh account has to receive (open) before it can send. After funding the address, run
`hermes-nano-x402-receive` once. It publishes receive/open blocks only and never sends.

## Example: one paid call from the Hermes agent loop (Ӿ0.001 at pursekeeper.dev)

```sh
export NANO_SEED=...   # throwaway wallet, funded with ~Ӿ0.01 and opened
export NANO_X402_ALLOWED_PAYTO=nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue
hermes-nano-x402-demo https://pursekeeper.dev/v1/hash --method POST --body hello
```

`hermes-nano-x402-demo` (source: `hermes_nano_x402/agent_example.py`) runs Hermes' own
`run_agent.AIAgent` loop:

1. It uses a throwaway `HERMES_HOME` whose `config.yaml` enables `nano-x402`.
2. `enabled_toolsets=["nano-x402"]`, so Hermes discovers the plugin through its entry point and offers the tool.
3. The model's tool call runs through Hermes' tool executor, and the result goes back to the model.

By default the model is a scripted OpenAI-compatible stand-in on `127.0.0.1`, so no LLM key is
needed. Its first turn calls `nano_x402_pay` once, and its second turn reports the result. To let a
real model decide, pass `--model NAME --base-url URL` and set `OPENAI_API_KEY`. The demo prints the
status, amount, block hash and body. It exits 0 only when the paid request returned 2xx.

Also free to try: `hermes-nano-x402-demo "https://pursekeeper.dev/v1/fetch?url=https://example.com" --method GET`.

## The guard (all before signing)

1. scheme `exact` on `nano:mainnet`, asset `XNO`; anything else → `wrong_network`
2. `payTo` must be in `NANO_X402_ALLOWED_PAYTO`; an empty allowlist fails closed → `pay_to_not_allowed`
3. amount ≤ per-call cap → `over_per_call_cap`
4. spent + reserved + amount ≤ cumulative cap (in memory, per process; a signed block always counts) → `over_total_cap`
5. a request already reserved or signed (same `request_id`, or same method + URL + body) is never signed again → `duplicate_request`
6. redirects: an unsigned probe follows same-origin redirects only; a cross-origin redirect stops the call unpaid
   (`cross_origin_redirect`); the signed request goes to the URL that answered 402 with redirects off; a quote whose
   `resource.url` names another origin is refused
7. malformed quotes (wrong `x402Version`, bad base64/JSON, missing or non-integer amount, bad `payTo`, bad
   `maxTimeoutSeconds`, `extra` or `workThreshold`) → `malformed_quote`
8. freshness: if the quote is older than `maxTimeoutSeconds` (less a 2 s margin) when it is time to sign, it is thrown
   away and re-fetched, and the new quote goes through the guard again; a quote is never re-used
9. one send block per call, built from the **confirmed** frontier (an unconfirmed frontier is refused), with
   `balance = balance − amount` and `link = payTo`
10. no automatic paid retry: a non-2xx (or a network error) after the payment is returned to the agent with the block hash.
    A 402 "not confirmed" answer is returned, not re-presented

## Offline tests

```sh
python -m pytest -p no:cacheprovider --pyargs hermes_nano_x402.tests
```

The tests ship inside the package. They make no network connections (any non-loopback `connect`
fails the test) and use a mocked 402 seller. They cover every guard rule above, block correctness
(hash and signature), the PoW paths, the receive command, the plugin's registration, and full
`AIAgent` runs through the scripted model. They also check that the seed and key appear in no
tool output, log or captured stream.

## Licence

MIT
