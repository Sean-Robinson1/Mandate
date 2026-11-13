"""End-to-end agent tests against a fake Valio Aimo API and a real SQLite file (no network, no LLM).

Prices are 1.00 EUR per litre in 1-litre packs, so an order's total in EUR equals its litres.
Hotel mandate: the agent may order up to 2,500 EUR alone, 8,000 EUR a week; 10,000 EUR per order is the cap.
"""
import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import agent, catalog, config, trust
from app.audit import AuditEvent, verify_chain
from app.db import Order, Run, init_db, make_engine
from app.mandates import DEMO_MANDATES
from app.valio_client import Money, ProductAvailability, ProductPrice, ScheduleLine

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=config.HELSINKI)        # Friday
CLOSING = datetime(2026, 11, 15, 17, 0, tzinfo=config.HELSINKI)    # Sunday 17:00 cut-off for Monday delivery
HOTEL = trust.sign_mandate(DEMO_MANDATES["hotel"])


def product(pid: str) -> dict:
    return {"id": pid, "gtin": None, "names": {"en": f"Lactose-free milk 1l {pid}"}, "brand": "Valio",
            "vendor": "VALIO OY", "category": "10400", "sales_unit": "ST", "contains": ["AM"], "may_contain": [],
            "free_from": [], "claims": ["FREE_FROM_LACTOSE"], "labels": [], "has_allergen_data": True,
            "reliability": 0.99, "co2_per_kg": None, "description_en": None}


INDEX = [product("100001"), product("100002")]


class FakeValio:
    def __init__(self, fail: bool = False, delay: float = 0.0):
        self.fail, self.delay = fail, delay

    def prices(self, ids, requested):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("Valio API /api/price failed after 3 attempts: HTTP 503")
        return {i: ProductPrice(productId=i, catalogPrice=Money(value=1.0, valueWithTax=1.14),
                                comparisonPrice=Money(value=1.0), comparisonUnit="L") for i in ids}

    def availability(self, ids, requested):
        # Cut-off at 17:00 the day before delivery (CLOSING for a Monday delivery).
        closing = datetime.combine(requested - timedelta(days=1), CLOSING.timetz())
        return {i: ProductAvailability(productId=i, closingTime=closing, logisticModel="Stock",
                                       scheduleLines=[ScheduleLine(availableAmount=100_000, availableDate=requested)])
                for i in ids}


@pytest.fixture(autouse=True)
def synthetic_catalog(monkeypatch):
    monkeypatch.setattr(catalog, "load_index", lambda: INDEX)


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def order(db, litres: int, now: datetime = NOW, api=None) -> agent.RunResult:
    with db() as s:
        return agent.run(f"I need {litres:,} litres of lactose-free milk by Monday", HOTEL, s, api or FakeValio(),
                         now=now)


def decide(db, trace_id: str, approve: bool = True, now: datetime = NOW + timedelta(minutes=5)) -> Run:
    with db() as s:
        return agent.decide(s, trace_id, HOTEL, approve=approve, now=now)


def orders(db) -> list[Order]:
    with db() as s:
        return list(s.scalars(select(Order)))


def chain_ok(db) -> bool:
    with db() as s:
        return verify_chain(s).ok


def event_types(db, trace_id: str) -> list[str]:
    with db() as s:
        return list(s.scalars(select(AuditEvent.event_type).where(AuditEvent.trace_id == trace_id)
                              .order_by(AuditEvent.seq)))


# ---------------------------------------------------------------- run

def test_within_authority_orders_automatically(db):
    r = order(db, 1000)
    assert r.status == "ORDERED" and r.plan.total_net == 1000
    assert [o.trace_id for o in orders(db)] == [r.trace_id]
    assert event_types(db, r.trace_id) == ["request.received", "intent.parsed", "candidates.considered",
                                           "plan.chosen", "policy.evaluated", "order.placed"]
    assert chain_ok(db)


def test_over_the_order_limit_waits_for_a_human(db):
    r = order(db, 3000)
    assert r.status == "NEEDS_HUMAN" and [x.rule for x in r.policy.reasons] == ["spend.per_order"]
    assert orders(db) == []


def test_unreadable_request_asks_and_is_audited(db):
    with db() as s:
        r = agent.run("Can you get us some milk?", HOTEL, s, FakeValio(), now=NOW)
    assert r.status == "NEEDS_CLARIFICATION"
    assert event_types(db, r.trace_id) == ["request.received", "clarification.requested"]
    assert chain_ok(db)


def test_lookup_failure_stops_and_is_audited(db):
    r = order(db, 1000, api=FakeValio(fail=True))
    assert r.status == "FAILED" and orders(db) == []
    assert event_types(db, r.trace_id)[-1] == "lookup.failed"
    assert chain_ok(db)


# ---------------------------------------------------------------- decide

def test_approval_places_the_order_with_a_signed_record(db):
    r = order(db, 3000)
    row = decide(db, r.trace_id)
    assert row.status == "APPROVED" and [o.trace_id for o in orders(db)] == [r.trace_id]
    record = json.loads(row.approval_json)
    assert record["decision"] == "APPROVED" and record["rules_signed_off"] == ["spend.per_order"]
    assert trust.verify_approval(record, HOTEL.approver)
    assert chain_ok(db)


def test_decline_places_nothing(db):
    r = order(db, 3000)
    assert decide(db, r.trace_id, approve=False).status == "DECLINED"
    assert orders(db) == []


def test_a_request_can_only_be_decided_once(db):
    r = order(db, 3000)
    decide(db, r.trace_id)
    with pytest.raises(ValueError):
        decide(db, r.trace_id)
    assert len(orders(db)) == 1


def test_new_reason_at_sign_off_sends_it_back_instead_of_ordering(db):
    """A (3,000) and B (6,000) each need approval for size only. Approving B first means approving A would
    take the week to 9,000, over the 8,000 weekly limit the approver of A never saw."""
    a, b = order(db, 3000), order(db, 6000)
    assert decide(db, b.trace_id).status == "APPROVED"

    row = decide(db, a.trace_id)
    assert row.status == "NEEDS_HUMAN" and row.order_id is None and row.approval_json == ""
    assert "spend.per_week" in row.policy_json
    assert event_types(db, a.trace_id)[-1] == "approval.reopened"
    assert [o.trace_id for o in orders(db)] == [b.trace_id]

    # Approving again, now having seen the weekly limit, goes through.
    assert decide(db, a.trace_id).status == "APPROVED"
    assert len(orders(db)) == 2 and chain_ok(db)


def test_hard_rule_at_sign_off_refuses_even_when_approved(db):
    r = order(db, 3000)
    row = decide(db, r.trace_id, now=CLOSING + timedelta(minutes=1))
    assert row.status == "REFUSED" and orders(db) == []
    assert event_types(db, r.trace_id)[-2:] == ["approval.decided", "order.refused"]


def test_weekly_spend_resets_on_monday(db):
    for _ in range(4):
        assert order(db, 2000).status == "ORDERED"            # 8,000 this week: at the limit
    assert order(db, 2000).status == "NEEDS_HUMAN"
    next_week = NOW + timedelta(days=3, hours=-6)               # Monday 06:00, before the new deadline
    with db() as s:
        r = agent.run("I need 2,000 litres of lactose-free milk by Wednesday", HOTEL, s, FakeValio(), now=next_week)
    assert r.status == "ORDERED"


# ---------------------------------------------------------------- concurrency

def run_concurrently(n: int, fn) -> list:
    barrier, results, errors = threading.Barrier(n), [None] * n, []

    def worker(i):
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as e:                      # noqa: BLE001  collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_requests_cannot_overspend_the_week_or_fork_the_chain(db):
    """Six simultaneous 2,000 EUR requests, each within the per-order limit: only four fit in 8,000.

    The lookup takes a second, as a real one can. No database lock may be held across it: if runs queued
    behind each other's lookups (6+ s here), a real LLM call would push them past SQLite's lock timeout.
    """
    slow = FakeValio(delay=1.0)
    started = time.monotonic()
    results, errors = run_concurrently(6, lambda i: order(db, 2000, api=slow).status)
    assert errors == []
    assert time.monotonic() - started < 4, "runs were serialised behind each other's lookups"
    assert sorted(results) == ["NEEDS_HUMAN"] * 2 + ["ORDERED"] * 4
    assert sum(o.total_net for o in orders(db)) == 8000
    with db() as s:
        seqs = list(s.scalars(select(AuditEvent.seq).order_by(AuditEvent.seq)))
    assert seqs == list(range(1, len(seqs) + 1)) and chain_ok(db)


def test_concurrent_approvals_place_one_order(db):
    r = order(db, 3000)
    results, errors = run_concurrently(4, lambda i: decide(db, r.trace_id).status)
    assert [x for x in results if x] == ["APPROVED"]
    assert len(errors) == 3 and all(isinstance(e, ValueError) for e in errors)
    assert len(orders(db)) == 1 and chain_ok(db)
