"""Exact-total quotes: created by get_quote, consumed by purchase.

A quote locks the exact total (product + delivery + tax + the $2.99 Rosebud
service fee) that the customer approves. Quotes expire after 30 minutes and
are single-use: purchase marks the quote used, and a changed total needs a
fresh quote and fresh approval.

The store backend is chosen by ROSEBUD_STORE (memory | postgres).
Methods are async so the Postgres backend can do real I/O; the memory
backend just awaits trivially.
"""

import time
import uuid
from dataclasses import dataclass, field

from .storage import make_quote_backend

QUOTE_TTL_SECONDS = 30 * 60
SERVICE_FEE = 2.99


@dataclass
class Quote:
    quote_id: str
    arrangement_id: str
    florist: str
    product_name: str
    product_price: float
    delivery_fee: float
    tax: float
    recipient: dict
    delivery_date: str
    card_message: str
    sender_name: str
    created_at: float = field(default_factory=time.time)
    used: bool = False
    service_fee: float = SERVICE_FEE

    @property
    def total(self) -> float:
        return round(
            self.product_price + self.delivery_fee + self.tax + self.service_fee, 2
        )

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > QUOTE_TTL_SECONDS

    def line_items(self) -> list[dict]:
        return [
            {"label": self.product_name, "amount": round(self.product_price, 2)},
            {"label": "Delivery", "amount": round(self.delivery_fee, 2)},
            {"label": "Estimated tax", "amount": round(self.tax, 2)},
            {"label": "Rosebud service fee", "amount": round(self.service_fee, 2)},
        ]

    def to_dict(self) -> dict:
        return {
            "quote_id": self.quote_id,
            "arrangement_id": self.arrangement_id,
            "florist": self.florist,
            "product_name": self.product_name,
            "delivery_date": self.delivery_date,
            "card_message": self.card_message,
            "sender_name": self.sender_name,
            "recipient": self.recipient,
            "line_items": self.line_items(),
            "total": self.total,
            "expires_in_seconds": max(
                0, int(QUOTE_TTL_SECONDS - (time.time() - self.created_at))
            ),
            "approval_note": (
                "Present the florist, product, delivery address, delivery "
                f"date, and the exact total of ${self.total:.2f} (including "
                "the $2.99 Rosebud service fee as its own line item) to the "
                "user. Only call purchase() after explicit approval of this "
                "exact quote. If anything changes, get a fresh quote."
            ),
        }


def _quote_to_payload(quote: "Quote") -> dict:
    return {
        "quote_id": quote.quote_id,
        "arrangement_id": quote.arrangement_id,
        "florist": quote.florist,
        "product_name": quote.product_name,
        "product_price": quote.product_price,
        "delivery_fee": quote.delivery_fee,
        "tax": quote.tax,
        "service_fee": quote.service_fee,
        "recipient": quote.recipient,
        "delivery_date": quote.delivery_date,
        "card_message": quote.card_message,
        "sender_name": quote.sender_name,
        "created_at": quote.created_at,
        "expires_at": quote.created_at + QUOTE_TTL_SECONDS,
        "used": quote.used,
    }


def _payload_to_quote(payload: dict) -> "Quote":
    return Quote(
        quote_id=payload["quote_id"],
        arrangement_id=payload["arrangement_id"],
        florist=payload["florist"],
        product_name=payload["product_name"],
        product_price=payload["product_price"],
        delivery_fee=payload["delivery_fee"],
        tax=payload["tax"],
        recipient=payload["recipient"],
        delivery_date=payload["delivery_date"],
        card_message=payload["card_message"],
        sender_name=payload["sender_name"],
        created_at=payload["created_at"],
        used=payload.get("used", False),
        service_fee=payload.get("service_fee", SERVICE_FEE),
    )


class QuoteStore:
    """Async facade over the configured quote backend."""

    def __init__(self, backend=None) -> None:
        self._backend = backend or make_quote_backend()

    async def create(
        self,
        *,
        arrangement_id: str,
        florist: str,
        product_name: str,
        product_price: float,
        delivery_fee: float,
        tax: float,
        recipient: dict,
        delivery_date: str,
        card_message: str,
        sender_name: str,
    ) -> Quote:
        quote = Quote(
            quote_id="Q-" + uuid.uuid4().hex[:10].upper(),
            arrangement_id=arrangement_id,
            florist=florist,
            product_name=product_name,
            product_price=product_price,
            delivery_fee=delivery_fee,
            tax=tax,
            recipient=recipient,
            delivery_date=delivery_date,
            card_message=card_message,
            sender_name=sender_name,
        )
        await self._backend.create(_quote_to_payload(quote))
        return quote

    async def get(self, quote_id: str) -> Quote | None:
        payload = await self._backend.get(quote_id)
        if payload is None:
            return None
        quote = _payload_to_quote(payload)
        if quote.expired:
            return None
        return quote

    async def claim(self, quote_id: str) -> bool:
        """Atomically claim a quote for checkout. Returns True only if
        this caller got it: unexpired and not already claimed. This is
        what makes single-use hold across workers."""
        payload = await self._backend.get(quote_id)
        if payload is None:
            return False
        if _payload_to_quote(payload).expired:
            return False
        return await self._backend.claim(quote_id)

    async def release(self, quote_id: str) -> None:
        """Release a claim (used when checkout fails before charging)."""
        await self._backend.release(quote_id)
