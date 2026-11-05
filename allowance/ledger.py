"""SQLite ledger: settings, purchases, and an append-only event log."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from decimal import Decimal

from .money import fmt, money

# Purchase statuses
PROPOSED = "PROPOSED"
DENIED = "DENIED"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
PAID = "PAID"
CANCELED = "CANCELED"
REFUNDED = "REFUNDED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
PROCESSING = "PROCESSING"   # claimed by one caller, PayPal call in flight
REFUNDING = "REFUNDING"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchases (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  product_id TEXT NOT NULL,
  title TEXT NOT NULL,
  domain TEXT,
  offer_url TEXT,
  image_url TEXT,
  unit_price TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  total TEXT NOT NULL,
  currency TEXT NOT NULL,
  reason TEXT,
  decision TEXT NOT NULL,
  decision_reasons TEXT NOT NULL,
  status TEXT NOT NULL,
  order_id TEXT,
  capture_id TEXT,
  approve_url TEXT,
  refund_id TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS purchases_order ON purchases(order_id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  purchase_id TEXT,
  kind TEXT NOT NULL,
  detail TEXT
);
"""

PURCHASE_FIELDS = (
    "id", "created_at", "updated_at", "product_id", "title", "domain", "offer_url", "image_url",
    "unit_price", "quantity", "total", "currency", "reason", "decision", "decision_reasons",
    "status", "order_id", "capture_id", "approve_url", "refund_id", "error",
)


class Ledger:
    def __init__(self, path: str = ":memory:", now=time.time):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.now = now
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    # settings -----------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def delete_setting(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key=?", (key,))

    # events ---------------------------------------------------------------
    def log(self, kind: str, purchase_id: str | None = None, **detail) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, purchase_id, kind, detail) VALUES(?, ?, ?, ?)",
                (self.now(), purchase_id, kind, json.dumps(detail, default=str)),
            )

    def events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r, detail=json.loads(r["detail"] or "{}")) for r in rows]

    # purchases ------------------------------------------------------------
    def create_purchase(self, **fields) -> dict:
        pid = "p_" + uuid.uuid4().hex[:10]
        ts = self.now()
        row = {k: None for k in PURCHASE_FIELDS}
        row.update(fields)
        row.update(id=pid, created_at=ts, updated_at=ts)
        row["decision_reasons"] = json.dumps(row.get("decision_reasons") or [])
        for k in ("unit_price", "total"):
            row[k] = fmt(row[k])
        cols = ",".join(PURCHASE_FIELDS)
        marks = ",".join("?" for _ in PURCHASE_FIELDS)
        with self._lock:
            self._conn.execute(f"INSERT INTO purchases({cols}) VALUES({marks})",
                               [row[k] for k in PURCHASE_FIELDS])
        return self.get_purchase(pid)

    def update_purchase(self, pid: str, **fields) -> dict:
        if "decision_reasons" in fields:
            fields["decision_reasons"] = json.dumps(fields["decision_reasons"] or [])
        fields["updated_at"] = self.now()
        bad = set(fields) - set(PURCHASE_FIELDS)
        if bad:
            raise KeyError(f"unknown purchase fields: {sorted(bad)}")
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE purchases SET {sets} WHERE id=?", [*fields.values(), pid])
        return self.get_purchase(pid)

    def transition(self, pid: str, from_statuses: tuple[str, ...], **fields) -> dict | None:
        """Compare-and-set on status, so two callers cannot both act on one purchase."""
        if "decision_reasons" in fields and not isinstance(fields["decision_reasons"], str):
            fields["decision_reasons"] = json.dumps(fields["decision_reasons"] or [])
        fields["updated_at"] = self.now()
        sets = ",".join(f"{k}=?" for k in fields)
        marks = ",".join("?" for _ in from_statuses)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE purchases SET {sets} WHERE id=? AND status IN ({marks})",
                [*fields.values(), pid, *from_statuses],
            )
        return self.get_purchase(pid) if cur.rowcount == 1 else None

    def get_purchase(self, pid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone()
        return _purchase(row)

    def find_by_order(self, order_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM purchases WHERE order_id=?", (order_id,)).fetchone()
        return _purchase(row)

    def find_by_capture(self, capture_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM purchases WHERE capture_id=?", (capture_id,)).fetchone()
        return _purchase(row)

    def list_purchases(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM purchases ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [_purchase(r) for r in rows]

    def sum_total(self, statuses: tuple[str, ...], since: float = 0.0) -> Decimal:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT total FROM purchases WHERE status IN ({marks}) AND created_at >= ?",
                [*statuses, since],
            ).fetchall()
        return sum((money(r["total"]) for r in rows), Decimal("0.00"))

    def count_since(self, statuses: tuple[str, ...], since: float) -> int:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM purchases WHERE status IN ({marks}) AND created_at >= ?",
                [*statuses, since],
            ).fetchone()
        return int(row["n"])

    def recent_same_product(self, product_id: str, statuses: tuple[str, ...], since: float) -> bool:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            row = self._conn.execute(
                f"SELECT 1 FROM purchases WHERE product_id=? AND status IN ({marks}) "
                "AND created_at >= ? LIMIT 1",
                [product_id, *statuses, since],
            ).fetchone()
        return row is not None

    def stale_awaiting(self, before: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM purchases WHERE status=? AND updated_at < ?", (AWAITING_APPROVAL, before)
            ).fetchall()
        return [_purchase(r) for r in rows]


def _purchase(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["decision_reasons"] = json.loads(d.get("decision_reasons") or "[]")
    return d
