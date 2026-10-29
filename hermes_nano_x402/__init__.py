"""Hermes Agent plugin for x402 ``exact`` payments on ``nano:mainnet``."""

__version__ = "0.1.0"

from .client import NanoX402Client, PayResult  # noqa: E402
from .errors import MalformedQuote, NanoX402Error, PaymentRefused, SignerUnavailable  # noqa: E402
from .guard import PaymentGuard, Policy  # noqa: E402
from .signer import NanopySigner  # noqa: E402


def register(ctx):
    from .plugin import register as _register

    _register(ctx)


__all__ = ["NanoX402Client", "PayResult", "Policy", "PaymentGuard", "NanopySigner", "NanoX402Error",
           "PaymentRefused", "MalformedQuote", "SignerUnavailable", "register", "__version__"]
