"""Append-only, hash-chained audit log of every agent and gateway decision.

Concurrency: every append first takes a write lock on the single ``audit_head`` row (an UPDATE), and holds it
until the caller commits. That serialises writers across threads and processes on both SQLite and
PostgreSQL, so two requests can never claim the same sequence number or fork the chain. Callers keep
these transactions short: no network or LLM calls while the lock is held.

The head row also records the last sequence number and hash, so deleting events from the end of the log
is detected, not only edits and gaps in the middle. Someone who can rewrite the head row as well can still
truncate undetected; anchoring the head hash outside the database closes that (see ASSUMPTIONS.md).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from protocol.signing import canonical_bytes

GENESIS_HASH = "0" * 64
_HEAD_ID = 1

__all__ = ["Base", "AuditEvent", "AuditHead", "ChainVerification", "GENESIS_HASH", "append_event",
           "lock_chain", "verify_chain", "events_for_trace", "event_to_dict"]


class AuditEvent(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    ts: Mapped[str] = mapped_column(String(40))
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditHead(Base):
    """Single row: the last sequence number and hash. Doubles as the writers' lock."""

    __tablename__ = "audit_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer)
    hash: Mapped[str] = mapped_column(String(64))


@dataclass
class ChainVerification:
    ok: bool
    events_checked: int
    first_bad_seq: int | None = None
    problem: str | None = None
    head_hash: str | None = None


def _event_hash(*, seq: int, ts: str, trace_id: str, actor: str, event_type: str, payload: dict,
                prev_hash: str) -> str:
    body = {"seq": seq, "ts": ts, "trace_id": trace_id, "actor": actor, "event_type": event_type,
            "payload": payload, "prev_hash": prev_hash}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _utc_iso(now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def lock_chain(session: Session) -> tuple[int, str]:
    """Take the writers' lock (held until the caller commits or rolls back) and return (seq, hash) of the head.

    Safe to call more than once in the same transaction.
    """
    locked = session.execute(
        update(AuditHead).where(AuditHead.id == _HEAD_ID).values(seq=AuditHead.seq)
    ).rowcount
    if not locked:
        # First write ever, or a database created before the head row existed: start from the last event.
        last = session.execute(select(AuditEvent.seq, AuditEvent.hash)
                               .order_by(AuditEvent.seq.desc()).limit(1)).first()
        session.add(AuditHead(id=_HEAD_ID, seq=last.seq if last else 0, hash=last.hash if last else GENESIS_HASH))
        session.flush()
    head = session.execute(select(AuditHead.seq, AuditHead.hash).where(AuditHead.id == _HEAD_ID)).one()
    return head.seq, head.hash


def append_event(session: Session, *, trace_id: str, actor: str, event_type: str, payload: dict,
                 now: datetime | None = None) -> AuditEvent:
    """Add the next event to the chain and flush; the caller commits (which releases the lock)."""
    last_seq, prev_hash = lock_chain(session)
    seq = last_seq + 1
    ts = _utc_iso(now)
    payload_json = canonical_bytes(payload).decode("utf-8")
    # Hash the stored JSON form (dates and enums already strings), which is exactly what verify_chain reads back.
    digest = _event_hash(seq=seq, ts=ts, trace_id=trace_id, actor=actor, event_type=event_type,
                         payload=json.loads(payload_json), prev_hash=prev_hash)
    event = AuditEvent(seq=seq, ts=ts, trace_id=trace_id, actor=actor, event_type=event_type,
                       payload_json=payload_json, prev_hash=prev_hash, hash=digest)
    session.add(event)
    session.execute(update(AuditHead).where(AuditHead.id == _HEAD_ID).values(seq=seq, hash=digest))
    session.flush()
    return event


def _recomputed_hash(event: AuditEvent) -> str | None:
    try:
        payload = json.loads(event.payload_json)
    except (TypeError, ValueError):
        return None
    return _event_hash(seq=event.seq, ts=event.ts, trace_id=event.trace_id, actor=event.actor,
                       event_type=event.event_type, payload=payload, prev_hash=event.prev_hash)


def verify_chain(session: Session) -> ChainVerification:
    """Recompute every hash, check links and seq continuity from genesis, and check the end against the head."""
    session.flush()
    events = session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars()
    checked = 0
    prev_hash = GENESIS_HASH
    for event in events:
        expected_seq = checked + 1
        problem = None
        if event.seq != expected_seq:
            problem = f"seq gap: expected {expected_seq}, found {event.seq}"
        elif event.prev_hash != prev_hash:
            problem = "prev_hash does not match the previous event's hash"
        elif _recomputed_hash(event) != event.hash:
            problem = "hash does not match the event contents"
        if problem:
            return ChainVerification(ok=False, events_checked=checked, first_bad_seq=event.seq, problem=problem)
        prev_hash = event.hash
        checked += 1

    head = session.execute(select(AuditHead.seq, AuditHead.hash).where(AuditHead.id == _HEAD_ID)).first()
    if head is not None and (head.seq, head.hash) != (checked, prev_hash):
        return ChainVerification(
            ok=False, events_checked=checked, first_bad_seq=checked + 1,
            problem=f"log ends at seq {checked} but the head records seq {head.seq}: events missing from the end")
    return ChainVerification(ok=True, events_checked=checked, head_hash=prev_hash if checked else None)


def events_for_trace(session: Session, trace_id: str) -> list[AuditEvent]:
    return list(session.execute(
        select(AuditEvent).where(AuditEvent.trace_id == trace_id).order_by(AuditEvent.seq)).scalars())


def event_to_dict(event: AuditEvent) -> dict:
    return {
        "seq": event.seq,
        "ts": event.ts,
        "trace_id": event.trace_id,
        "actor": event.actor,
        "event_type": event.event_type,
        "payload": json.loads(event.payload_json),
        "prev_hash": event.prev_hash,
        "hash": event.hash,
    }
