"""Append-only, hash-chained audit log of every agent and gateway decision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Integer, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.signing import canonical_bytes

GENESIS_HASH = "0" * 64


from app.db import Base  # noqa: E402  shared declarative base, so one create_all covers every table


class AuditEvent(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40))
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


@dataclass
class ChainVerification:
    ok: bool
    events_checked: int
    first_bad_seq: int | None = None
    problem: str | None = None
    head_hash: str | None = None


def _event_hash(
    *,
    seq: int,
    ts: str,
    trace_id: str,
    actor: str,
    event_type: str,
    payload: dict,
    prev_hash: str,
) -> str:
    body = {
        "seq": seq,
        "ts": ts,
        "trace_id": trace_id,
        "actor": actor,
        "event_type": event_type,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _utc_iso(now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def append_event(
    session: Session,
    *,
    trace_id: str,
    actor: str,
    event_type: str,
    payload: dict,
    now: datetime | None = None,
) -> AuditEvent:
    """Add the next event to the chain and flush; the caller commits."""
    last = session.execute(
        select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
    ).scalar_one_or_none()
    seq = last.seq + 1 if last else 1
    prev_hash = last.hash if last else GENESIS_HASH
    ts = _utc_iso(now)
    payload_json = canonical_bytes(payload).decode("utf-8")
    event = AuditEvent(
        seq=seq,
        ts=ts,
        trace_id=trace_id,
        actor=actor,
        event_type=event_type,
        payload_json=payload_json,
        prev_hash=prev_hash,
        # Hash the stored JSON form (dates/enums already strings), which is
        # exactly what verify_chain will read back.
        hash=_event_hash(
            seq=seq,
            ts=ts,
            trace_id=trace_id,
            actor=actor,
            event_type=event_type,
            payload=json.loads(payload_json),
            prev_hash=prev_hash,
        ),
    )
    session.add(event)
    session.flush()
    return event


def _recomputed_hash(event: AuditEvent) -> str | None:
    try:
        payload = json.loads(event.payload_json)
    except (TypeError, ValueError):
        return None
    return _event_hash(
        seq=event.seq,
        ts=event.ts,
        trace_id=event.trace_id,
        actor=event.actor,
        event_type=event.event_type,
        payload=payload,
        prev_hash=event.prev_hash,
    )


def verify_chain(session: Session) -> ChainVerification:
    """Recompute every hash and check links and seq continuity from genesis."""
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
            return ChainVerification(
                ok=False,
                events_checked=checked,
                first_bad_seq=event.seq,
                problem=problem,
                head_hash=None,
            )
        prev_hash = event.hash
        checked += 1
    return ChainVerification(
        ok=True,
        events_checked=checked,
        head_hash=prev_hash if checked else None,
    )


def events_for_trace(session: Session, trace_id: str) -> list[AuditEvent]:
    return list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.trace_id == trace_id)
            .order_by(AuditEvent.seq)
        ).scalars()
    )


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
