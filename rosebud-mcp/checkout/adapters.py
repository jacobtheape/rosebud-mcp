"""Florist adapters: search, quote, checkout, status, cancel.

Each adapter encapsulates one florist's storefront. The MCP tools call
through these; the customer only ever talks to the agent.

Checkout modes:
  fake  - FakeFloristAdapter simulates the whole flow with no network.
          This is the default: the server can never place a real order
          unless ROSEBUD_CHECKOUT_MODE=live is set explicitly.
  live  - TelefloraAdapter drives a headless browser through teleflora.com.
          Selectors were mapped against the live site on 2026-09-22
          (ZIP 10038, delivery 2026-09-25). 1-800-Flowers remains
          scaffolding with unvalidated selectors.

Quoting in live mode: exact delivery fees and tax are only revealed on
Teleflora's checkout review page, so quote_details() performs a full
walkthrough to that page (filling the delivery form, placing nothing)
and reads the real numbers. That walkthrough is too slow to run per
search result, so live adapters set exact_search_filter = False: search
pre-filters on product price and get_quote locks the exact total, which
the user approves before anything moves.
"""

import os
import random
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class CheckoutError(Exception):
    """A checkout step failed; safe to surface to the agent."""


@dataclass
class Arrangement:
    arrangement_id: str
    florist: str
    name: str
    description: str
    product_price: float
    url: str | None = None

    def to_dict(self) -> dict:
        return {
            "arrangement_id": self.arrangement_id,
            "florist": self.florist,
            "name": self.name,
            "description": self.description,
            "product_price": round(self.product_price, 2),
        }


@dataclass
class OrderResult:
    order_id: str
    florist: str
    product_name: str
    total_charged: float  # charged AT THE FLORIST with Rosebud's business
    # payment method (merchant total: product + delivery + tax, WITHOUT the
    # Rosebud service fee). What the customer paid Rosebud is tracked
    # separately in the payment ledger. 0.0 on dry runs (nothing charged).
    delivery_date: str
    confirmation: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "florist": self.florist,
            "product_name": self.product_name,
            "total_charged": round(self.total_charged, 2),
            "delivery_date": self.delivery_date,
            "confirmation": self.confirmation,
        }


@dataclass
class Payment:
    """Rosebud's business payment method for paying florists.

    Under the merchant-of-record model this is ROSEBUD'S money, sourced
    server-side from env vars (see payments.get_business_payment) — never
    customer card data. Raw customer card numbers never touch this server:
    the customer pays Rosebud through the platform, and the platform hands
    purchase() a payment ref, not card details.
    Card data is never logged or persisted.
    """

    card_number: str
    exp_month: str
    exp_year: str
    cvc: str
    name: str

    def validate(self) -> None:
        digits = "".join(c for c in self.card_number if c.isdigit())
        if len(digits) < 13 or len(digits) > 19:
            raise CheckoutError("card_number looks invalid")
        if not (self.exp_month.isdigit() and 1 <= int(self.exp_month) <= 12):
            raise CheckoutError("card exp_month looks invalid")
        if not (self.exp_year.isdigit() and len(self.exp_year) in (2, 4)):
            raise CheckoutError("card exp_year looks invalid")
        if not (self.cvc.isdigit() and 3 <= len(self.cvc) <= 4):
            raise CheckoutError("card cvc looks invalid")


class FloristAdapter(ABC):
    """One florist storefront."""

    name: str = "base"
    # True: the server can exactly budget-filter search results by calling
    # quote_details per option (cheap, e.g. fake mode). False: exact totals
    # need a full checkout walkthrough per option (too slow for search);
    # the server pre-filters on product price and get_quote locks the
    # exact total for the chosen arrangement instead.
    exact_search_filter: bool = True

    @abstractmethod
    async def search(
        self,
        occasion: str,
        budget_max: float,
        delivery_zip: str,
        delivery_date: str,
    ) -> list[Arrangement]:
        """Arrangements matching the occasion.

        budget_max is a product-price pre-filter only; the exact all-in
        budget check (product + delivery + tax + the Rosebud fee) happens
        in get_quote / at approval time, so implementations must not rely
        on search alone to enforce the customer's budget.
        """

    @abstractmethod
    async def quote_details(
        self,
        arrangement_id: str,
        delivery_zip: str,
        delivery_date: str | None = None,
        recipient: dict | None = None,
    ) -> tuple[float, float, float]:
        """Exact (product_price, delivery_fee, tax) for the arrangement.

        Live adapters walk the florist's checkout to the review page to
        read the real numbers. No order is placed and nothing is charged.
        """

    @abstractmethod
    async def checkout(
        self,
        arrangement_id: str,
        recipient: dict,
        delivery_date: str,
        card_message: str,
        sender_name: str,
        payment: Payment,
        expected_total: float,
        dry_run: bool = False,
    ) -> OrderResult:
        """Run the purchase, paying the florist with Rosebud's business
        payment method. expected_total is the MERCHANT total (product +
        delivery + tax, WITHOUT the Rosebud service fee) — the fee is
        Rosebud's margin and is never charged at the florist. Must refuse
        if the live total differs from expected_total instead of
        charging. dry_run walks the whole flow, fills payment, verifies
        the live total, but does NOT click the final place-order button:
        total_charged is 0.0 and raw carries the totals seen."""

    @abstractmethod
    async def status(self, order_id: str) -> dict:
        """Order status details."""

    @abstractmethod
    async def cancel(self, order_id: str) -> dict:
        """Cancel the order; returns cancelled true/false plus detail."""


# ---------------------------------------------------------------- fake ---
class FakeFloristAdapter(FloristAdapter):
    """Simulated national-florist checkout. No network, no real orders.

    Used for development, demos, and the MCP smoke test. Catalog prices
    are static; delivery/tax are simulated estimates.
    """

    name = "fake"

    CATALOG: dict[str, list[tuple[str, str, float]]] = {
        "birthday": [
            ("FA-BD-1", "Birthday Brights Bouquet", "Mixed seasonal blooms in festive colors", 59.99),
            ("FA-BD-2", "Deluxe Birthday Garden", "Roses, lilies, and daisies in a keepsake vase", 79.99),
        ],
        "anniversary": [
            ("FA-AN-1", "Two Dozen Red Roses", "Classic long-stem red roses", 89.99),
            ("FA-AN-2", "Elegant Orchid Plant", "White phalaenopsis in a ceramic pot", 69.99),
        ],
        "sympathy": [
            ("FA-SY-1", "Peaceful White Lilies", "White lilies and greens sympathy arrangement", 74.99),
            ("FA-SY-2", "Serene Garden Basket", "Soft whites and greens in a woven basket", 64.99),
        ],
        "get_well": [
            ("FA-GW-1", "Cheerful Daisy Bouquet", "Bright daisies to lift spirits", 49.99),
            ("FA-GW-2", "Sunshine Tulip Bunch", "Yellow tulips wrapped in kraft", 54.99),
        ],
        "congratulations": [
            ("FA-CG-1", "Celebration Roses & Lilies", "Pink roses with white lilies", 69.99),
            ("FA-CG-2", "Grand Iris Arrangement", "Purple iris and mixed blooms", 59.99),
        ],
        "love": [
            ("FA-LV-1", "Dozen Red Roses", "A dozen classic red roses", 64.99),
            ("FA-LV-2", "Rose & Chocolate Duo", "Roses paired with a chocolate box", 79.99),
        ],
    }

    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}
        self._rng = random.Random(20260920)

    def _catalog(self, occasion: str) -> list[tuple[str, str, float]]:
        key = occasion.strip().lower().replace(" ", "_")
        return self.CATALOG.get(key, self.CATALOG["birthday"])

    def _price(self, arrangement_id: str) -> float:
        price = 59.99
        for items in self.CATALOG.values():
            for aid, _name, _desc, p in items:
                if aid == arrangement_id:
                    price = p
                    break
        return price

    async def search(self, occasion, budget_max, delivery_zip, delivery_date):
        return [
            Arrangement(
                arrangement_id=aid,
                florist="Demo Florist (simulated)",
                name=name,
                description=desc,
                product_price=price,
            )
            for aid, name, desc, price in self._catalog(occasion)
            if price <= budget_max
        ]

    async def quote_details(self, arrangement_id, delivery_zip,
                            delivery_date=None, recipient=None):
        # Simulated: flat delivery fee + 8% tax estimate on product.
        price = self._price(arrangement_id)
        return price, 14.99, round(price * 0.08, 2)

    async def checkout(self, arrangement_id, recipient, delivery_date,
                       card_message, sender_name, payment, expected_total,
                       dry_run=False):
        payment.validate()
        name = arrangement_id
        price = None
        for items in self.CATALOG.values():
            for aid, n, _desc, p in items:
                if aid == arrangement_id:
                    name, price = n, p
                    break
        if dry_run:
            return OrderResult(
                order_id="DRYRUN-FAKE-" + uuid.uuid4().hex[:8].upper(),
                florist="Demo Florist (simulated)",
                product_name=name,
                total_charged=0.0,
                delivery_date=delivery_date,
                confirmation="Dry run (fake mode): no order placed.",
                raw={"note": "simulated dry run"},
            )
        if price is not None:
            # Mirror the live _assert_total guard: refuse if the chargeable
            # merchant total drifted from the approved amount. The merchant
            # total EXCLUDES the Rosebud service fee — the fee is margin,
            # never charged at the florist.
            recomputed = round(price + 14.99 + round(price * 0.08, 2), 2)
            if abs(recomputed - expected_total) > 0.01:
                raise CheckoutError(
                    f"computed merchant total ${recomputed:.2f} differs from "
                    f"approved ${expected_total:.2f}; refusing to charge."
                )
        order_id = "RB-" + uuid.uuid4().hex[:8].upper()
        self._orders[order_id] = {
            "order_id": order_id,
            "status": "Processing",
            "product_name": name,
            "total_charged": expected_total,
            "delivery_date": delivery_date,
            "recipient": recipient,
            "card_message": card_message,
            "sender_name": sender_name,
            "created_at": time.time(),
            "cancelled": False,
        }
        return OrderResult(
            order_id=order_id,
            florist="Demo Florist (simulated)",
            product_name=name,
            total_charged=expected_total,
            delivery_date=delivery_date,
            confirmation="Simulated checkout complete (mode=fake). No real order placed.",
        )

    async def status(self, order_id):
        order = self._orders.get(order_id)
        if order is None:
            return {"error": f"unknown order_id: {order_id}"}
        return {
            "order_id": order_id,
            "status": "Cancelled" if order["cancelled"] else order["status"],
            "product_name": order["product_name"],
            "total_charged": order["total_charged"],
            "delivery_date": order["delivery_date"],
        }

    async def cancel(self, order_id):
        order = self._orders.get(order_id)
        if order is None:
            return {"error": f"unknown order_id: {order_id}"}
        if order["cancelled"]:
            return {"order_id": order_id, "cancelled": False,
                    "detail": "order was already cancelled"}
        order["cancelled"] = True
        return {"order_id": order_id, "cancelled": True,
                "detail": "Simulated cancellation accepted (mode=fake)."}


# ------------------------------------------------- real-site adapters ---
class _BrowserAdapter(FloristAdapter):
    """Shared headless-checkout skeleton for real florist sites."""

    exact_search_filter = False
    base_url: str = ""
    checkout_timeout_ms: int = 60_000

    def _new_page(self):
        from .browser import browser_page
        return browser_page(headless=True)

    def _sel(self, name: str, mapping: dict) -> str:
        sel = mapping.get(name, "")
        if not sel:
            raise CheckoutError(
                f"checkout selector {name!r} is not mapped yet for "
                f"{self.name}; refusing to continue rather than guessing."
            )
        return sel

    async def _dismiss_popups(self, page) -> None:
        """Best-effort dismissal of cookie/marketing/survey overlays."""
        for sel in (
            "#onetrust-accept-btn-handler",
            ".onetrust-close-btn-handler",
            "[aria-label='Close']",
            "button.close",
            ".modal-close",
        ):
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(500)
            except Exception:
                pass
        # Some overlays only close with Escape.
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _fill_or_select(self, page, selector: str, value: str) -> None:
        """Fill a text input or choose a dropdown option, whichever it is."""
        try:
            tag = await page.eval_on_selector(
                selector, "el => el.tagName.toLowerCase()"
            )
        except Exception as e:
            raise CheckoutError(
                f"checkout field {selector} not found: {e}"
            )
        if tag == "select":
            await page.select_option(selector, value)
        else:
            await page.fill(selector, value)


class TelefloraAdapter(_BrowserAdapter):
    """Teleflora.com headless checkout.

    Mapped against the live site 2026-09-22 (ZIP 10038): product ->
    cart -> deliveryInfo.jsp -> billing_review.jsp. No CAPTCHA, bot block,
    or rate limit was encountered. Cookie/marketing/survey popups appear
    but are dismissible.
    """

    name = "teleflora"
    base_url = "https://www.teleflora.com"

    # Occasion -> category page. Mapped from the Teleflora homepage nav/footer
    # 2026-09-22 (all five non-birthday URLs verified live).
    CATEGORY_URLS = {
        "birthday": "https://www.teleflora.com/birthday-flowers?catID=cat210012",
        "anniversary": "https://www.teleflora.com/anniversary-flowers?catID=cat440206",
        "sympathy": "https://www.teleflora.com/funeral-sympathy-collection",
        "get_well": "https://www.teleflora.com/get-well-flowers?catID=cat210073",
        "congratulations": "https://www.teleflora.com/congratulations-flowers?catID=cat210066",
        "love": "https://www.teleflora.com/love-romance-flowers?catID=cat210087",
    }

    # Selectors verified against the live site 2026-09-22 unless marked
    # PENDING (empty string = not yet mapped; _sel refuses rather than
    # guessing). NOTE: several delivery-form ids embed a dynamic session
    # token (e.g. id="address-sg114364766-1"), so selectors use
    # prefix/suffix matching on the stable parts instead of the full id.
    SEL = {
        # Product page — VERIFIED 2026-09-22
        "add_to_cart": "input#pdpAddToCartBtn",
        "zip_gate": "input#postalCode",  # "Enter Delivery Zip Code" field
        # Cart page — VERIFIED 2026-09-22
        "checkout_from_cart": "input#shoppingCartBtn1",
        # Delivery info page (deliveryInfo.jsp) — VERIFIED 2026-09-22
        "recipient_first_name": "#firstName1",
        "recipient_last_name": "#lastName1",
        "recipient_address": "input[id^='address-'][id$='-1']",
        "recipient_apt": "input[id^='address-'][id$='-2']",
        "recipient_city": "#city1",
        "recipient_state": "select[id^='state-']",  # native dropdown, full names
        "recipient_zip": "#zip-1",
        "recipient_phone": "#phone_number1",  # requires 10 digits
        "recipient_email": "#email1",
        "delivery_date": "#deliveryInfoDate-1",  # readonly; calendar widget
        "occasion": "",  # PENDING (native select, required, id not captured)
        "card_message": "textarea[id^='giftCardMessage-']",
        "sender_name": "input[id^='messageFrom-']",
        "billing_same_as_delivery": "",  # PENDING ("Use this address as my
        # Billing Address" checkbox exists; selector not captured)
        "delivery_continue": "input.btn-submit[value='Next: Billing & Review']",
        # Billing/review page (billing_review.jsp)
        "order_summary": "",  # PENDING
        "billing_name": "",  # PENDING (if no same-as-delivery)
        "billing_zip": "",  # PENDING (if no same-as-delivery)
        # Payment fields — VERIFIED 2026-09-22
        "cc_number": "#cc_number",
        "cc_cvv": "#cvv_number",
        "cc_month": "#cc_month",
        "cc_year": "#cc_year",
        # Final submit — VERIFIED 2026-09-22
        "place_order": "#billingReviewBtn",
        "place_order_alt": "button[name=billingReviewBtn]",
    }

    # Guest order tracking (no login): the header/footer "Order Status" link
    # lands on /account/orderTrackWithoutLogin.jsp, which asks for EMAIL
    # ONLY (input#orderTrackEmail) — no order-number field at that step.

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    async def search(self, occasion, budget_max, delivery_zip, delivery_date):
        key = occasion.strip().lower().replace(" ", "_")
        url = self.CATEGORY_URLS.get(key)
        if not url:
            raise CheckoutError(
                f"Teleflora category page for occasion {occasion!r} is not "
                "mapped yet; use 'birthday' or map the category first."
            )
        async with self._new_page() as page:
            await page.goto(url, timeout=self.checkout_timeout_ms)
            await self._dismiss_popups(page)
            # Product cards link to /bouquet/<slug>?prodID=... Collect each
            # card's name and price.
            cards = await page.query_selector_all("a[href*='/bouquet/']")
            seen: dict[str, Arrangement] = {}
            for a in cards:
                try:
                    href = await a.get_attribute("href")
                    if not href or "/bouquet/" not in href:
                        continue
                    full = href if href.startswith("http") else self.base_url + href
                    # Product URL is the stable arrangement id (strip
                    # tracking params, keep prodID/skuId which identify
                    # the purchasable variant).
                    if full in seen:
                        continue
                    text = (await a.text_content() or "").strip()
                    price_m = re.search(r"\$([\d,]+\.\d{2})", text)
                    if not price_m:
                        continue
                    price = float(price_m.group(1).replace(",", ""))
                    if price > budget_max:
                        continue
                    # Name: first non-empty line of the card text.
                    name = next(
                        (ln.strip() for ln in text.splitlines() if ln.strip()),
                        "Teleflora arrangement",
                    )
                    seen[full] = Arrangement(
                        arrangement_id=full,
                        florist="Teleflora",
                        name=name[:120],
                        description="",
                        product_price=price,
                        url=full,
                    )
                except Exception:
                    continue
            return list(seen.values())

    # ------------------------------------------------------------------
    # walkthrough shared by quote_details and checkout
    # ------------------------------------------------------------------
    async def _walk_to_review(self, page, arrangement_url, recipient,
                              delivery_date, card_message, sender_name):
        """Drive product -> cart -> delivery form -> review page.

        Leaves the page on billing_review.jsp with the exact order
        summary visible. Places nothing, charges nothing.
        """
        s = lambda n: self._sel(n, self.SEL)
        await page.goto(arrangement_url, timeout=self.checkout_timeout_ms)
        await self._dismiss_popups(page)
        await page.click(s("add_to_cart"), timeout=self.checkout_timeout_ms)
        # Land on the cart (the site redirects to /cart on success).
        try:
            await page.wait_for_url("**/cart**", timeout=15000)
        except Exception:
            await page.goto(f"{self.base_url}/cart",
                            timeout=self.checkout_timeout_ms)
        await self._dismiss_popups(page)
        await page.click(s("checkout_from_cart"),
                         timeout=self.checkout_timeout_ms)
        await page.wait_for_url("**/deliveryInfo.jsp**",
                                timeout=self.checkout_timeout_ms)
        await self._dismiss_popups(page)
        await self._fill_delivery(page, recipient, delivery_date,
                                  card_message, sender_name)
        await page.click(s("delivery_continue"),
                         timeout=self.checkout_timeout_ms)
        await page.wait_for_url("**/billing_review.jsp**",
                                timeout=self.checkout_timeout_ms)
        await self._dismiss_popups(page)
        return page

    async def _fill_delivery(self, page, recipient, delivery_date,
                             card_message, sender_name) -> None:
        s = lambda n: self._sel(n, self.SEL)
        await self._fill_or_select(page, s("recipient_first_name"),
                                   recipient["first_name"])
        await self._fill_or_select(page, s("recipient_last_name"),
                                   recipient["last_name"])
        await self._fill_or_select(page, s("recipient_address"),
                                   recipient["address1"])
        apt_sel = self.SEL.get("recipient_apt") or ""
        if apt_sel and recipient.get("address2"):
            await self._fill_or_select(page, apt_sel, recipient["address2"])
        await self._fill_or_select(page, s("recipient_city"), recipient["city"])
        await self._fill_or_select(page, s("recipient_state"),
                                   recipient["state"])
        await self._fill_or_select(page, s("recipient_zip"), recipient["zip"])
        if recipient.get("phone"):
            try:
                await self._fill_or_select(page, s("recipient_phone"),
                                           recipient["phone"])
            except CheckoutError:
                pass
        email_sel = self.SEL.get("recipient_email") or ""
        if email_sel and recipient.get("email"):
            try:
                await self._fill_or_select(page, email_sel,
                                           recipient["email"])
            except CheckoutError:
                pass
        # Delivery date picker interaction is site-specific; implemented
        # once mapped.
        await self._set_delivery_date(page, delivery_date)
        if card_message:
            try:
                await self._fill_or_select(page, s("card_message"),
                                           card_message)
            except CheckoutError:
                pass
        if sender_name:
            try:
                await self._fill_or_select(page, s("sender_name"),
                                           sender_name)
            except CheckoutError:
                pass

    async def _set_delivery_date(self, page, delivery_date: str) -> None:
        raise CheckoutError(
            "Teleflora delivery-date picker is not mapped yet; refusing "
            "to guess."
        )

    def _parse_totals(self, text: str) -> dict:
        """Parse product/delivery/tax/total dollar amounts from the order
        summary text."""
        def find(pattern: str) -> float | None:
            ms = re.findall(pattern, text or "", flags=re.IGNORECASE)
            if not ms:
                return None
            return float(ms[-1].replace(",", ""))

        product = find(r"(?:subtotal|merchandise|item\s+subtotal)[^\d$]*\$([\d,]+\.\d{2})")
        delivery = find(r"delivery[^\d$]*\$([\d,]+\.\d{2})")
        tax = find(r"(?<!\w)tax[^\d$]*\$([\d,]+\.\d{2})")
        # "total" also appears inside "subtotal" — word boundary + last match.
        total = find(r"\btotal\b[^\d$]*\$([\d,]+\.\d{2})")
        if total is None:
            raise CheckoutError(
                "could not read the checkout total from the review page; "
                "refusing to charge."
            )
        return {
            "product": product,
            "delivery": delivery,
            "tax": tax,
            "total": total,
        }

    async def _read_totals(self, page) -> dict:
        text = ""
        summary_sel = self.SEL.get("order_summary") or ""
        if summary_sel:
            try:
                el = await page.query_selector(summary_sel)
                if el:
                    text = (await el.text_content()) or ""
            except Exception:
                text = ""
        if "$" not in text:
            text = (await page.text_content("body")) or ""
        totals = self._parse_totals(text)
        try:
            h1 = await page.text_content("h1")
            totals["product_name"] = (h1 or "").strip()[:150]
        except Exception:
            pass
        totals["raw_text"] = text[:2000]
        return totals

    async def _fill_payment(self, page, payment: Payment) -> None:
        """Fill ROSEBUD'S business card into Teleflora's payment form.

        Never customer card data. Filling the form charges nothing; only
        the final place-order click charges.
        """
        s = lambda n: self._sel(n, self.SEL)
        # Prefer "billing same as delivery" when the site offers it.
        same_sel = self.SEL.get("billing_same_as_delivery") or ""
        if same_sel:
            try:
                box = await page.query_selector(same_sel)
                if box and not await box.is_checked():
                    await box.check()
            except Exception:
                pass
        await self._fill_or_select(page, s("cc_number"), payment.card_number)
        # Month/year may be selects or text inputs; _fill_or_select handles
        # both. Values are zero-padded just in case.
        month = payment.exp_month.zfill(2)
        year = payment.exp_year
        if len(year) == 2:
            year = "20" + year
        await self._fill_or_select(page, s("cc_month"), month)
        await self._fill_or_select(page, s("cc_year"), year)
        # Year dropdowns sometimes want 2-digit years; retry if 4-digit
        # didn't take.
        try:
            val = await page.eval_on_selector(
                s("cc_year"), "el => el.value || ''")
            if val and year not in val and payment.exp_year not in val:
                await self._fill_or_select(page, s("cc_year"),
                                           payment.exp_year)
        except Exception:
            pass
        await self._fill_or_select(page, s("cc_cvv"), payment.cvc)
        name_sel = self.SEL.get("billing_name") or ""
        if name_sel:
            try:
                await self._fill_or_select(page, name_sel, payment.name)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # quote_details / checkout / status / cancel
    # ------------------------------------------------------------------
    async def quote_details(self, arrangement_id, delivery_zip,
                            delivery_date=None, recipient=None):
        if not delivery_date or not recipient:
            raise CheckoutError(
                "Teleflora exact quotes need the delivery date and "
                "recipient address (tax and delivery fee are computed at "
                "checkout)."
            )
        recipient = dict(recipient)
        recipient["zip"] = delivery_zip
        async with self._new_page() as page:
            await self._walk_to_review(page, arrangement_id, recipient,
                                       delivery_date, "", "")
            totals = await self._read_totals(page)
        if totals["product"] is None or totals["delivery"] is None \
                or totals["tax"] is None:
            raise CheckoutError(
                "could not parse the exact product/delivery/tax amounts "
                "from Teleflora's review page; refusing to quote."
            )
        return totals["product"], totals["delivery"], totals["tax"]

    async def checkout(self, arrangement_id, recipient, delivery_date,
                       card_message, sender_name, payment, expected_total,
                       dry_run=False):
        payment.validate()
        async with self._new_page() as page:
            await self._walk_to_review(page, arrangement_id, recipient,
                                       delivery_date, card_message,
                                       sender_name)
            await self._fill_payment(page, payment)
            totals = await self._read_totals(page)
            live_total = totals["total"]
            if abs(live_total - expected_total) > 0.01:
                raise CheckoutError(
                    f"live total ${live_total:.2f} differs from approved "
                    f"${expected_total:.2f}; refusing to charge. Get a "
                    "fresh quote and fresh user approval."
                )
            if dry_run:
                return OrderResult(
                    order_id="DRYRUN-" + uuid.uuid4().hex[:8].upper(),
                    florist="Teleflora",
                    product_name=totals.get("product_name")
                    or arrangement_id,
                    total_charged=0.0,
                    delivery_date=delivery_date,
                    confirmation=(
                        "Dry run: walked the live Teleflora checkout to "
                        "the final review, verified the live total, and "
                        "did NOT click Place Order. Nothing was charged."
                    ),
                    raw={
                        "live_product": totals["product"],
                        "live_delivery": totals["delivery"],
                        "live_tax": totals["tax"],
                        "live_total": live_total,
                    },
                )
            # Final, irreversible step. Only reached with explicit user
            # approval of the exact total, verified line above.
            try:
                await page.click(self.SEL["place_order"],
                                 timeout=self.checkout_timeout_ms)
            except Exception:
                await page.click(self._sel("place_order_alt", self.SEL),
                                 timeout=self.checkout_timeout_ms)
            await page.wait_for_load_state("networkidle",
                                           timeout=self.checkout_timeout_ms)
            body = (await page.text_content("body")) or ""
            m = re.search(
                r"(?:order|confirmation|receipt)\s*(?:number|#|no\.?)?\s*[:#]?\s*([A-Z0-9-]{6,})",
                body, flags=re.IGNORECASE)
            order_number = m.group(1) if m else "TF-" + uuid.uuid4().hex[:8].upper()
            return OrderResult(
                order_id=order_number,
                florist="Teleflora",
                product_name=totals.get("product_name") or arrangement_id,
                total_charged=expected_total,
                delivery_date=delivery_date,
                confirmation=body[:500],
                raw={
                    "live_product": totals["product"],
                    "live_delivery": totals["delivery"],
                    "live_tax": totals["tax"],
                    "live_total": live_total,
                },
            )

    async def status(self, order_id):
        # Teleflora's self-service order tracking was not mapped; the
        # confirmation email carries tracking. Refuse rather than guess.
        raise CheckoutError(
            "Teleflora live order tracking is not mapped yet; check the "
            "order confirmation email for status."
        )

    async def cancel(self, order_id):
        # No self-service cancellation flow was mapped on teleflora.com.
        # Surface this plainly so the agent can arrange cancellation
        # (confirmation-email link or phone) instead of pretending.
        raise CheckoutError(
            "Teleflora has no mapped self-service cancellation; cancel "
            f"order {order_id} via the confirmation email link or by "
            "phone, then record the refund."
        )


class Flowers1800Adapter(_BrowserAdapter):
    """1-800-Flowers.com headless checkout. Selectors NOT validated."""

    name = "1800flowers"
    base_url = "https://www.1800flowers.com"

    async def search(self, occasion, budget_max, delivery_zip, delivery_date):
        raise CheckoutError(
            "1-800-Flowers live search is not validated yet; use fake mode "
            "or validate selectors against 1800flowers.com first."
        )

    async def quote_details(self, arrangement_id, delivery_zip,
                            delivery_date=None, recipient=None):
        raise CheckoutError("1-800-Flowers live quoting is not validated yet.")

    async def checkout(self, arrangement_id, recipient, delivery_date,
                       card_message, sender_name, payment, expected_total,
                       dry_run=False):
        raise CheckoutError(
            "1-800-Flowers live checkout is not validated yet.")

    async def status(self, order_id):
        raise CheckoutError(
            "1-800-Flowers live order tracking is not validated yet.")

    async def cancel(self, order_id):
        raise CheckoutError(
            "1-800-Flowers live cancellation is not validated yet.")


ADAPTERS: dict[str, type[FloristAdapter]] = {
    "fake": FakeFloristAdapter,
    "teleflora": TelefloraAdapter,
    "1800flowers": Flowers1800Adapter,
}

_adapter_instances: dict[str, FloristAdapter] = {}


def get_adapter() -> FloristAdapter:
    """Adapter for the configured checkout mode (default: fake)."""
    mode = os.environ.get("ROSEBUD_CHECKOUT_MODE", "fake").strip().lower()
    if mode == "live":
        # Live mode needs an explicit florist choice; default to teleflora.
        florist = os.environ.get("ROSEBUD_FLORIST", "teleflora").strip().lower()
        if florist not in ("teleflora", "1800flowers"):
            raise CheckoutError(f"unknown ROSEBUD_FLORIST: {florist}")
        mode = florist
    if mode not in ADAPTERS:
        raise CheckoutError(f"unknown ROSEBUD_CHECKOUT_MODE: {mode}")
    if mode not in _adapter_instances:
        _adapter_instances[mode] = ADAPTERS[mode]()
    return _adapter_instances[mode]
