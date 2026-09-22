"""Rosebud MCP server — US flower delivery as agent tools.

The customer only ever talks to the agent. The agent uses these tools;
Rosebud's backend runs the florist checkout (headless browser in live
mode, simulated in the default fake mode).

Checkout mode (env ROSEBUD_CHECKOUT_MODE):
  fake  simulated checkout, no network, no real orders (default)
  live  headless-browser checkout at a real florist site
        (also set ROSEBUD_FLORIST=teleflora|1800flowers)

Tool flow:
  1. search_arrangements  find bouquets for the occasion within budget
  2. get_quote             lock the exact total (product + delivery +
                           tax + $2.99 Rosebud service fee, its own line)
  3. present the exact quote to the user and get explicit approval
  4. platform charges the customer the exact total into Rosebud's
     account and returns a platform_payment_ref
  5. purchase              runs ONLY with user_confirmed=true, a valid,
                           unexpired, unused quote_id, and a verified
                           platform_payment_ref. Rosebud pays the florist
                           the merchant total (WITHOUT the fee) with its
                           own business payment method and keeps the
                           $2.99 fee as margin.
  6. order_status / delivery_confirmation / cancel_order to follow through
     (cancel_order refunds the customer payment)

Environment:
  ROSEBUD_API_KEY      Bearer token clients must present (required)
  ROSEBUD_CHECKOUT_MODE  fake (default) or live
  ROSEBUD_FLORIST        teleflora (default) or 1800flowers, live mode only
  STRIPE_SECRET_KEY      live mode: verifies customer payments + refunds
  ROSEBUD_BUSINESS_CARD_*  live mode: Rosebud's business card used to pay
                         florists (NUMBER, EXP_MONTH, EXP_YEAR, CVC, NAME)
  ROSEBUD_STORE        memory (default) or postgres. Postgres (with
                       DATABASE_URL) is required for production /
                       multi-worker deploys: quotes, payment refs, and
                       refunds are enforced atomically by the database.
  DATABASE_URL         Postgres connection string, ROSEBUD_STORE=postgres
  PORT                 HTTP port (default 8000)

Endpoints:
  POST /mcp        MCP streamable-HTTP endpoint (auth required)
  GET  /health     Liveness check (no auth)

Run:
  uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import os
import re

from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from checkout import (
    SERVICE_FEE,
    QuoteStore,
    get_adapter,
    get_business_payment,
    ledger,
    refund_customer_payment,
    verify_customer_payment,
)
from checkout.adapters import CheckoutError

quotes = QuoteStore()

server = MCPServer(
    "rosebud",
    instructions=(
        "Rosebud sends flowers anywhere in the US through a conversation. "
        "The customer only talks to you; you operate the florist checkout "
        "through these tools. Workflow: collect the recipient's first and "
        "last name, full US street address, city, state, ZIP, delivery date "
        "(YYYY-MM-DD), total budget, card note, occasion, and sender name. "
        "Call search_arrangements for options within budget, then get_quote "
        "to lock the exact total. Present the exact arrangement, florist, "
        "delivery details, and total — with the $2.99 Rosebud service fee "
        "as its own line item — to the user, and only call purchase after "
        "the user explicitly approves that exact quote and the platform "
        "confirms the customer's payment to Rosebud (platform_payment_ref). "
        "Rosebud is the merchant of record: the customer pays Rosebud, "
        "Rosebud pays the florist the merchant total with its own business "
        "payment method, and the $2.99 fee is Rosebud's margin. No customer "
        "card data is ever handled. Spend can never be "
        "pre-authorized, batched, or automated. A quote expires after 30 "
        "minutes and is single-use; if anything changes, get a fresh quote "
        "and fresh approval. Track with order_status and "
        "delivery_confirmation; cancel_order needs explicit user approval "
        "too. US addresses only."
    ),
)

ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


def _check_zip(zip_code: str) -> str | None:
    z = (zip_code or "").strip()
    if not ZIP_RE.match(z):
        return f"invalid US ZIP code: {zip_code!r}"
    digits = z[:5]
    # Reject ZIPs that can never be real: repeated digits ("00000") or
    # outside the assigned range 00501-99950. Full deliverability is still
    # confirmed by the florist at quote/checkout time.
    if len(set(digits)) == 1 or not 501 <= int(digits) <= 99950:
        return f"invalid US ZIP code: {zip_code!r}"
    return None


@server.tool()
async def search_arrangements(
    occasion: str,
    budget_max: float,
    delivery_zip: str,
    delivery_date: str,
) -> dict:
    """Find flower arrangements for an occasion within the total budget.

    occasion: birthday, anniversary, sympathy, get_well, congratulations,
    love. budget_max is the most the customer will spend total (flowers +
    delivery + tax + the $2.99 Rosebud fee). Returns options with
    arrangement_id, name, florist, product_price, description. Pass a
    chosen arrangement_id to get_quote.
    """
    if err := _check_zip(delivery_zip):
        return {"error": err}
    if budget_max <= 0:
        return {"error": "budget_max must be positive"}
    try:
        adapter = get_adapter()
        options = await adapter.search(
            occasion, budget_max, delivery_zip, delivery_date
        )
    except CheckoutError as e:
        return {"error": str(e)}
    # budget_max is the customer's total spend (flowers + delivery + tax +
    # the $2.99 Rosebud fee). Adapters that can quote cheaply (fake mode)
    # get the exact all-in check per option here. Live adapters need a full
    # checkout walkthrough for exact totals — too slow per search result —
    # so search pre-filters on product price and get_quote locks the exact
    # total for the chosen arrangement, which the user approves.
    if adapter.exact_search_filter:
        in_budget = []
        for o in options:
            try:
                product_price, delivery_fee, tax = await adapter.quote_details(
                    o.arrangement_id, delivery_zip, delivery_date, None
                )
            except CheckoutError:
                continue
            if product_price + delivery_fee + tax + SERVICE_FEE <= budget_max + 1e-9:
                in_budget.append(o)
        options = in_budget
        note = (
            "Prices are the arrangement alone; get_quote adds delivery, "
            "tax, and the $2.99 Rosebud service fee for the exact total."
            if options
            else "No arrangements found within that budget."
        )
    else:
        options = [o for o in options if o.product_price <= budget_max]
        note = (
            "Product prices only — get_quote walks the live checkout to "
            "lock the exact total (delivery + tax + the $2.99 Rosebud "
            "service fee) before you present it for approval. A chosen "
            "option can exceed the budget once delivery and tax are added; "
            "the exact total is what the user approves."
            if options
            else "No arrangements found within that budget."
        )
    return {"options": [o.to_dict() for o in options], "note": note}


@server.tool()
async def get_quote(
    arrangement_id: str,
    occasion: str,
    first_name: str,
    last_name: str,
    address1: str,
    city: str,
    state: str,
    zip: str,
    delivery_date: str,
    card_message: str,
    sender_name: str,
    phone: str | None = None,
    special_instructions: str | None = None,
) -> dict:
    """Lock the exact total for an arrangement + delivery.

    occasion is the same value used in search_arrangements (birthday,
    anniversary, sympathy, get_well, congratulations, love). Returns a
    quote_id, line items (product, delivery, estimated tax, $2.99 Rosebud
    service fee), and the exact total. Present every line to the user and
    get explicit approval of the exact total before calling purchase.
    Quotes expire after 30 minutes and are single-use.
    """
    if err := _check_zip(zip):
        return {"error": err}
    try:
        adapter = get_adapter()
        options = await adapter.search(occasion, 99999, zip, delivery_date)
        arrangement = next(
            (o for o in options if o.arrangement_id == arrangement_id), None
        )
        if arrangement is None:
            # Live mode: allow a direct florist product URL as the
            # arrangement id (the walkthrough validates it and reads the
            # exact price). Anything else must come from search.
            if arrangement_id.startswith("https://www.teleflora.com/"):
                from checkout.adapters import Arrangement as _Arrangement

                arrangement = _Arrangement(
                    arrangement_id=arrangement_id,
                    florist="Teleflora",
                    name="Teleflora arrangement",
                    description="",
                    product_price=0.0,
                    url=arrangement_id,
                )
            else:
                return {"error": f"unknown arrangement_id: {arrangement_id}"}
    except CheckoutError as e:
        return {"error": str(e)}
    recipient = {
        "first_name": first_name,
        "last_name": last_name,
        "address1": address1,
        "city": city,
        "state": state,
        "zip": zip,
    }
    if phone:
        recipient["phone"] = phone
    if special_instructions:
        recipient["special_instructions"] = special_instructions
    try:
        product_price, delivery_fee, tax = await adapter.quote_details(
            arrangement.arrangement_id, zip, delivery_date, recipient
        )
    except CheckoutError as e:
        return {"error": str(e)}
    if arrangement.product_price and abs(
        arrangement.product_price - product_price
    ) > 0.01:
        # The listing price moved since search; the walkthrough price is
        # authoritative.
        arrangement.product_price = product_price
    quote = await quotes.create(
        arrangement_id=arrangement.arrangement_id,
        florist=arrangement.florist,
        product_name=arrangement.name,
        product_price=product_price,
        delivery_fee=delivery_fee,
        tax=tax,
        recipient=recipient,
        delivery_date=delivery_date,
        card_message=card_message,
        sender_name=sender_name,
    )
    return quote.to_dict()


@server.tool()
async def purchase(
    quote_id: str,
    user_confirmed: bool,
    platform_payment_ref: str,
    payment_amount: float,
) -> dict:
    """Run the florist checkout for an approved quote.

    Rosebud is the merchant of record. The customer pays Rosebud the exact
    approved total through the platform; Rosebud pays the florist the
    merchant total (product + delivery + tax, WITHOUT the $2.99 fee) with
    its own business payment method and keeps the $2.99 fee as margin. No
    customer card data is accepted or handled here — raw card numbers
    never touch this server.

    ONLY call this after the user explicitly approved the exact quote:
    arrangement, florist, delivery address, delivery date, card note, and
    the total including the $2.99 Rosebud service fee as a separate line
    item. Pass user_confirmed=true to attest to that approval; the call
    is refused otherwise. The quote must be unexpired and unused.

    platform_payment_ref is the platform's proof that the customer paid
    Rosebud (a Stripe payment intent id in live mode); payment_amount is
    what the platform collected and must equal the quote total exactly.
    The checkout refuses to charge the florist if the live merchant total
    differs from the approved amount, and the customer payment is
    refunded.
    """
    if not user_confirmed:
        return {
            "error": "refused: user_confirmed must be true. Present the exact "
                     "quote to the user and obtain explicit approval first."
        }
    quote = await quotes.get(quote_id)
    if quote is None:
        return {
            "error": f"unknown or expired quote_id: {quote_id}. "
                     "Get a fresh quote and fresh user approval."
        }
    if quote.used:
        return {
            "error": "that quote was already used. Get a fresh quote and "
                     "fresh user approval."
        }
    if abs(payment_amount - quote.total) > 0.01:
        return {
            "error": f"refused: payment_amount ${payment_amount:.2f} does not "
                     f"equal the approved total ${quote.total:.2f}. The "
                     "customer must pay Rosebud exactly the approved total."
        }
    try:
        payment_record = await verify_customer_payment(
            platform_payment_ref, quote.total
        )
    except CheckoutError as e:
        return {"error": str(e)}
    try:
        business_payment = get_business_payment()
    except CheckoutError as e:
        # Nothing was ordered, but the customer payment was already
        # captured: refund it rather than holding the money.
        try:
            refund = await refund_customer_payment(
                payment_record["payment_ref"],
                reason="business payment not configured",
            )
            refund_note = (
                " Customer payment refunded: "
                f"{refund['refund_id']} (${refund['amount']:.2f})."
            )
        except CheckoutError as re:
            refund_note = (
                f" REFUND FAILED ({re}) — manual refund of "
                f"{payment_record['payment_ref']} is required."
            )
        return {"error": str(e) + refund_note}
    dry_run = os.environ.get("ROSEBUD_DRY_RUN", "").strip().lower() in (
        "1", "true", "yes",
    )
    merchant_total = round(
        quote.product_price + quote.delivery_fee + quote.tax, 2
    )
    if dry_run:
        # Trial mode: prove the live florist walkthrough and the total
        # assertion without placing an order. The quote is NOT claimed
        # (nothing is charged) and stays valid; the verified customer
        # payment is refunded immediately since no florist order exists.
        try:
            adapter = get_adapter()
            result = await adapter.checkout(
                arrangement_id=quote.arrangement_id,
                recipient=quote.recipient,
                delivery_date=quote.delivery_date,
                card_message=quote.card_message,
                sender_name=quote.sender_name,
                payment=business_payment,
                expected_total=merchant_total,
                dry_run=True,
            )
        except CheckoutError as e:
            try:
                refund = await refund_customer_payment(
                    payment_record["payment_ref"],
                    reason="dry run aborted",
                )
                refund_note = (
                    " Customer payment refunded: "
                    f"{refund['refund_id']} (${refund['amount']:.2f})."
                )
            except CheckoutError as re:
                refund_note = (
                    f" REFUND FAILED ({re}) — manual refund of "
                    f"{payment_record['payment_ref']} is required."
                )
            return {"error": "dry run aborted: " + str(e) + refund_note}
        try:
            refund = await refund_customer_payment(
                payment_record["payment_ref"],
                reason="dry run — no florist order placed",
            )
        except CheckoutError as e:
            return {
                "error": (
                    "dry run walked the checkout successfully, but the "
                    f"customer-payment refund FAILED ({e}) — manual refund "
                    f"of {payment_record['payment_ref']} is required."
                ),
                "live_line_items": result.raw,
            }
        return {
            "dry_run": True,
            "note": (
                "Dry run complete: walked the live florist checkout to the "
                "final review, verified the live merchant total matches "
                "the approved quote, and did NOT place the order. The "
                "customer payment was captured and refunded in full; the "
                "florist was never charged."
            ),
            "quote_id": quote_id,
            "quote_still_valid": True,
            "line_items": quote.line_items(),
            "approved_total": quote.total,
            "live_merchant_total_seen": result.raw.get("live_total"),
            "live_line_items": result.raw,
            "refund": {
                "refund_id": refund["refund_id"],
                "amount": refund["amount"],
                "status": refund["status"],
            },
            "next": (
                "Present the live totals to the user for explicit approval. "
                "For the real purchase: unset ROSEBUD_DRY_RUN, get a fresh "
                "platform_payment_ref (refs are single-use), and call "
                "purchase again — the same quote may be reused if unexpired."
            ),
        }
    # Claim the quote BEFORE charging the florist, atomically: two
    # concurrent purchases can never charge the florist twice on one
    # quote, even across workers.
    if not await quotes.claim(quote_id):
        try:
            refund = await refund_customer_payment(
                payment_record["payment_ref"],
                reason="quote already used",
            )
            refund_note = (
                " Customer payment refunded: "
                f"{refund['refund_id']} (${refund['amount']:.2f})."
            )
        except CheckoutError as re:
            refund_note = (
                f" REFUND FAILED ({re}) — manual refund of "
                f"{payment_record['payment_ref']} is required."
            )
        return {
            "error": "that quote was already used. Get a fresh quote and "
                     "fresh user approval." + refund_note
        }
    # merchant_total was computed above (shared by the dry-run branch).
    try:
        adapter = get_adapter()
        result = await adapter.checkout(
            arrangement_id=quote.arrangement_id,
            recipient=quote.recipient,
            delivery_date=quote.delivery_date,
            card_message=quote.card_message,
            sender_name=quote.sender_name,
            payment=business_payment,
            expected_total=merchant_total,
        )
    except CheckoutError as e:
        # Adapters refuse BEFORE charging on drift, so the florist was not
        # paid. Release the quote claim and refund the customer payment so
        # no money is held.
        await quotes.release(quote_id)
        try:
            refund = await refund_customer_payment(
                payment_record["payment_ref"],
                reason="florist checkout failed",
            )
            refund_note = (
                " Customer payment refunded: "
                f"{refund['refund_id']} (${refund['amount']:.2f})."
            )
        except CheckoutError as re:
            refund_note = (
                f" REFUND FAILED ({re}) — manual refund of "
                f"{payment_record['payment_ref']} is required."
            )
        return {"error": str(e) + refund_note}
    await ledger.link_order(
        payment_record["payment_ref"], result.order_id, result.total_charged
    )
    response = result.to_dict()
    response["line_items"] = quote.line_items()
    response["charged_to_customer"] = quote.total
    response["paid_to_florist"] = round(result.total_charged, 2)
    response["rosebud_service_fee"] = SERVICE_FEE
    response["platform_payment_ref"] = payment_record["payment_ref"]
    response["fee_note"] = (
        f"The customer paid Rosebud ${quote.total:.2f}. Rosebud paid the "
        f"florist ${result.total_charged:.2f} and kept the "
        f"${SERVICE_FEE:.2f} service fee as margin."
    )
    return response


@server.tool()
async def order_status(order_id: str) -> dict:
    """Get an order's details and status."""
    try:
        return await get_adapter().status(order_id)
    except CheckoutError as e:
        return {"error": str(e)}


@server.tool()
async def cancel_order(order_id: str, user_confirmed: bool) -> dict:
    """Cancel an order and refund the customer. Needs the user's explicit
    approval (user_confirmed=true)."""
    if not user_confirmed:
        return {
            "error": "refused: user_confirmed must be true. Confirm the "
                     "cancellation with the user first."
        }
    try:
        result = await get_adapter().cancel(order_id)
    except CheckoutError as e:
        return {"error": str(e)}
    if not result.get("cancelled"):
        return result
    record = await ledger.payment_for_order(order_id)
    if record is None:
        result["refund"] = {
            "status": "unknown",
            "note": "no customer payment record for this order; "
                    "verify the refund manually.",
        }
        return result
    if record["status"] == "refunded":
        result["refund"] = {
            "status": "already_refunded",
            "refund_id": record["refund_id"],
        }
        return result
    try:
        result["refund"] = await refund_customer_payment(
            record["payment_ref"], reason="order cancelled"
        )
    except CheckoutError as e:
        result["refund"] = {
            "status": "failed",
            "error": str(e),
            "note": "manual refund required.",
        }
    return result


@server.tool()
async def delivery_confirmation(order_id: str) -> dict:
    """Delivery confirmation for an order: delivered/unconfirmed status,
    delivery time, and recipient signature when the florist provides them."""
    try:
        status = await get_adapter().status(order_id)
    except CheckoutError as e:
        return {"error": str(e)}
    if "error" in status:
        return status
    status["delivery_confirmation"] = (
        "Delivery confirmation details come from the fulfilling florist; "
        "check order_status for the latest state."
    )
    return status


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        expected = os.environ.get("ROSEBUD_API_KEY")
        if not expected:
            return PlainTextResponse(
                "server misconfigured: ROSEBUD_API_KEY not set", status_code=500
            )
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {expected}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    info: dict = {"ok": True, "service": "rosebud-mcp"}
    try:
        from checkout.storage import _store_mode
        mode = _store_mode()
    except Exception:
        mode = "unknown"
    info["store"] = mode
    if mode == "postgres":
        # Eager connectivity check: the Postgres pool is created lazily on
        # first tool call, so /health is the honest place to prove the DB is
        # reachable. No secrets are exposed; only the outcome is reported.
        try:
            from checkout.storage import _get_pool
            pool = await asyncio.wait_for(_get_pool(), timeout=10)
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
            info["db"] = "ok"
        except Exception as exc:
            info["ok"] = False
            info["db"] = f"error: {type(exc).__name__}"
    return JSONResponse(info)


# Build the MCP app directly (not mounted as a sub-app: its lifespan must run
# on the served app or the streamable-HTTP task group never initializes),
# then layer the API-key auth middleware on top.
app = server.streamable_http_app()
app.routes.append(Route("/health", health))
app.add_middleware(BearerAuthMiddleware)
