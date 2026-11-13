from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session

from app.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditHead,
    Base,
    append_event,
    event_to_dict,
    events_for_trace,
    verify_chain,
)

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)

EVENTS = [
    ("trace-a", "buyer-agent", "intent.parsed", {"text": "20 l maitoa", "qty": 20}),
    ("trace-a", "gateway", "candidates.considered", {"skus": ["VAL-1", "VAL-2"]}),
    ("trace-b", "buyer-agent", "intent.parsed", {"text": "kermaa"}),
    ("trace-a", "policy-engine", "policy.evaluated", {"allowed": True, "total": 31.5}),
    ("trace-a", "gateway", "order.placed", {"order_id": "ORD-1"}),
]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def fill(session) -> list[AuditEvent]:
    events = [
        append_event(
            session,
            trace_id=trace_id,
            actor=actor,
            event_type=event_type,
            payload=payload,
            now=T0 + timedelta(seconds=i),
        )
        for i, (trace_id, actor, event_type, payload) in enumerate(EVENTS)
    ]
    session.commit()
    return events


def test_empty_log_verifies(session):
    result = verify_chain(session)
    assert result.ok
    assert result.events_checked == 0
    assert result.first_bad_seq is None
    assert result.problem is None
    assert result.head_hash is None


def test_chain_of_events_verifies(session):
    events = fill(session)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert events[0].prev_hash == GENESIS_HASH
    for prev, cur in zip(events, events[1:]):
        assert cur.prev_hash == prev.hash
    result = verify_chain(session)
    assert result.ok
    assert result.events_checked == 5
    assert result.first_bad_seq is None
    assert result.head_hash == events[-1].hash


def test_uncommitted_events_are_verified(session):
    append_event(session, trace_id="t", actor="gateway", event_type="x", payload={})
    append_event(session, trace_id="t", actor="gateway", event_type="y", payload={})
    assert verify_chain(session).events_checked == 2


def test_editing_payload_breaks_chain_at_that_seq(session):
    fill(session)
    session.execute(
        update(AuditEvent)
        .where(AuditEvent.seq == 3)
        .values(payload_json='{"text":"samppanjaa"}')
    )
    session.commit()
    result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 3
    assert result.events_checked == 2
    assert "hash" in result.problem


@pytest.mark.parametrize(
    "column,value",
    [
        ("actor", "human:purchasing-manager"),
        ("event_type", "order.refused"),
        ("trace_id", "trace-z"),
        ("ts", "2020-01-01T00:00:00+00:00"),
        ("payload_json", "not json"),
    ],
)
def test_editing_any_column_breaks_chain(session, column, value):
    fill(session)
    session.execute(update(AuditEvent).where(AuditEvent.seq == 2).values({column: value}))
    session.commit()
    result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 2


def test_editing_prev_hash_breaks_chain(session):
    fill(session)
    session.execute(
        update(AuditEvent).where(AuditEvent.seq == 4).values(prev_hash="f" * 64)
    )
    session.commit()
    result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 4
    assert "prev_hash" in result.problem


def test_rewriting_an_event_with_a_fresh_hash_is_detected_downstream(session):
    """An attacker who recomputes the edited row's own hash still breaks the next link."""
    fill(session)
    session.execute(update(AuditEvent).where(AuditEvent.seq == 2).values(hash="a" * 64))
    session.commit()
    result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 2


def test_deleting_a_middle_row_is_detected(session):
    fill(session)
    session.execute(delete(AuditEvent).where(AuditEvent.seq == 3))
    session.commit()
    result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 4
    assert result.events_checked == 2
    assert "gap" in result.problem


def test_deleting_the_first_row_is_detected(session):
    fill(session)
    session.execute(delete(AuditEvent).where(AuditEvent.seq == 1))
    session.commit()
    result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 2


def test_deleting_the_last_rows_is_detected(session):
    """Without the head record, a log cut short still verifies as a valid shorter chain."""
    fill(session)
    session.execute(delete(AuditEvent).where(AuditEvent.seq >= 4))
    session.commit()
    result = verify_chain(session)
    assert not result.ok
    assert result.events_checked == 3 and result.first_bad_seq == 4
    assert "missing from the end" in result.problem


def test_existing_log_without_a_head_is_adopted(session):
    """A database written before the head row existed: the first new append starts from its last event."""
    events = fill(session)
    session.execute(delete(AuditHead))
    session.commit()
    assert verify_chain(session).ok
    new = append_event(session, trace_id="t", actor="gateway", event_type="x", payload={})
    session.commit()
    assert new.seq == 6 and new.prev_hash == events[-1].hash
    assert verify_chain(session).ok


def test_events_for_trace_filters_and_orders(session):
    fill(session)
    events = events_for_trace(session, "trace-a")
    assert [e.seq for e in events] == [1, 2, 4, 5]
    assert {e.trace_id for e in events} == {"trace-a"}
    assert [e.seq for e in events_for_trace(session, "trace-b")] == [3]
    assert events_for_trace(session, "nope") == []


def test_event_to_dict(session):
    events = fill(session)
    d = event_to_dict(events[3])
    assert d == {
        "seq": 4,
        "ts": "2026-11-13T09:00:03+00:00",
        "trace_id": "trace-a",
        "actor": "policy-engine",
        "event_type": "policy.evaluated",
        "payload": {"allowed": True, "total": 31.5},
        "prev_hash": events[2].hash,
        "hash": events[3].hash,
    }


def test_hashing_is_deterministic_given_fixed_now():
    def head() -> str:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            fill(s)
            return verify_chain(s).head_hash

    first = head()
    assert first == head()
    assert len(first) == 64


def test_payload_with_dates_survives_round_trip(session):
    event = append_event(
        session,
        trace_id="t",
        actor="gateway",
        event_type="offer.issued",
        payload={"delivery_date": date(2026, 11, 14), "valid_until": T0},
        now=T0,
    )
    session.commit()
    assert event_to_dict(event)["payload"] == {
        "delivery_date": "2026-11-14",
        "valid_until": "2026-11-13T09:00:00+00:00",
    }
    assert verify_chain(session).ok


def test_ts_is_normalised_to_utc(session):
    helsinki = timezone(timedelta(hours=2))
    aware = append_event(
        session,
        trace_id="t",
        actor="gateway",
        event_type="x",
        payload={},
        now=datetime(2026, 11, 13, 11, 0, tzinfo=helsinki),
    )
    default = append_event(
        session, trace_id="t", actor="gateway", event_type="x", payload={}
    )
    assert aware.ts == "2026-11-13T09:00:00+00:00"
    assert default.ts.endswith("+00:00")
