# Rosebud MCP server

US flower delivery as Model Context Protocol tools, built for the Muse
Platform connector submission ("Existing MCP" connection type).

The customer only ever talks to the agent. The agent uses these tools;
Rosebud's backend runs the florist checkout — a headless browser at a
national florist in live mode, fully simulated in the default fake mode.

## Tools

| Tool | Purpose |
|---|---|
| `search_arrangements` | Bouquets for an occasion within the total budget |
| `get_quote` | Locks the exact total: product + delivery + tax + the $2.99 Rosebud service fee as its own line item. Expires after 30 min, single-use |
| `purchase` | Runs the checkout. **Refuses unless `user_confirmed=true`** and the quote is valid, unexpired, and unused. Refuses to charge if the live total differs from the approved quote |
| `order_status` | Order details and status |
| `cancel_order` | Cancels an order; needs `user_confirmed=true` |
| `delivery_confirmation` | Delivery proof details when the florist provides them |

## Checkout modes

`ROSEBUD_CHECKOUT_MODE` (default: `fake`)

- **fake** — simulated florist, no network, no real orders. Safe default:
  the server can never place a real order unless live mode is set
  explicitly. Used for development, demos, and smoke tests.
- **live** — headless-browser checkout at a real florist
  (`ROSEBUD_FLORIST=teleflora` or `1800flowers`; needs
  `pip install playwright && playwright install chromium`).

Live-mode adapters are structural scaffolding: their selectors are
marked `VALIDATE` and must be verified against the live sites before
use. Both Teleflora and 1-800-Flowers run anti-bot measures — expect
CAPTCHAs or blocks and have a fallback plan. No live purchase has been
tested end to end.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ROSEBUD_API_KEY=your-key \
  .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

- `POST /mcp` — MCP streamable-HTTP endpoint (`Authorization: Bearer <ROSEBUD_API_KEY>`)
- `GET /health` — liveness check, no auth

## Payments (merchant of record)

Rosebud is the merchant of record. The customer pays Rosebud the exact
approved total through the Muse Platform, which credits Rosebud's Stripe
account and hands the agent a `platform_payment_ref`. `purchase` takes
`platform_payment_ref` + `payment_amount` (verified: captured, exact
amount, single-use) — never customer card data. Raw card numbers never
touch this server.

Rosebud pays the florist the merchant total (product + delivery + tax,
WITHOUT the fee) with its own business payment method
(`ROSEBUD_BUSINESS_CARD_*` env vars in live mode; a dummy card in fake
mode). The $2.99 Rosebud service fee is disclosed as a separate line
item on every quote and order, and is kept by Rosebud as margin.
`cancel_order` (and any florist-total drift at checkout) refunds the
customer payment.

## Deploy

**Render (recommended):** `render.yaml` is a Blueprint — new Web Service →
deploy the repo, set `ROSEBUD_API_KEY` in the dashboard (auto-generated
in the blueprint). Docker runtime, health check on `/health`.

**Railway / Fly.io:** `Dockerfile` is included; set `ROSEBUD_API_KEY` as
an environment variable, expose `$PORT`. Keep `ROSEBUD_CHECKOUT_MODE`
at `fake` until live checkout is validated.

## Auth for the connector form

- Authentication method: **API keys** (Bearer token, `ROSEBUD_API_KEY`)
- The Muse Platform connects to `https://<your-host>/mcp` with the key.

## Still needed before submission

1. A hosted deployment (above) — the form needs a live MCP URL.
2. A Rosebud business entity + Stripe account (merchant of record).
3. Payment routing: confirm with the Muse Platform that the customer
   charge lands in Rosebud's Stripe account and the payment ref reaches
   `purchase`.
4. Live-checkout validation: verify selectors against Teleflora /
   1-800-Flowers, handle anti-bot/CAPTCHA, run a real test purchase and
   cancellation (reconcile every cent).
5. ~~A shared quote/order/payment store (Redis/Postgres) if deployed with
   more than one worker — the current stores are in-memory.~~ Done
   2026-09-20: `ROSEBUD_STORE=postgres` (with `DATABASE_URL`) gives a
   persistent, multi-worker-safe store — quotes, payment refs, and refunds
   are enforced atomically by Postgres (single-use holds across workers
   and restarts; money in integer cents). Default `memory` keeps
   zero-config dev/fake mode.

## Storage

| `ROSEBUD_STORE` | Behavior |
|---|---|
| `memory` (default) | In-process dicts. Fine for fake mode, dev, single worker. Lost on restart. |
| `postgres` | Shared Postgres via `DATABASE_URL`. Required for production / multi-worker. Schema (`checkout/schema.sql`) auto-applies on first connect. |

In `purchase`, the quote is now claimed atomically *before* the florist
is charged, so two concurrent purchases can never double-charge on one
quote — the loser gets its customer payment refunded. A captured payment
is also refunded if the business-card config is missing (previously the
money would have been held with no order).

## Parked

`fsn_client.py` — the earlier Flower Shop Network relay backend. Kept in
the repo; the direct-relay route is parked while the browser-checkout
route is the plan.
