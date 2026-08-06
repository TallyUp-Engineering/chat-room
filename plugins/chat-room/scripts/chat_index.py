"""Optional indexed store for local CLI transcripts.

Chat Room works without this module. Without it the room re-reads vendor session
files on every catalog refresh and can only search its own coordination messages.
With it, transcripts are indexed once and answered from the index, so search
reaches inside conversations and history stops costing a filesystem walk.

The store is deliberately boring: four tables, no ORM relationships that hide a
query, and one dialect-portable schema. SQLite is the default and needs nothing
installed beyond SQLAlchemy. Point ``CHAT_ROOM_DATABASE_URL`` at
``postgresql+psycopg://...`` for Postgres, where the same models gain the
trigram/GIN acceleration noted on ``search_turns``.

Backfill is incremental: a transcript is re-read only when its file size or
mtime has moved, so re-indexing a large history is close to free.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DATABASE_URL_ENV = "CHAT_ROOM_DATABASE_URL"
SCHEMA_REVISION = "0001_actor_chat_turn_server"


class Base(DeclarativeBase):
    """Declarative base.

    Columns are always named on write because several versions of Chat Room may
    share one store; a positional insert couples every writer to the exact column
    count, and the ORM removes that coupling by construction.
    """


class Actor(Base):
    """A human, an agent session, or a CLI client that appears in this project."""

    __tablename__ = "actor"
    __table_args__ = (UniqueConstraint("client", "session_id", name="uq_actor_client_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    handle: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    worktree: Mapped[Optional[str]] = mapped_column(Text, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Chat(Base):
    """One indexed vendor transcript, with what is needed to skip re-reading it."""

    __tablename__ = "chat"
    __table_args__ = (
        UniqueConstraint("client", "session_id", name="uq_chat_client_session"),
        Index("ix_chat_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text, default="")
    cwd: Mapped[Optional[str]] = mapped_column(Text, default=None)
    worktree: Mapped[Optional[str]] = mapped_column(String(256), default=None)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    actor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("actor.id", ondelete="SET NULL"), default=None)
    source_path: Mapped[str] = mapped_column(Text, default="")
    source_size: Mapped[int] = mapped_column(Integer, default=0)
    source_mtime: Mapped[float] = mapped_column(default=0.0)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Turn(Base):
    """One user or assistant message inside a chat.

    Tool calls, hidden instructions, and reasoning are never indexed — the same
    boundary the browser transcript honours.
    """

    __tablename__ = "turn"
    __table_args__ = (
        UniqueConstraint("chat_id", "ordinal", name="uq_turn_chat_ordinal"),
        Index("ix_turn_chat_ordinal", "chat_id", "ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[Optional[str]] = mapped_column(String(64), default=None)


class Server(Base):
    """A locally reachable endpoint — a room UI, or a session's wake socket.

    Recorded so a later run can tell a live endpoint from one left behind by a
    process that died, which is otherwise invisible.
    """

    __tablename__ = "server"
    __table_args__ = (UniqueConstraint("endpoint", name="uq_server_endpoint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(Text)
    pid: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    worktree: Mapped[Optional[str]] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def database_url(data_dir: Path) -> str:
    configured = os.environ.get(DATABASE_URL_ENV, "").strip()
    if configured:
        return configured
    return f"sqlite+pysqlite:///{(data_dir.expanduser().resolve() / 'chat-index.sqlite3')}"


class IndexUnavailable(RuntimeError):
    """The configured store could not be reached. Callers report this, never a traceback."""


def build_engine(data_dir: Path) -> Engine:
    url = database_url(data_dir)
    # future=True is the 2.0 default; stated so the intent survives a downgrade.
    engine = create_engine(url, future=True)
    try:
        Base.metadata.create_all(engine)
    except SQLAlchemyError as error:
        raise IndexUnavailable(f"cannot reach the transcript index at {url.split('@')[-1]}: {type(error).__name__}") from error
    return engine


def index_chat(session: Session, summary: Dict[str, Any], messages: Sequence[Dict[str, str]], source: Path) -> bool:
    """Index one transcript. Returns False when the source has not moved since last time."""
    try:
        stat = source.stat()
    except OSError:
        return False
    client = str(summary.get("client") or "").lower()
    session_id = str(summary.get("id") or "")
    if not client or not session_id:
        return False
    existing = session.scalar(select(Chat).where(Chat.client == client, Chat.session_id == session_id))
    if existing and existing.source_size == stat.st_size and existing.source_mtime == stat.st_mtime:
        return False

    actor = session.scalar(select(Actor).where(Actor.client == client, Actor.session_id == session_id))
    if actor is None:
        actor = Actor(client=client, session_id=session_id, worktree=str(summary.get("worktree") or ""))
        session.add(actor)
        session.flush()
    else:
        actor.last_seen_at = datetime.now(timezone.utc)

    updated = _parse_timestamp(str(summary.get("updated_at") or ""))
    if existing is None:
        existing = Chat(client=client, session_id=session_id)
        session.add(existing)
    existing.title = str(summary.get("title") or "")
    existing.cwd = str(summary.get("cwd") or "")
    existing.worktree = str(summary.get("worktree") or "")
    existing.updated_at = updated
    existing.actor_id = actor.id
    existing.source_path = str(source)
    existing.source_size = stat.st_size
    existing.source_mtime = stat.st_mtime
    existing.turn_count = len(messages)
    existing.indexed_at = datetime.now(timezone.utc)
    session.flush()

    # Transcripts are append-mostly, but a rewrite must not leave stale turns behind.
    session.execute(delete(Turn).where(Turn.chat_id == existing.id))
    session.add_all([
        Turn(chat_id=existing.id, ordinal=ordinal, role=str(item.get("role") or ""), body=str(item.get("body") or ""), occurred_at=str(item.get("timestamp") or ""))
        for ordinal, item in enumerate(messages)
    ])
    return True


def backfill(engine: Engine, transcripts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Index every supplied transcript, skipping those whose source has not moved."""
    indexed = skipped = 0
    with Session(engine) as session:
        for item in transcripts:
            if index_chat(session, item["chat"], item["messages"], Path(item["source"])):
                indexed += 1
            else:
                skipped += 1
        session.commit()
    return {"indexed": indexed, "skipped": skipped}


def search_turns(engine: Engine, query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Find transcript turns containing `query`.

    ponytail: LIKE with a leading wildcard, which SQLite answers by scan and
    Postgres by scan without a trigram index. That is fine at one machine's
    history; add `pg_trgm` with a GIN index on `turn.body`, or a tsvector column,
    when a room's history outgrows it.
    """
    text = query.strip()
    if not text:
        return []
    pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    statement = (
        select(Turn, Chat)
        .join(Chat, Chat.id == Turn.chat_id)
        .where(Turn.body.like(pattern, escape="\\"))
        .order_by(Chat.updated_at.desc().nullslast(), Turn.ordinal)
        .limit(max(1, min(200, int(limit))))
    )
    with Session(engine) as session:
        return [
            {
                "client": chat.client, "session_id": chat.session_id, "title": chat.title,
                "worktree": chat.worktree, "role": turn.role, "ordinal": turn.ordinal,
                "body": turn.body, "occurred_at": turn.occurred_at,
            }
            for turn, chat in session.execute(statement).all()
        ]


def summary(engine: Engine) -> Dict[str, Any]:
    with Session(engine) as session:
        return {
            "actors": session.scalar(select(func.count()).select_from(Actor)) or 0,
            "chats": session.scalar(select(func.count()).select_from(Chat)) or 0,
            "turns": session.scalar(select(func.count()).select_from(Turn)) or 0,
            "servers": session.scalar(select(func.count()).select_from(Server)) or 0,
            "revision": SCHEMA_REVISION,
        }


def record_server(engine: Engine, kind: str, endpoint: str, pid: Optional[int] = None, worktree: Optional[str] = None) -> None:
    with Session(engine) as session:
        existing = session.scalar(select(Server).where(Server.endpoint == endpoint))
        if existing is None:
            session.add(Server(kind=kind, endpoint=endpoint, pid=pid, worktree=worktree))
        else:
            existing.kind, existing.pid, existing.worktree = kind, pid, worktree
            existing.last_seen_at = datetime.now(timezone.utc)
        session.commit()


def _parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
