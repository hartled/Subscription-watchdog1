"""SQLite storage (session-scoped). Path from DATABASE_PATH, else DATA_DIR/watchdog.db, else ./data/watchdog.db."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, create_engine, delete, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def database_path() -> Path:
    if os.getenv("DATABASE_PATH"):
        return Path(os.environ["DATABASE_PATH"])
    return Path(os.getenv("DATA_DIR", "data")) / "watchdog.db"


RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))


class Base(DeclarativeBase):
    pass


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    SessionLocal = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def purge_expired(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    ids = [s.id for s in db.scalars(select(UserSession).where(UserSession.last_seen < cutoff))]
    for sid in ids:
        delete_session_data(db, sid)
    return len(ids)


def delete_session_data(db: Session, sid: str):
    db.execute(delete(Transaction).where(Transaction.session_id == sid))
    db.execute(delete(Reminder).where(Reminder.session_id == sid))
    db.execute(delete(UserSession).where(UserSession.id == sid))
    db.commit()
