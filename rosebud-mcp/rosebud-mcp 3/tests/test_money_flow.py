"""End-to-end money-flow tests for Rosebud (fake mode, memory store).

Run:  .venv/bin/python tests/test_money_flow.py
Covers the Option B merchant-of-record flow plus the concurrency and
refund-gap paths added with the persistent store.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ROSEBUD_CHECKOUT_MODE", "fake")
os.environ.setdefault("ROSEBUD_STORE", "memory")

import server  # noqa: E402
from checkout import ledger  # noqa: E402

RECIPIENT = {
    "first_name": "Test",
    "last_name": "User",
    "address1": "1 Test St",
    "city": "Jersey City",
    "state": "NJ",
    "zip": "07310",
}

passed = []


def check(name, cond):
    assert cond, f"FAILED: {name}"
    passed.append(name)


async def fresh_quote():
    res = await server.search_arrangements(
        occasion="birthday",
        budget_max=200.0,
        delivery_zip="07310",
        delivery_date="2026-10-05",
    )
    assert "options" in res and res["options"], res
    opt = res["options"][0]
    return await server.get_quote(
        arrangement_id=opt["arrangement_id"],
        occasion="birthday",
        first_name="Test",
        last_name="User",
        address1="1 Test St",
        city="Jersey City",
        state="NJ",
        zip="07310",
        delivery_date="2026-10-05",
        card_message="Happy birthday!",
        sender_name="Jacob",
    )


async def main():
    # --- happy path -------------------------------------------------------
    quote = await fresh_quote()
    check("quote total is 82.77", quote["total"] == 82.77)
    fee_line = [li for li in quote["line_items"] if "fee" in li["label"].lower()]
    check("fee is its own line item", fee_line and fee_line[0]["amount"] == 2.99)

    r = await server.purchase(
        quote_id=quote["quote_id"],
        user_confirmed=False,
        platform_payment_ref="pay_fake_nope",
        payment_amount=82.77,
    )
    check("purchase refused without approval", "refused" in r["error"])

    r = await server.purchase(
        quote_id="Q-BOGUS",
        user_confirmed=True,
        platform_payment_ref="pay_fake_a",
        payment_amount=82.77,
    )
    check("unknown quote refused", "unknown or expired" in r["error"])

    r = await server.purchase(
        quote_id=quote["quote_id"],
        user_confirmed=True,
        platform_payment_ref="pay_fake_b",
        payment_amount=80.00,
    )
    check("amount mismatch refused", "does not equal" in r["error"])

    r = await server.purchase(
        quote_id=quote["quote_id"],
        user_confirmed=True,
        platform_payment_ref="bogus_ref",
        payment_amount=82.77,
    )
    check("bad payment ref refused", "unknown platform_payment_ref" in r["error"])

    r = await server.purchase(
        quote_id=quote["quote_id"],
        user_confirmed=True,
        platform_payment_ref="pay_fake_happy",
        payment_amount=82.77,
    )
    check("purchase succeeded", r.get("order_id") is not None, )
    check("customer charged 82.77", r["charged_to_customer"] == 82.77)
    check("florist paid 79.78", r["paid_to_florist"] == 79.78)
    check("fee kept is 2.99", r["rosebud_service_fee"] == 2.99)
    order_id = r["order_id"]

    r = await server.purchase(
        quote_id=quote["quote_id"],
        user_confirmed=True,
        platform_payment_ref="pay_fake_happy2",
        payment_amount=82.77,
    )
    check("quote reuse refused", "already used" in r["error"])

    quote2 = await fresh_quote()
    r = await server.purchase(
        quote_id=quote2["quote_id"],
        user_confirmed=True,
        platform_payment_ref="pay_fake_happy",  # same ref as happy path
        payment_amount=82.77,
    )
    check("payment ref reuse refused", "already used" in r["error"])

    # --- cancellation refunds ---------------------------------------------
    r = await server.cancel_order(order_id=order_id, user_confirmed=False)
    check("cancel refused without approval", "refused" in r["error"])

    r = await server.cancel_order(order_id=order_id, user_confirmed=True)
    check("cancel succeeded", r.get("cancelled") is True)
    check("refund issued", r["refund"]["refund_id"].startswith("re_fake_"))

    r = await server.cancel_order(order_id=order_id, user_confirmed=True)
    check(
        "second cancel reports already-cancelled, no new refund",
        r.get("cancelled") is False and "refund" not in r,
    )

    # ledger-level: a second refund of the same payment is refused
    payments = ledger._backend._payments  # memory-backend internals
    pay_ref = next(
        ref for ref, rec in payments.items() if rec["order_id"] == order_id
    )
    try:
        await ledger.record_refund(pay_ref, "re_fake_double")
        check("double refund refused", False)
    except Exception as e:
        check("double refund refused", "already refunded" in str(e))

    st = await server.order_status(order_id=order_id)
    check("order cancelled", st["status"] == "Cancelled")

    # --- concurrent double-purchase on one quote: exactly one wins --------
    quote3 = await fresh_quote()
    results = await asyncio.gather(
        server.purchase(
            quote_id=quote3["quote_id"],
            user_confirmed=True,
            platform_payment_ref="pay_fake_race1",
            payment_amount=82.77,
        ),
        server.purchase(
            quote_id=quote3["quote_id"],
            user_confirmed=True,
            platform_payment_ref="pay_fake_race2",
            payment_amount=82.77,
        ),
    )
    winners = [x for x in results if "order_id" in x]
    losers = [x for x in results if "order_id" not in x]
    check("exactly one purchase wins the race", len(winners) == 1)
    check("loser told quote was used", "already used" in losers[0]["error"])
    # clean up the winner so the ledger stays tidy
    await server.cancel_order(
        order_id=winners[0]["order_id"], user_confirmed=True
    )

    # --- lost claim AFTER payment capture: payment must be refunded -------
    # (forces the interleaving the Postgres backend can produce in prod)
    backend = server.quotes._backend
    orig_claim = backend.claim

    async def always_lose(quote_id):
        return False

    backend.claim = always_lose
    try:
        quote5 = await fresh_quote()
        r = await server.purchase(
            quote_id=quote5["quote_id"],
            user_confirmed=True,
            platform_payment_ref="pay_fake_racelose",
            payment_amount=82.77,
        )
        check("lost claim errors", "already used" in r["error"])
        check("lost claim refunds the payment", "refunded" in r["error"])
        payments = ledger._backend._payments  # memory-backend internals
        check(
            "loser payment marked refunded",
            payments["pay_fake_racelose"]["status"] == "refunded",
        )
    finally:
        backend.claim = orig_claim

    # --- business-card failure refunds the captured payment ---------------
    import checkout.payments as paymod

    orig = paymod.get_business_payment

    def boom():
        from checkout.adapters import CheckoutError

        raise CheckoutError("business card not configured (test)")

    paymod.get_business_payment = boom
    server.get_business_payment = boom
    try:
        quote4 = await fresh_quote()
        r = await server.purchase(
            quote_id=quote4["quote_id"],
            user_confirmed=True,
            platform_payment_ref="pay_fake_bizfail",
            payment_amount=82.77,
        )
        check("business-card failure errors", "error" in r)
        check(
            "customer refunded on business-card failure",
            "refunded" in r["error"],
        )
        payments = ledger._backend._payments  # memory-backend internals
        check(
            "payment marked refunded in ledger",
            payments["pay_fake_bizfail"]["status"] == "refunded",
        )
    finally:
        paymod.get_business_payment = orig
        server.get_business_payment = orig

    print(f"\nALL {len(passed)} CHECKS PASSED")


asyncio.run(main())
