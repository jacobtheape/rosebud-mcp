"""Storage backends for quotes and the payment ledger.

ROSEBUD_STORE (default: "memory")
  memory    In-process dicts. Zero-config; fine for fake mode, dev, and
            single-worker deploys. State is lost on restart.
  postgres  Shared Postgres via DATABASE_URL. Required for production /
            multi-worker deploys. Single-use guarantees (quotes, payment
            refs) are enforced atomically by the database, so they hold
            across workers and restarts.

Backends exchange plain dict "payloads"/"records" and never import the
domain objects, so there are no import cycles: quotes.py and payments.py
own the Quote/record shapes and translate at the boundary.

Money is stored as integer cents. Public record dicts carry dollar
floats for API compatibility.
"""

import asyncio
import os
import time
from pathlib import Path

from .adapters import CheckoutError

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _store_mode() -> str:
    return os.environ.get("ROSEBUD_STORE", "memory").strip().lower()


def _cents(amount: float) -> int:
    return int(round(amount * 100))


def _dollars(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100, 2)


# ---------------------------------------------------------------------------
# Quote backends: payloads are plain dicts (see quotes._quote_to_payload).
# ---------------------------------------------------------------------------


class MemoryQuoteBackend:
    def __init__(self) -> None:
        self._quotes: dict[str, dict] = {}

    async def create(self, payload: dict) -> None:
        self._quotes[payload["quote_id"]] = dict(payload)

    async def get(self, quote_id: str) -> dict | None:
        payload = self._quotes.get(quote_id)
        return dict(payload) if payload else None

    async def claim(self, quote_id: str) -> bool:
        # Single-threaded asyncio: no await between check and set, so this
        # is atomic for the memory backend.
        payload = self._quotes.get(quote_id)
        if payload is None or payload.get("used"):
            return False
        payload["used"] = True
        return True

    async def release(self, quote_id: str) -> None:
        payload = self._quotes.get(quote_id)
        if payload is not None:
            payload["used"] = False


# ---------------------------------------------------------------------------
# Ledger backends: internal records use integer cents.
# ---------------------------------------------------------------------------


def _public_record(rec: dict) -> dict:
    return {
        "payment_ref": rec["payment_ref"],
        "amount_collected": _dollars(rec["amount_cents"]),
        "status": rec["status"],
        "order_id": rec["order_id"],
        "florist_charged": _dollars(rec["florist_charged_cents"]),
        "fee_kept": _dollars(rec["fee_kept_cents"]),
        "refund_id": rec["refund_id"],
        "captured_at": rec["captured_at"],
        "refunded_at": rec["refunded_at"],
    }


class MemoryLedgerBackend:
    def __init__(self) -> None:
        self._payments: dict[str, dict] = {}
        self._orders: dict[str, str] = {}

    def _new_record(self, payment_ref: str, amount_cents: int) -> dict:
        return {
            "payment_ref": payment_ref,
            "amount_cents": amount_cents,
            "status": "captured",
            "order_id": None,
            "florist_charged_cents": None,
            "fee_kept_cents": None,
            "refund_id": None,
            "captured_at": time.time(),
            "refunded_at": None,
        }

    async def record_capture(
        self, payment_ref: str, amount_cents: int
    ) -> dict | None:
        if payment_ref in self._payments:
            return None  # already used
        record = self._new_record(payment_ref, amount_cents)
        self._payments[payment_ref] = record
        return _public_record(record)

    async def link_order(
        self, payment_ref: str, order_id: str, florist_charged_cents: int
    ) -> dict | None:
        record = self._payments.get(payment_ref)
        if record is None:
            return None
        record["order_id"] = order_id
        record["florist_charged_cents"] = florist_charged_cents
        record["fee_kept_cents"] = (
            record["amount_cents"] - florist_charged_cents
        )
        self._orders[order_id] = payment_ref
        return _public_record(record)

    async def record_refund(
        self, payment_ref: str, refund_id: str
    ) -> tuple[str, dict | None]:
        record = self._payments.get(payment_ref)
        if record is None:
            return ("unknown", None)
        if record["status"] == "refunded":
            return ("already_refunded", _public_record(record))
        record["status"] = "refunded"
        record["refund_id"] = refund_id
        record["refunded_at"] = time.time()
        return ("ok", _public_record(record))

    async def payment_for_order(self, order_id: str) -> dict | None:
        ref = self._orders.get(order_id)
        if not ref:
            return None
        return _public_record(self._payments[ref])


# ---------------------------------------------------------------------------
# Postgres backends (lazy driver import: memory mode never needs psycopg).
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = asyncio.Lock()


async def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError:
            raise CheckoutError(
                "ROSEBUD_STORE=postgres needs the Postgres driver: "
                "pip install 'psycopg[binary]' psycopg-pool"
            )
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise CheckoutError(
                "ROSEBUD_STORE=postgres needs DATABASE_URL to be set"
            )
        _pool = AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False)
        await _pool.open()
        schema = SCHEMA_PATH.read_text()
        async with _pool.connection() as conn:
            for stmt in schema.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(stmt)
        return _pool


class PostgresQuoteBackend:
    async def _pool(self):
        return await _get_pool()

    async def create(self, payload: dict) -> None:
        from psycopg.types.json import Json

        pool = await self._pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO quotes (quote_id, payload, created_at, expires_at)"
                " VALUES (%s, %s, to_timestamp(%s), to_timestamp(%s))",
                (
                    payload["quote_id"],
                    Json(payload),
                    payload["created_at"],
                    payload["expires_at"],
                ),
            )

    async def get(self, quote_id: str) -> dict | None:
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT payload, (used_at IS NOT NULL) AS used"
                " FROM quotes WHERE quote_id = %s",
                (quote_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        payload = dict(row[0])
        payload["used"] = bool(row[1])
        return payload

    async def claim(self, quote_id: str) -> bool:
        # Atomic single-use: only one worker can flip used_at from NULL.
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE quotes SET used_at = now()"
                " WHERE quote_id = %s AND used_at IS NULL"
                " AND expires_at > now()",
                (quote_id,),
            )
            return cur.rowcount == 1

    async def release(self, quote_id: str) -> None:
        pool = await self._pool()
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE quotes SET used_at = NULL WHERE quote_id = %s",
                (quote_id,),
            )


def _row_to_internal(row) -> dict:
    return {
        "payment_ref": row[0],
        "amount_cents": row[1],
        "status": row[2],
        "order_id": row[3],
        "florist_charged_cents": row[4],
        "fee_kept_cents": row[5],
        "refund_id": row[6],
        "captured_at": row[7].timestamp() if row[7] else None,
        "refunded_at": row[8].timestamp() if row[8] else None,
    }


_PAYMENT_COLS = (
    "payment_ref, amount_cents, status, order_id, florist_charged_cents,"
    " fee_kept_cents, refund_id, captured_at, refunded_at"
)


class PostgresLedgerBackend:
    async def _pool(self):
        return await _get_pool()

    async def record_capture(
        self, payment_ref: str, amount_cents: int
    ) -> dict | None:
        # The PRIMARY KEY is the single-use guarantee: a reused ref
        # inserts zero rows, atomically, across all workers.
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO payments (payment_ref, amount_cents, status)"
                " VALUES (%s, %s, 'captured')"
                " ON CONFLICT (payment_ref) DO NOTHING"
                " RETURNING " + _PAYMENT_COLS,
                (payment_ref, amount_cents),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return _public_record(_row_to_internal(row))

    async def link_order(
        self, payment_ref: str, order_id: str, florist_charged_cents: int
    ) -> dict | None:
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE payments SET order_id = %s,"
                " florist_charged_cents = %s,"
                " fee_kept_cents = amount_cents - %s"
                " WHERE payment_ref = %s"
                " RETURNING " + _PAYMENT_COLS,
                (order_id, florist_charged_cents, florist_charged_cents,
                 payment_ref),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return _public_record(_row_to_internal(row))

    async def record_refund(
        self, payment_ref: str, refund_id: str
    ) -> tuple[str, dict | None]:
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE payments SET status = 'refunded', refund_id = %s,"
                " refunded_at = now()"
                " WHERE payment_ref = %s AND status = 'captured'"
                " RETURNING " + _PAYMENT_COLS,
                (refund_id, payment_ref),
            )
            row = await cur.fetchone()
            if row is not None:
                return ("ok", _public_record(_row_to_internal(row)))
            cur = await conn.execute(
                "SELECT status FROM payments WHERE payment_ref = %s",
                (payment_ref,),
            )
            exists = await cur.fetchone()
        if exists is None:
            return ("unknown", None)
        return ("already_refunded", None)

    async def payment_for_order(self, order_id: str) -> dict | None:
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT " + _PAYMENT_COLS + " FROM payments"
                " WHERE order_id = %s",
                (order_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return _public_record(_row_to_internal(row))


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_quote_backend():
    if _store_mode() == "postgres":
        return PostgresQuoteBackend()
    return MemoryQuoteBackend()


def make_ledger_backend():
    if _store_mode() == "postgres":
        return PostgresLedgerBackend()
    return MemoryLedgerBackend()
