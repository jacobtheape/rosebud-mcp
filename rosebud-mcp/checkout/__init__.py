"""Rosebud browser-checkout backend.

Checkout modes (env ROSEBUD_CHECKOUT_MODE):
  fake  - simulated florist adapters; no network, no real orders (default)
  live  - headless-browser checkout at real florist sites (requires
          playwright + `playwright install chromium`)

The default is fake so the server can never place a real order by accident.
"""

from .adapters import ADAPTERS, FloristAdapter, get_adapter
from .payments import (
    PaymentLedger,
    get_business_payment,
    ledger,
    refund_customer_payment,
    verify_customer_payment,
)
from .quotes import Quote, QuoteStore, QUOTE_TTL_SECONDS, SERVICE_FEE

__all__ = [
    "ADAPTERS",
    "FloristAdapter",
    "get_adapter",
    "PaymentLedger",
    "Quote",
    "QuoteStore",
    "QUOTE_TTL_SECONDS",
    "SERVICE_FEE",
    "get_business_payment",
    "ledger",
    "refund_customer_payment",
    "verify_customer_payment",
]
