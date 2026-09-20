"""Florist adapters: search, quote, checkout, status, cancel.

Each adapter encapsulates one florist's storefront. The MCP tools call
through these; the customer only ever talks to the agent.

Checkout modes:
  fake  - FakeFloristAdapter simulates the whole flow with no network.
          This is the default: the server can never place a real order
          unless ROSEBUD_CHECKOUT_MODE=live is set explicitly.
  live  - TelefloraAdapter / Flowers1800Adapter drive a headless browser.

WARNING on live mode: the real-site adapters are structural scaffolding.
Their selectors are marked VALIDATE and must be verified against the live
sites before use. Both Teleflora and 1-800-Flowers run anti-bot measures;
expect CAPTCHAs or blocks and have a fallback plan. No live purchase has
been tested end to end.
"""

import os
import random
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
    # separately in the payment ledger.
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

    @abstractmethod
    async def search(
        self,
        occasion: str,
        budget_max: float,
        delivery_zip: str,
        delivery_date: str,
    ) -> list[Arrangement]:
        """Arrangements matching the occasion.

        budget_max is a product-price pre-filter only; the server applies
        the exact all-in budget check (product + delivery + tax + the
        Rosebud fee) using quote_details, so implementations must not rely
        on search alone to enforce the customer's budget.
        """

    @abstractmethod
    async def quote_details(
        self, arrangement_id: str, delivery_zip: str
    ) -> tuple[float, float]:
        """(delivery_fee, tax_estimate) for the arrangement + ZIP."""

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
    ) -> OrderResult:
        """Run the purchase, paying the florist with Rosebud's business
        payment method. expected_total is the MERCHANT total (product +
        delivery + tax, WITHOUT the Rosebud service fee) — the fee is
        Rosebud's margin and is never charged at the florist. Must refuse
        if the live total differs from expected_total instead of
        charging."""

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

    async def quote_details(self, arrangement_id, delivery_zip):
        # Simulated: flat delivery fee + 8% tax estimate on product.
        price = 59.99
        for items in self.CATALOG.values():
            for aid, _name, _desc, p in items:
                if aid == arrangement_id:
                    price = p
                    break
        return 14.99, round(price * 0.08, 2)

    async def checkout(self, arrangement_id, recipient, delivery_date,
                       card_message, sender_name, payment, expected_total):
        payment.validate()
        name, price = arrangement_id, None
        for items in self.CATALOG.values():
            for aid, n, _desc, p in items:
                if aid == arrangement_id:
                    name, price = n, p
                    break
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
            confirmation=f"Simulated checkout complete (mode=fake). No real order placed.",
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


# ------------------------------------------------- real-site scaffolding ---
class _BrowserAdapter(FloristAdapter):
    """Shared headless-checkout skeleton for real florist sites.

    Subclasses fill in site-specific steps. Every selector is marked
    VALIDATE until verified against the live site.
    """

    base_url: str = ""
    checkout_timeout_ms: int = 60_000

    async def _new_page(self):
        from .browser import browser_page
        return browser_page(headless=True)

    async def _fill_recipient(self, page, recipient: dict) -> None:
        # VALIDATE: field selectors against the live checkout form.
        await page.fill("#recipient-first-name", recipient["first_name"])
        await page.fill("#recipient-last-name", recipient["last_name"])
        await page.fill("#recipient-address", recipient["address1"])
        await page.fill("#recipient-city", recipient["city"])
        await page.select_option("#recipient-state", recipient["state"])
        await page.fill("#recipient-zip", recipient["zip"])
        if recipient.get("phone"):
            await page.fill("#recipient-phone", recipient["phone"])

    async def _fill_payment(self, page, payment: Payment) -> None:
        # VALIDATE: field selectors against the live checkout form.
        # This fills ROSEBUD'S business card into the merchant page —
        # never customer card data. Card data is never logged or stored
        # by Rosebud.
        await page.fill("#card-number", payment.card_number)
        await page.fill("#card-exp-month", payment.exp_month)
        await page.fill("#card-exp-year", payment.exp_year)
        await page.fill("#card-cvc", payment.cvc)
        await page.fill("#card-name", payment.name)

    async def _assert_total(self, page, expected_total: float) -> None:
        # VALIDATE: selector for the order-total element.
        total_text = await page.text_content("#order-total")
        import re
        m = re.search(r"[\d,]+\.\d{2}", total_text or "")
        if not m:
            raise CheckoutError("could not read the checkout total; refusing to charge")
        live_total = float(m.group(0).replace(",", ""))
        if abs(live_total - expected_total) > 0.01:
            raise CheckoutError(
                f"live total ${live_total:.2f} differs from approved "
                f"${expected_total:.2f}; refusing to charge. Get a fresh "
                "quote and fresh user approval."
            )


class TelefloraAdapter(_BrowserAdapter):
    """Teleflora.com headless checkout. Selectors NOT validated."""

    name = "teleflora"
    base_url = "https://www.teleflora.com"

    async def search(self, occasion, budget_max, delivery_zip, delivery_date):
        # Production approach: drive Teleflora's search/category pages with
        # the occasion + ZIP, parse product cards. Not implemented until
        # selectors are validated.
        raise CheckoutError(
            "Teleflora live search is not validated yet; use fake mode or "
            "validate selectors against teleflora.com first."
        )

    async def quote_details(self, arrangement_id, delivery_zip):
        raise CheckoutError("Teleflora live quoting is not validated yet.")

    async def checkout(self, arrangement_id, recipient, delivery_date,
                       card_message, sender_name, payment, expected_total):
        payment.validate()
        async with self._new_page() as page:
            # VALIDATE each step against the live site.
            await page.goto(f"{self.base_url}/product/{arrangement_id}",
                            timeout=self.checkout_timeout_ms)
            await page.click("#add-to-cart")  # VALIDATE
            await page.click("#checkout")  # VALIDATE
            await self._fill_recipient(page, recipient)
            # VALIDATE: delivery-date picker interaction.
            await page.fill("#delivery-date", delivery_date)
            await page.fill("#card-message", card_message)
            await page.fill("#sender-name", sender_name)
            await self._fill_payment(page, payment)
            await self._assert_total(page, expected_total)
            await page.click("#place-order")  # VALIDATE
            await page.wait_for_selector("#order-confirmation",  # VALIDATE
                                         timeout=self.checkout_timeout_ms)
            confirmation = await page.text_content("#order-confirmation")
            order_id = "TF-" + uuid.uuid4().hex[:8].upper()
            return OrderResult(
                order_id=order_id,
                florist="Teleflora",
                product_name=arrangement_id,
                total_charged=expected_total,
                delivery_date=delivery_date,
                confirmation=(confirmation or "").strip()[:500],
            )

    async def status(self, order_id):
        raise CheckoutError("Teleflora live order tracking is not validated yet.")

    async def cancel(self, order_id):
        raise CheckoutError("Teleflora live cancellation is not validated yet.")


class Flowers1800Adapter(_BrowserAdapter):
    """1-800-Flowers.com headless checkout. Selectors NOT validated."""

    name = "1800flowers"
    base_url = "https://www.1800flowers.com"

    async def search(self, occasion, budget_max, delivery_zip, delivery_date):
        raise CheckoutError(
            "1-800-Flowers live search is not validated yet; use fake mode "
            "or validate selectors against 1800flowers.com first."
        )

    async def quote_details(self, arrangement_id, delivery_zip):
        raise CheckoutError("1-800-Flowers live quoting is not validated yet.")

    async def checkout(self, arrangement_id, recipient, delivery_date,
                       card_message, sender_name, payment, expected_total):
        payment.validate()
        async with self._new_page() as page:
            # VALIDATE each step against the live site.
            await page.goto(f"{self.base_url}/product/{arrangement_id}",
                            timeout=self.checkout_timeout_ms)
            await page.click("#add-to-cart")  # VALIDATE
            await page.click("#checkout")  # VALIDATE
            await self._fill_recipient(page, recipient)
            await page.fill("#delivery-date", delivery_date)  # VALIDATE
            await page.fill("#card-message", card_message)
            await page.fill("#sender-name", sender_name)
            await self._fill_payment(page, payment)
            await self._assert_total(page, expected_total)
            await page.click("#place-order")  # VALIDATE
            await page.wait_for_selector("#order-confirmation",  # VALIDATE
                                         timeout=self.checkout_timeout_ms)
            confirmation = await page.text_content("#order-confirmation")
            order_id = "FF-" + uuid.uuid4().hex[:8].upper()
            return OrderResult(
                order_id=order_id,
                florist="1-800-Flowers",
                product_name=arrangement_id,
                total_charged=expected_total,
                delivery_date=delivery_date,
                confirmation=(confirmation or "").strip()[:500],
            )

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
