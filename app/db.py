"""SQLite storage (session-scoped). Path from DATABASE_PATH, else DATA_DIR/watchdog.db, else ./data/watchdog.db."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, create_engine, delete, event, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def database_path() -> Path:
    if os.getenv("DATABASE_PATH"):
        return Path(os.environ["DATABASE_PATH"])
    return Path(os.getenv("DATA_DIR", "data")) / "watchdog.db"


RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
# Opt-in saved history (premium) is kept until this many days of inactivity, or until the user deletes it.
SAVED_RETENTION_DAYS = int(os.getenv("SAVED_RETENTION_DAYS", "365"))


class Base(DeclarativeBase):
    pass


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    saved: Mapped[bool] = mapped_column(Boolean, default=False)  # premium opt-in: exempt from short retention


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(200))
    amount: Mapped[float] = mapped_column(Float)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    merchant: Mapped[str] = mapped_column(String(100))
    remind_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(String(300), default="")


class Entitlement(Base):
    """A paid (or dev-simulated) unlock. Holds NO card, name or email data — only what proves the purchase."""
    __tablename__ = "entitlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    stripe_session_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)       # sha256 of the unlock token
    nonce_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)  # ties checkout to buyer's browser
    payment_intent: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)  # for refund revocation
    status: Mapped[str] = mapped_column(String(20), default="paid")       # paid | refunded
    source: Mapped[str] = mapped_column(String(10), default="stripe")     # stripe | dev
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    saved_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RecoveryEmail(Base):
    """OPTIONAL, opt-in recovery email for an unlock. We store only a keyed one-way hash (HMAC-SHA256) of the
    normalised address — never the address itself. To re-send, the buyer types their email again; we hash it,
    find the purchase and mail the link to the address they just typed. We therefore can't read, list or
    market to anyone's email, and a leaked database reveals no addresses."""
    __tablename__ = "recovery_emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    entitlement_id: Mapped[int] = mapped_column(ForeignKey("entitlements.id", ondelete="CASCADE"), unique=True, index=True)
    email_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookEvent(Base):
    """Processed Stripe event ids — makes the webhook idempotent (Stripe retries and may deliver twice)."""
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


_engine = None
SessionLocal: sessionmaker | None = None


def init_db(path: Path | None = None):
    global _engine, SessionLocal
    path = path or database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(_engine, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA secure_delete=ON")  # overwrite deleted content on disk

    Base.metadata.create_all(_engine)
    _migrate(_engine)
    SessionLocal = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _migrate(engine):
    """Tiny forward-only migration for DBs created by the pre-unlock MVP."""
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sessions)"))}
        if "saved" not in cols:
            conn.execute(text("ALTER TABLE sessions ADD COLUMN saved BOOLEAN DEFAULT 0"))


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def purge_expired(db: Session) -> int:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    saved_cutoff = now - timedelta(days=SAVED_RETENTION_DAYS)
    ids = [s.id for s in db.scalars(select(UserSession).where(
        UserSession.last_seen < cutoff, UserSession.saved.is_not(True)))]
    ids += [s.id for s in db.scalars(select(UserSession).where(
        UserSession.last_seen < saved_cutoff, UserSession.saved.is_(True)))]
    for sid in ids:
        delete_session_data(db, sid)
    return len(ids)


def delete_recovery_email(db: Session, entitlement_id: int) -> int:
    n = db.execute(delete(RecoveryEmail).where(RecoveryEmail.entitlement_id == entitlement_id)).rowcount
    db.commit()
    return n


def delete_session_data(db: Session, sid: str):
    db.execute(delete(Transaction).where(Transaction.session_id == sid))
    db.execute(delete(Reminder).where(Reminder.session_id == sid))
    db.execute(delete(UserSession).where(UserSession.id == sid))
    for e in db.scalars(select(Entitlement).where(Entitlement.saved_session_id == sid)):
        e.saved_session_id = None  # the purchase record stays; the saved history is gone
    db.commit()
