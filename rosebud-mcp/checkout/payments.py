"""Customer payments under the merchant-of-record model (Option B).

Money flow:
  1. The customer approves the exact quote (all-in total: product +
     delivery + tax + the $2.99 Rosebud service fee).
  2. The Muse Platform charges the customer the full total and credits
     Rosebud's Stripe account. It hands the agent a platform_payment_ref
     (a Stripe payment intent id) to pass to purchase().
  3. purchase() verifies the ref: the payment must exist, be captured,
     and equal the quote's exact total. Refs are single-use.
  4. Rosebud pays the florist the MERCHANT total (product + delivery +
     tax, WITHOUT the $2.99 fee) using Rosebud's own business payment
     method, which lives server-side and is never customer data.
  5. The $2.99 fee stays with Rosebud as margin. It is never charged at
     the florist, and raw customer card data never touches this server.

Modes (env ROSEBUD_CHECKOUT_MODE):
  fake  simulated platform payments (refs like "pay_fake_...") and a
        dummy business card. No network, no real money. Default.
  live  refs are verified against the Stripe API (STRIPE_SECRET_KEY),
        refunds go through Stripe, and the florist is paid with the real
        business card from ROSEBUD_BUSINESS_CARD_* env vars.

The ledger backend is chosen by ROSEBUD_STORE (memory | postgres).
Methods are async so the Postgres backend can do real I/O.
"""

import os
import uuid

from .adapters import CheckoutError, Payment
from .quotes import SERVICE_FEE
from .storage import _cents, make_ledger_backend


def _mode() -> str:
    return os.environ.get("ROSEBUD_CHECKOUT_MODE", "fake").strip().lower()


class PaymentLedger:
    """Async facade over the configured ledger backend. Record of customer
    payments, florist payouts, and refunds."""

    def __init__(self, backend=None) -> None:
        self._backend = backend or make_ledger_backend()

    async def record_capture(self, payment_ref: str, amount: float) -> dict:
        record = await self._backend.record_capture(
            payment_ref, _cents(amount)
        )
        if record is None:
            raise CheckoutError(
                "that platform_payment_ref was already used. Payment refs "
                "are single-use; a reused ref is refused to prevent "
                "double-spending."
            )
        return record

    async def link_order(
        self, payment_ref: str, order_id: str, florist_charged: float
    ) -> dict:
        record = await self._backend.link_order(
            payment_ref, order_id, _cents(florist_charged)
        )
        if record is None:
            raise CheckoutError(f"unknown payment_ref: {payment_ref}")
        return record

    async def record_refund(self, payment_ref: str, refund_id: str) -> dict:
        outcome, record = await self._backend.record_refund(
            payment_ref, refund_id
        )
        if outcome == "already_refunded":
            raise CheckoutError("that payment was already refunded")
        if outcome == "unknown" or record is None:
            raise CheckoutError(f"unknown payment_ref: {payment_ref}")
        return record

    async def payment_for_order(self, order_id: str) -> dict | None:
        return await self._backend.payment_for_order(order_id)


ledger = PaymentLedger()


async def verify_customer_payment(
    payment_ref: str, expected_total: float
) -> dict:
    """Confirm the platform collected exactly expected_total from the
    customer into Rosebud's account. Returns the ledger record.

    Raises CheckoutError if the ref is missing, already used, or the
    amount does not match the approved quote exactly.
    """
    ref = (payment_ref or "").strip()
    if not ref:
        raise CheckoutError(
            "platform_payment_ref is required: the customer pays Rosebud "
            "the exact approved total before purchase runs."
        )
    if _mode() == "live":
        return await _verify_via_stripe(ref, expected_total)
    # Fake mode: accept simulated platform refs, enforce single-use and
    # exact-amount match (the amount check happens in purchase() against
    # the agent-reported payment_amount).
    if not ref.startswith("pay_fake_"):
        raise CheckoutError(
            f"unknown platform_payment_ref: {ref!r}. In fake mode, refs "
            "look like 'pay_fake_...'."
        )
    return await ledger.record_capture(ref, expected_total)


async def _verify_via_stripe(payment_ref: str, expected_total: float) -> dict:
    try:
        import stripe
    except ImportError:
        raise CheckoutError(
            "the stripe package is required in live mode (pip install stripe)"
        )
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise CheckoutError("STRIPE_SECRET_KEY is not set (live mode)")
    stripe.api_key = secret
    try:
        intent = stripe.PaymentIntent.retrieve(payment_ref)
    except Exception as e:
        raise CheckoutError(f"could not verify payment {payment_ref!r}: {e}")
    # Retrieving with Rosebud's secret key proves the intent belongs to
    # Rosebud's Stripe account.
    if intent.status != "succeeded":
        raise CheckoutError(
            f"payment {payment_ref!r} is not captured (status: {intent.status}); "
            "refusing purchase."
        )
    collected = intent.amount_received / 100
    if abs(collected - expected_total) > 0.01:
        raise CheckoutError(
            f"payment {payment_ref!r} collected ${collected:.2f} but the "
            f"approved total is ${expected_total:.2f}; refusing purchase."
        )
    return await ledger.record_capture(payment_ref, collected)


async def refund_customer_payment(
    payment_ref: str, reason: str = "order cancelled"
) -> dict:
    """Refund a captured customer payment. Returns refund details."""
    if _mode() == "live":
        return await _refund_via_stripe(payment_ref, reason)
    refund_id = "re_fake_" + uuid.uuid4().hex[:12]
    record = await ledger.record_refund(payment_ref, refund_id)
    return {
        "refund_id": refund_id,
        "amount": record["amount_collected"],
        "status": "simulated",
        "note": "Simulated refund (mode=fake). No real money moved.",
    }


async def _refund_via_stripe(payment_ref: str, reason: str) -> dict:
    try:
        import stripe
    except ImportError:
        raise CheckoutError(
            "the stripe package is required in live mode (pip install stripe)"
        )
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise CheckoutError("STRIPE_SECRET_KEY is not set (live mode)")
    stripe.api_key = secret
    try:
        refund = stripe.Refund.create(
            payment_intent=payment_ref, reason="requested_by_customer"
        )
    except Exception as e:
        raise CheckoutError(f"Stripe refund failed for {payment_ref!r}: {e}")
    record = await ledger.record_refund(payment_ref, refund.id)
    return {
        "refund_id": refund.id,
        "amount": record["amount_collected"],
        "status": refund.status,
    }


def get_business_payment() -> Payment:
    """Rosebud's own business payment method for paying florists.

    In live mode this comes from server-side env vars — it is Rosebud's
    money, never customer card data. In fake mode a dummy card is used
    (never charged).
    """
    if _mode() != "live":
        return Payment(
            card_number="4111111111111111",
            exp_month="12",
            exp_year="2030",
            cvc="123",
            name="Rosebud Business (simulated)",
        )
    missing = [
        name
        for name in (
            "ROSEBUD_BUSINESS_CARD_NUMBER",
            "ROSEBUD_BUSINESS_CARD_EXP_MONTH",
            "ROSEBUD_BUSINESS_CARD_EXP_YEAR",
            "ROSEBUD_BUSINESS_CARD_CVC",
            "ROSEBUD_BUSINESS_CARD_NAME",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise CheckoutError(
            "live mode needs Rosebud's business card configured: "
            + ", ".join(missing)
        )
    payment = Payment(
        card_number=os.environ["ROSEBUD_BUSINESS_CARD_NUMBER"],
        exp_month=os.environ["ROSEBUD_BUSINESS_CARD_EXP_MONTH"],
        exp_year=os.environ["ROSEBUD_BUSINESS_CARD_EXP_YEAR"],
        cvc=os.environ["ROSEBUD_BUSINESS_CARD_CVC"],
        name=os.environ["ROSEBUD_BUSINESS_CARD_NAME"],
    )
    payment.validate()
    return payment


def expected_fee(collected: float, florist_charged: float) -> float:
    """The Rosebud margin on an order; should always equal SERVICE_FEE."""
    return round(collected - florist_charged, 2)
