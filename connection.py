
"""
Shared database connection.

Production:
    Uses MySQL hosted on Aiven through DATABASE_URL.

Development:
    Uses a local SQLite database (dev.db) when DATABASE_URL
    is not provided.
"""
from dotenv import load_dotenv

load_dotenv()
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base

_BASE = os.path.dirname(os.path.abspath(__file__))
_IS_PROD = os.environ.get("APP_ENV", "development").lower() == "production"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_URL:
    if _IS_PROD:
        raise RuntimeError("DATABASE_URL must be set in production.")

    DATABASE_URL = "sqlite:///" + os.path.join(_BASE, "dev.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False}
    )

else:
    connect_args = {}

    # Aiven MySQL SSL configuration
    ssl_ca = os.environ.get("DB_SSL_CA")

    if ssl_ca:
        connect_args["ssl"] = {"ca": ssl_ca}
    else:
        connect_args["ssl"] = {}

    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=280,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        connect_args=connect_args,
    )

SessionLocal = scoped_session(
    sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine
    )
)

Base = declarative_base()








