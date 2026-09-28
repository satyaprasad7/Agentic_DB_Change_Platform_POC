"""SQLite-backed target database plus introspection helpers shared by the agents.

The "production" database is an in-memory SQLite instance seeded with a small
commerce schema. Agents never touch it during analysis: they work on clones.
"""
import hashlib
import random
import sqlite3
import threading

from faker import Faker

BACKUP_PREFIX = "_rb_"

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    country TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    active INTEGER DEFAULT 1
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'NEW',
    total REAL,
    ordered_at TEXT
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE audit_trail (
    id INTEGER PRIMARY KEY,
    entity TEXT,
    entity_id INTEGER,
    action TEXT,
    at TEXT
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE VIEW v_customer_revenue AS
    SELECT c.id, c.name, SUM(o.total) AS revenue
    FROM customers c JOIN orders o ON o.customer_id = c.id
    GROUP BY c.id;
CREATE TRIGGER trg_orders_status_audit AFTER UPDATE OF status ON orders
BEGIN
    INSERT INTO audit_trail(entity, entity_id, action, at)
    VALUES ('orders', NEW.id, 'status:' || NEW.status, CURRENT_TIMESTAMP);
END;
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    fake = Faker()
    Faker.seed(42)
    rnd = random.Random(42)
    conn.executescript(SCHEMA)
    conn.execute("BEGIN")
    for i in range(1, 201):
        conn.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?)",
            (i, fake.name(), f"user{i}@{fake.free_email_domain()}", fake.country_code(),
             fake.date_time_between("-3y", "-1y").isoformat(sep=" ", timespec="seconds")),
        )
    prices = {}
    for i in range(1, 51):
        prices[i] = round(rnd.uniform(5, 500), 2)
        conn.execute("INSERT INTO products VALUES (?,?,?,?,?)",
                     (i, f"SKU-{1000 + i}", fake.catch_phrase(), prices[i], 0 if rnd.random() < 0.2 else 1))
    item_id = 1
    for i in range(1, 1001):
        items = [(rnd.randint(1, 50), rnd.randint(1, 4)) for _ in range(rnd.randint(1, 5))]
        total = round(sum(prices[p] * q for p, q in items), 2)
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?)",
                     (i, rnd.randint(1, 200), rnd.choice(["NEW", "NEW", "PAID", "SHIPPED", "CLOSED"]), total,
                      fake.date_time_between("-2y", "now").isoformat(sep=" ", timespec="seconds")))
        for p, q in items:
            conn.execute("INSERT INTO order_items VALUES (?,?,?,?,?)", (item_id, i, p, q, prices[p]))
            item_id += 1
    conn.execute("COMMIT")


class Sandbox:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.conn = connect()
            _seed(self.conn)

    def clone(self) -> sqlite3.Connection:
        with self.lock:
            dst = connect()
            self.conn.backup(dst)
            dst.execute("PRAGMA foreign_keys = ON")
            return dst

    def restore(self, snapshot: sqlite3.Connection) -> None:
        with self.lock:
            snapshot.backup(self.conn)


# ---- introspection helpers (work on any connection) ----

def objects(conn, kind: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE ?"
    args = [BACKUP_PREFIX + "%"]
    if kind:
        sql += " AND type = ?"
        args.append(kind)
    return conn.execute(sql + " ORDER BY type, name", args).fetchall()


def exists(conn, name: str, kind: str | None = None) -> bool:
    sql = "SELECT 1 FROM sqlite_master WHERE lower(name) = lower(?)"
    args = [name]
    if kind:
        sql += " AND type = ?"
        args.append(kind)
    return conn.execute(sql, args).fetchone() is not None


def object_sql(conn, name: str) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE lower(name) = lower(?)", (name,)).fetchone()
    return row["sql"] if row else None


def columns(conn, table: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def foreign_keys(conn, table: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()]


def row_count(conn, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def table_checksum(conn, table: str) -> str:
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    canon = sorted(repr(sorted(dict(r).items())) for r in rows)
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()[:16]


def fingerprint(conn, tables: list[str]) -> dict:
    """Order-insensitive fingerprint of the schema plus data checksums for the given tables."""
    schema = {}
    for o in objects(conn):
        if o["type"] == "table":
            schema[f"table:{o['name']}"] = sorted(
                (c["name"], (c["type"] or "").upper(), c["notnull"], c["dflt_value"], c["pk"]) for c in columns(conn, o["name"])
            )
        else:
            schema[f"{o['type']}:{o['name']}"] = o["tbl_name"]
    data = {}
    for t in tables:
        if exists(conn, t, "table"):
            data[t] = {"rows": row_count(conn, t), "checksum": table_checksum(conn, t)}
    return {"schema": schema, "data": data}


def describe(conn) -> dict:
    tables = []
    for o in objects(conn, "table"):
        t = o["name"]
        tables.append({
            "name": t,
            "rows": row_count(conn, t),
            "columns": [{"name": c["name"], "type": c["type"], "notnull": bool(c["notnull"]),
                         "default": c["dflt_value"], "pk": bool(c["pk"])} for c in columns(conn, t)],
            "foreign_keys": [{"column": f["from"], "references": f"{f['table']}.{f['to']}"} for f in foreign_keys(conn, t)],
        })
    others = [{"type": o["type"], "name": o["name"], "table": o["tbl_name"], "sql": o["sql"]}
              for o in objects(conn) if o["type"] != "table" and o["sql"]]
    return {"tables": tables, "objects": others}
