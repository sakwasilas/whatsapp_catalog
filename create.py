"""Create the database (MySQL) and tables, and optionally the first admin.

    python create.py
Optional first admin:
    export ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='long-password' ADMIN_NAME='Your Name'
    python create.py
"""
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from werkzeug.security import generate_password_hash

from connection import Base, DATABASE_URL, SessionLocal, engine
import models  # noqa: F401


def ensure_database_exists():
    url = make_url(DATABASE_URL)
    if url.get_backend_name() == "sqlite":
        print(f"[OK] Using SQLite file {url.database}")
        return
    server = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    try:
        with server.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{url.database}` "
                              "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        print(f"[OK] Database '{url.database}' is ready.")
    finally:
        server.dispose()


def create_admin_from_env():
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or not password:
        print("[INFO] ADMIN_EMAIL / ADMIN_PASSWORD not set - no admin created.")
        return
    if len(password) < 8:
        print("[ERROR] ADMIN_PASSWORD must be at least 8 characters.")
        return
    db = SessionLocal()
    try:
        if db.query(models.User).filter(models.User.email == email).first():
            print(f"[INFO] A user with email {email} already exists - nothing changed.")
            return
        db.add(models.User(full_name=os.environ.get("ADMIN_NAME", "Administrator"), email=email, phone=None,
                           password_hash=generate_password_hash(password), role="admin",
                           is_active=True, email_verified=True))
        db.commit()
        print(f"[OK] Admin account created for {email}.")
    except Exception as exc:
        db.rollback()
        print(f"[ERROR] Could not create admin: {exc}")
    finally:
        SessionLocal.remove()


if __name__ == "__main__":
    try:
        ensure_database_exists()
        Base.metadata.create_all(bind=engine)
        print("[OK] Tables: " + ", ".join(sorted(Base.metadata.tables)))
        create_admin_from_env()
        print("[DONE] Setup complete.")
    except Exception as exc:
        print(f"[ERROR] Setup failed: {exc}")
        sys.exit(1)
