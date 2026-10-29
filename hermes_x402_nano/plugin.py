"""Hermes Agent plugin: registers the ``nano_x402_pay`` tool in the ``x402-nano`` toolset."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from .client import NanoX402Client
from .errors import NanoX402Error

logger = logging.getLogger(__name__)

TOOL_NAME = "nano_x402_pay"
TOOLSET = "x402-nano"

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Fetch an HTTP resource that may require an x402 payment in Nano (XNO, network nano:mainnet, "
        "scheme exact). If the server answers 402, the operator's guard checks the quote (network, payTo "
        "allowlist, per-call and total caps, freshness) and, if allowed, signs exactly one Nano send block "
        "and re-sends the request. Returns status, body, block hash and amount paid. Never retries a paid "
        "request: if a paid call fails, report it instead of calling again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL of the resource."},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                       "description": "HTTP method (default GET)."},
            "body": {"type": "string", "description": "Optional request body (sent as-is)."},
            "content_type": {"type": "string", "description": "Content-Type for the body (default application/json)."},
            "request_id": {"type": "string",
                           "description": "Optional idempotency key; a key that was already paid is never paid again."},
        },
        "required": ["url"],
    },
}

_client: Optional[NanoX402Client] = None
_client_lock = threading.Lock()


def get_client() -> NanoX402Client:
    """One client (and so one spend ledger) per process."""
    global _client
    with _client_lock:
        if _client is None:
            _client = NanoX402Client.from_env()
        return _client


def set_client(client: Optional[NanoX402Client]) -> None:
    """Inject a client (custom signer, policy, session). ``None`` resets to env config."""
    global _client
    with _client_lock:
        _client = client


def handle_pay(args: dict, **kwargs) -> str:
    args = args or {}
    url = args.get("url")
    if not isinstance(url, str) or not url:
        return json.dumps({"ok": False, "paid": False, "refused": "bad_arguments", "message": "url is required"})
    method = args.get("method") or "GET"
    body = args.get("body")
    headers = {}
    if body is not None:
        headers["Content-Type"] = args.get("content_type") or "application/json"
    try:
        result = get_client().pay(url, method=method, body=body, headers=headers,
                                  request_id=args.get("request_id"))
        out = result.to_dict()
    except NanoX402Error as e:
        out = {"ok": False, "paid": False, "refused": getattr(e, "code", "error"), "message": str(e)}
    except Exception as e:  # never let a stack trace (or anything in it) reach the model
        logger.warning("nano_x402_pay failed: %s", type(e).__name__)
        out = {"ok": False, "paid": False, "refused": "error", "message": type(e).__name__}
    return json.dumps(out)


def _check() -> bool:
    return bool(os.environ.get("NANO_X402_ALLOWED_PAYTO"))


def register(ctx) -> None:
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=handle_pay,
        check_fn=_check,
        requires_env=["NANO_X402_ALLOWED_PAYTO", "NANO_SEED"],
        description=SCHEMA["description"],
        emoji="Ӿ",
    )
