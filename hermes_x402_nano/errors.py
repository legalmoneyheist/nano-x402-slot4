"""Exceptions raised by the guard and the client. Messages never contain key material."""


class NanoX402Error(Exception):
    """Base class for every refusal or failure in this package."""

    code = "error"


class PaymentRefused(NanoX402Error):
    """The guard refused the payment. Nothing was signed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MalformedQuote(PaymentRefused):
    """The 402 quote could not be parsed or broke the scheme's shape. Nothing was signed."""

    def __init__(self, message: str):
        super().__init__("malformed_quote", message)


class SignerUnavailable(NanoX402Error):
    code = "signer_unavailable"
