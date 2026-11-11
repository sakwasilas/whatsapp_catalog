"""Upgrade an EXISTING v1 database to the v2 schema (safe to run more than once).

    python upgrade.py

Adds the new tables (events, rate_events), the new columns, and replaces the old
unused `orders` table (it held no data in v1) with the new order-tracking table.
"""
from sqlalchemy import inspect, text

from connection import Base, engine
import models  # noqa: F401

NEW_COLUMNS = {
    "users": [("email_verified", "BOOLEAN NOT NULL DEFAULT 0"), ("last_login_at", "DATETIME NULL")],
    "businesses": [("instagram", "VARCHAR(60) NULL"), ("theme", "VARCHAR(12) NOT NULL DEFAULT 'green'")],
    "products": [("compare_price", "NUMERIC(12,2) NULL"), ("is_featured", "BOOLEAN NOT NULL DEFAULT 0")],
    "subscriptions": [("source", "VARCHAR(10) NOT NULL DEFAULT 'payment'")],
    "payments": [("note", "VARCHAR(255) NULL"), ("reviewed_by", "INTEGER NULL")],
}


def main():
    insp = inspect(engine)
    with engine.begin() as conn:
        if "orders" in insp.get_table_names() and "product_name" not in [c["name"] for c in insp.get_columns("orders")]:
            n = conn.execute(text("SELECT COUNT(*) FROM orders")).scalar()
            if n:
                raise SystemExit(f"Old 'orders' table has {n} rows; migrate them manually first.")
            conn.execute(text("DROP TABLE orders"))
            print("[OK] Dropped empty legacy orders table")
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in NEW_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    print(f"[OK] Added {table}.{name}")
        # existing accounts predate verification: treat them as verified
        conn.execute(text("UPDATE users SET email_verified = 1 WHERE email_verified = 0 AND created_at < CURRENT_TIMESTAMP"))
    print("[DONE] Upgrade complete.")


if __name__ == "__main__":
    main()
