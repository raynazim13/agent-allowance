"""SQLite storage: inventory, mandate, purchases and the audit log."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from app.models import Mandate

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT PRIMARY KEY, name TEXT, category TEXT, stock REAL,
    reorder_at REAL, daily_sales REAL, usual_price REAL);
CREATE TABLE IF NOT EXISTS mandate (
    id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, sku TEXT,
    supplier_id TEXT, qty INTEGER, unit_price REAL, total REAL,
    category TEXT, reason TEXT, decision TEXT, decision_reason TEXT,
    status TEXT, payment_ref TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT,
    action TEXT, detail TEXT);
"""


def _db_path() -> str:
    """Return the database path (read at call time for tests)."""
    return os.getenv("DB_PATH", str(BASE_DIR / "agent_allowance.db"))


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, always close."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    """Current UTC time as an ISO string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(name: str) -> object:
    """Load one of the JSON files from app/data."""
    with open(DATA_DIR / f"{name}.json", encoding="utf-8") as file:
        return json.load(file)


def reset_db() -> None:
    """Drop everything and reload the demo data."""
    with _conn() as conn:
        for table in ("inventory", "mandate", "purchases", "audit_log"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)
        for item in load_json("inventory"):
            conn.execute(
                "INSERT INTO inventory VALUES (?,?,?,?,?,?,?)",
                (item["sku"], item["name"], item["category"],
                 item["stock"], item["reorder_at"], item["daily_sales"],
                 item["usual_price"]),
            )
        conn.execute(
            "INSERT INTO mandate VALUES (1, ?)",
            (json.dumps(load_json("mandate")),),
        )
    log("system", "reset", "Demo data reloaded")


def init_db() -> None:
    """Create tables and seed demo data if the database is new."""
    with _conn() as conn:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    if count == 0:
        reset_db()


def get_suppliers() -> list[dict]:
    """Return the supplier catalogue (static JSON)."""
    return load_json("suppliers")  # type: ignore[return-value]


def get_inventory() -> list[dict]:
    """Return all inventory items."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM inventory ORDER BY sku")
        return [dict(row) for row in rows]


def get_item(sku: str) -> Optional[dict]:
    """Return one inventory item or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM inventory WHERE sku = ?", (sku,)
        ).fetchone()
    return dict(row) if row else None


def add_stock(sku: str, delta: float) -> None:
    """Change an item's stock by delta (never below zero)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE inventory SET stock = MAX(0, stock + ?) WHERE sku = ?",
            (delta, sku),
        )


def get_mandate() -> Mandate:
    """Return the current mandate."""
    with _conn() as conn:
        row = conn.execute("SELECT data FROM mandate WHERE id = 1").fetchone()
    return Mandate(**json.loads(row["data"]))


def save_mandate(mandate: Mandate) -> None:
    """Replace the stored mandate."""
    with _conn() as conn:
        conn.execute(
            "UPDATE mandate SET data = ? WHERE id = 1",
            (mandate.model_dump_json(),),
        )


def save_purchase(record: dict) -> int:
    """Insert a purchase row and return its id."""
    with _conn() as conn:
        cursor = conn.execute(
            "INSERT INTO purchases (created_at, sku, supplier_id, qty,"
            " unit_price, total, category, reason, decision,"
            " decision_reason, status, payment_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), record["sku"], record["supplier_id"], record["qty"],
             record["unit_price"], record["total"], record["category"],
             record["reason"], record["decision"],
             record["decision_reason"], record["status"],
             record.get("payment_ref")),
        )
        return int(cursor.lastrowid)


def get_purchase(purchase_id: int) -> Optional[dict]:
    """Return one purchase or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM purchases WHERE id = ?", (purchase_id,)
        ).fetchone()
    return dict(row) if row else None


def update_purchase(
    purchase_id: int, status: str, payment_ref: Optional[str] = None
) -> None:
    """Set a purchase's status (and payment reference if given)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE purchases SET status = ?,"
            " payment_ref = COALESCE(?, payment_ref) WHERE id = ?",
            (status, payment_ref, purchase_id),
        )


def transition_purchase(
    purchase_id: int, from_status: str, to_status: str,
    payment_ref: Optional[str] = None,
) -> bool:
    """Change status only if it is still from_status. True if changed."""
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE purchases SET status = ?,"
            " payment_ref = COALESCE(?, payment_ref)"
            " WHERE id = ? AND status = ?",
            (to_status, payment_ref, purchase_id, from_status),
        )
        return cursor.rowcount == 1


def list_purchases(status: Optional[str] = None) -> list[dict]:
    """Return purchases, newest first, optionally filtered by status."""
    query = "SELECT * FROM purchases"
    params: tuple = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY id DESC"
    with _conn() as conn:
        return [dict(row) for row in conn.execute(query, params)]


def log(actor: str, action: str, detail: str) -> None:
    """Append an entry to the audit log."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail)"
            " VALUES (?,?,?,?)",
            (_now(), actor, action, detail),
        )


def list_log(limit: int = 50) -> list[dict]:
    """Return the latest audit log entries, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]


def count_log(action: str) -> int:
    """How many audit log entries have this action."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", (action,)
        ).fetchone()
    return int(row[0])
