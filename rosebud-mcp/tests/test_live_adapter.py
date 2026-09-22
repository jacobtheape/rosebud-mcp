"""Tests for the live Teleflora adapter's parsing and refusal behavior.

Run:  /tmp/rosebud-test/bin/python tests/test_live_adapter.py
These never touch the network: parsing runs on captured sample text,
refusal paths raise CheckoutError before any browser is launched.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkout.adapters import TelefloraAdapter, Payment, CheckoutError  # noqa: E402

PASSED = []
FAILED = []


def check(name, fn):
    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        PASSED.append(name)
    except Exception as e:  # noqa: BLE001
        FAILED.append(f"{name}: {e!r}")


# Sample order-summary text in the shape Teleflora's billing_review.jsp
# shows (captured 2026-09-22 for the Best Wishes bouquet, ZIP 10038):
SAMPLE_SUMMARY = """
Order Summary
Teleflora's Best Wishes Bouquet
Subtotal $79.99
Delivery $19.99
Tax $8.87
Order Total $108.85
"""


def t_parse_sample():
    a = TelefloraAdapter()
    t = a._parse_totals(SAMPLE_SUMMARY)
    assert t["product"] == 79.99, t
    assert t["delivery"] == 19.99, t
    assert t["tax"] == 8.87, t
    assert t["total"] == 108.85, t


def t_parse_no_subtotal_confusion():
    # "Total" must not match the "$79.99" inside "Subtotal".
    a = TelefloraAdapter()
    t = a._parse_totals(SAMPLE_SUMMARY)
    assert t["total"] == 108.85, t


def t_parse_missing_total_refuses():
    a = TelefloraAdapter()
    try:
        a._parse_totals("Subtotal $79.99\nDelivery $19.99")
    except CheckoutError:
        return
    raise AssertionError("expected CheckoutError when no total is readable")


def t_parse_missing_line_items_refuses_quote():
    # quote_details needs product/delivery/tax; without them it must not
    # return a guess. Exercise _parse_totals on a thin summary.
    a = TelefloraAdapter()
    t = a._parse_totals("Some text\nOrder Total $50.00\n")
    assert t["product"] is None and t["delivery"] is None and t["tax"] is None


def t_pending_selector_refuses():
    a = TelefloraAdapter()
    try:
        a._sel("order_summary", TelefloraAdapter.SEL)
    except CheckoutError:
        return
    raise AssertionError("expected CheckoutError for PENDING selector")


def t_mapped_selectors_resolve():
    a = TelefloraAdapter()
    assert a._sel("add_to_cart", TelefloraAdapter.SEL) == "input#pdpAddToCartBtn"
    assert a._sel("checkout_from_cart", TelefloraAdapter.SEL) == "input#shoppingCartBtn1"
    assert a._sel("recipient_phone", TelefloraAdapter.SEL) == "#phone_number1"
    assert a._sel("delivery_continue", TelefloraAdapter.SEL) == \
        "input.btn-submit[value='Next: Billing & Review']"
    assert a._sel("cc_number", TelefloraAdapter.SEL) == "#cc_number"
    assert a._sel("place_order", TelefloraAdapter.SEL) == "#billingReviewBtn"


async def t_search_unknown_occasion_refuses():
    a = TelefloraAdapter()
    try:
        await a.search("graduation", 100, "10038", "2026-09-25")
    except CheckoutError:
        return
    raise AssertionError("expected CheckoutError for unmapped occasion")


async def t_status_refuses():
    a = TelefloraAdapter()
    try:
        await a.status("TF-123")
    except CheckoutError:
        return
    raise AssertionError("expected CheckoutError for unmapped status")


async def t_cancel_refuses():
    a = TelefloraAdapter()
    try:
        await a.cancel("TF-123")
    except CheckoutError:
        return
    raise AssertionError("expected CheckoutError for unmapped cancel")


def t_delivery_date_refuses():
    a = TelefloraAdapter()

    async def run():
        # _set_delivery_date is called during the walkthrough; confirm it
        # refuses before we have a mapped picker.
        try:
            await a._set_delivery_date(None, "2026-09-25")
        except CheckoutError:
            return
        raise AssertionError("expected CheckoutError for unmapped date picker")

    asyncio.run(run())


def t_payment_validate():
    good = Payment("4242424242424242", "12", "2027", "123", "Rosebud Works LLC")
    good.validate()
    for bad in [
        Payment("123", "12", "2027", "123", "x"),
        Payment("4242424242424242", "13", "2027", "123", "x"),
        Payment("4242424242424242", "12", "27", "12", "x"),
    ]:
        try:
            bad.validate()
        except CheckoutError:
            continue
        raise AssertionError(f"expected invalid payment to fail: {bad}")


for name, fn in [
    ("parse sample summary", t_parse_sample),
    ("total not confused by subtotal", t_parse_no_subtotal_confusion),
    ("missing total refuses", t_parse_missing_total_refuses),
    ("missing line items parse as None", t_parse_missing_line_items_refuses_quote),
    ("PENDING selector refuses", t_pending_selector_refuses),
    ("mapped selectors resolve", t_mapped_selectors_resolve),
    ("unknown occasion refuses", t_search_unknown_occasion_refuses),
    ("status refuses", t_status_refuses),
    ("cancel refuses", t_cancel_refuses),
    ("unmapped date picker refuses", t_delivery_date_refuses),
    ("payment validation", t_payment_validate),
]:
    check(name, fn)

print(f"{len(PASSED)} passed, {len(FAILED)} failed")
for f in FAILED:
    print("FAIL:", f)
if FAILED:
    sys.exit(1)
print("ALL LIVE-ADAPTER CHECKS PASSED")
