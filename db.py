"""Database setup (SQLAlchemy 2.x).

SQLite file next to the app for local use; Postgres when DATABASE_URL is set
(Render's postgres:// URLs are rewritten for the psycopg 3 driver).

Tables: users (who may sign in), user_settings (each user's funding rules and preferences), chat_calls
(the assistant quota and token log) and nse_cache (shared market data).

The Evaluation list and the suggestions it receives are NOT stored here: they live on each user's own
computer (see static/eval-store.js). Every per-user table carries a user_id, and every query on it must
filter by that id.
"""
import datetime as dt
import os
import time
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

import config


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{(config.DATA_DIR / 'app.db').as_posix()}"
    for old in ("postgres://", "postgresql://"):
        if url.startswith(old):
            return "postgresql+psycopg://" + url[len(old):]
    return url


URL = database_url()
engine = create_engine(
    URL, pool_pre_ping=True,
    connect_args={"check_same_thread": False} if URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_sub: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    picture: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="user")          # "user" or "admin"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)       # bump to sign this user out everywhere
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSettings(Base):
    """One row per user: funding rules (null = defaults) and small UI preferences."""
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    funding_rules: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    prefs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ChatCall(Base):
    """One accepted chat question, for per-user limits and token/cost tracking."""
    __tablename__ = "chat_calls"
    __table_args__ = (Index("ix_chat_calls_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class NseCacheEntry(Base):
    """Shared NSE market-data cache (same for every user). See nse_cache.py."""
    __tablename__ = "nse_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool: Mapped[str] = mapped_column(String(40), index=True)
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    stale_until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)


def init_db() -> None:
    """Create missing tables. Two instances booting for the first time at once can race; the retry covers it.
    Replace with Alembic migrations when a table's columns need to change."""
    for attempt in (1, 2, 3):
        try:
            Base.metadata.create_all(engine)
            return
        except (ProgrammingError, IntegrityError, OperationalError):
            if attempt == 3:
                raise
            time.sleep(1.0 * attempt)
