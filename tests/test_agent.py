"""End-to-end: the buyer's agent ordering through Valio Aimo's gateway (see conftest.py for the world)."""
import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import agent, keys
from app.audit import AuditEvent, verify_chain
from app.db import Order, Run, Step
from protocol.signing import verify_document
from tests.conftest import CLOSING, NOW


def order(world, litres: int, now: datetime = NOW, buyer: str = "hotel", text: str | None = None):
    text = text or f"I need {litres:,} litres of lactose-free milk by Monday"
    with world.buyer_db() as s:
        return agent.run(text, world.mandates[buyer], s, world.client, now=now)


def decide(world, trace_id: str, approve: bool = True, now: datetime = NOW + timedelta(minutes=5)) -> Run:
    world.clock.now = now
    with world.buyer_db() as s:
        return agent.decide(s, trace_id, world.mandates["hotel"], approve=approve, gateway=world.client, now=now)


def orders(world, status: str = "PLACED") -> list[Order]:
    with world.buyer_db() as s:
        return list(s.scalars(select(Order).where(Order.status == status)))


def supplier_orders(world) -> int:
    from gateway.store import OrderRecord
    with world.gateway.sessions() as s:
        return len(list(s.scalars(select(OrderRecord))))


def chain_ok(world) -> bool:
    with world.buyer_db() as s:
        return verify_chain(s).ok


def event_types(world, trace_id: str) -> list[str]:
    with world.buyer_db() as s:
        return list(s.scalars(select(AuditEvent.event_type).where(AuditEvent.trace_id == trace_id)
                              .order_by(AuditEvent.seq)))


def step_paths(world, trace_id: str) -> list[str]:
    with world.buyer_db() as s:
        return [f"{x.method} {x.path.split('?')[0]}" if x.lane == "exchange" else x.lane
                for x in s.scalars(select(Step).where(Step.trace_id == trace_id).order_by(Step.id))]


# ---------------------------------------------------------------- run

def test_within_authority_orders_through_valio_aimo(world):
    r = order(world, 1000)
    assert r.status == "ORDERED" and r.plan.total_net == 1000 and r.order_id.startswith("AIMO-")
    placed = orders(world)
    assert [o.trace_id for o in placed] == [r.trace_id] and supplier_orders(world) == 1
    confirmation = json.loads(placed[0].confirmation_json)
    assert verify_document(confirmation, world.gateway.signer.public_key_b64())
    assert [c["check"] for c in confirmation["checks"] if not c["ok"]] == []
    assert event_types(world, r.trace_id) == ["request.received", "intent.parsed", "offer.received", "plan.chosen",
                                              "policy.evaluated", "order.reserved", "order.confirmed"]
    assert [p for p in step_paths(world, r.trace_id) if p != "agent"] == [
        "GET /.well-known/jwks.json", "GET /v1/products", "POST /v1/offers", "POST /v1/orders"]
    assert chain_ok(world)


def test_over_the_order_limit_waits_for_a_person(world):
    r = order(world, 3000)
    assert r.status == "NEEDS_HUMAN" and [x.rule for x in r.policy.reasons] == ["spend.per_order"]
    assert orders(world) == [] and supplier_orders(world) == 0


def test_unreadable_request_asks_and_is_audited(world):
    with world.buyer_db() as s:
        r = agent.run("Can you get us some milk?", world.mandates["hotel"], s, world.client, now=NOW)
    assert r.status == "NEEDS_CLARIFICATION"
    assert event_types(world, r.trace_id) == ["request.received", "clarification.requested"]


def test_backend_failure_stops_cleanly(world):
    world.backend.fail = True
    r = order(world, 1000)
    assert r.status == "FAILED" and orders(world) == []
    assert event_types(world, r.trace_id)[-1] == "lookup.failed" and chain_ok(world)


def test_a_request_that_was_not_ordered_can_be_tried_again_once_nothing_is_in_progress(world):
    world.backend.fail = True
    failed = order(world, 60)
    world.backend.fail = False
    with world.buyer_db() as s:
        row = s.get(Run, failed.trace_id)
        assert agent.retry_blocker(s, row) is None
        row.supply_id = 1                                     # a restock of a stock item...
        busy = Run(trace_id="busy", created_at=NOW.isoformat(), request_text="x", mandate_id=row.mandate_id,
                   buyer_org=row.buyer_org, status="NEEDS_HUMAN", supply_id=1)
        s.add(busy)
        s.commit()
        assert "in progress" in agent.retry_blocker(s, row)  # ...is held back while another one is open
        assert agent.retry_blocker(s, busy) is not None       # and only a request that was not ordered can be retried
    assert order(world, 60).status == "ORDERED"               # the same words go through once the backend is back


def test_a_forged_offer_is_not_trusted(world, monkeypatch):
    original = world.gateway.offer

    def tampered(req):
        signed = original(req)
        signed["items"][0]["unit_price"] = 0.01           # someone in the middle lowers a price
        return signed

    monkeypatch.setattr(world.gateway, "offer", tampered)
    r = order(world, 1000)
    assert r.status == "FAILED" and "not signed by Valio Aimo" in r.receipt
    assert supplier_orders(world) == 0


def test_an_edited_mandate_is_refused_by_both_sides(world):
    """Someone raises the hotel's limit in the stored mandate file: the signature no longer matches."""
    forged = world.mandates["hotel"].model_copy(update={"limits": world.mandates["hotel"].limits.model_copy(
        update={"per_order_auto_eur": 1_000_000})})
    with world.buyer_db() as s:
        r = agent.run("I need 1,000 litres of lactose-free milk by Monday", forged, s, world.client, now=NOW)
    assert r.status == "REFUSED" and "mandate.signature" in [x.rule for x in r.policy.reasons]
    assert supplier_orders(world) == 0


# ---------------------------------------------------------------- decide

def test_approval_is_signed_and_checked_by_valio_aimo(world):
    r = order(world, 3000)
    row = decide(world, r.trace_id)
    assert row.status == "APPROVED" and supplier_orders(world) == 1
    record = json.loads(row.approval_json)
    assert record["decision"] == "APPROVED" and record["approver_name"] == "Mikko Laine"
    assert keys.verify_approval(record, world.mandates["hotel"])
    confirmation = json.loads(orders(world)[0].confirmation_json)
    assert confirmation["approved_by"] == "Mikko Laine"
    assert any(c["check"] == "authority" and "approved by Mikko Laine" in c["message"] for c in confirmation["checks"])
    assert chain_ok(world)


def test_decline_places_nothing(world):
    r = order(world, 3000)
    assert decide(world, r.trace_id, approve=False).status == "DECLINED"
    assert orders(world) == [] and supplier_orders(world) == 0


def test_a_request_can_only_be_decided_once(world):
    r = order(world, 3000)
    decide(world, r.trace_id)
    with pytest.raises(ValueError):
        decide(world, r.trace_id)
    assert supplier_orders(world) == 1


def test_new_reason_at_sign_off_sends_it_back_instead_of_ordering(world):
    """A (3,000) and B (6,000) each need approval for size only. Approving B first means approving A would
    take the week to 9,000, over the 8,000 weekly limit the approver of A never saw."""
    a, b = order(world, 3000), order(world, 6000)
    assert decide(world, b.trace_id).status == "APPROVED"
    row = decide(world, a.trace_id)
    assert row.status == "NEEDS_HUMAN" and row.order_id is None and "spend.per_week" in row.policy_json
    assert event_types(world, a.trace_id)[-1] == "approval.reopened"
    assert supplier_orders(world) == 1
    assert decide(world, a.trace_id).status == "APPROVED"       # approved again, having seen the weekly limit
    assert supplier_orders(world) == 2 and chain_ok(world)


def test_hard_rule_at_sign_off_refuses_even_when_approved(world):
    r = order(world, 3000)
    row = decide(world, r.trace_id, now=CLOSING + timedelta(minutes=1))
    assert row.status == "REFUSED" and supplier_orders(world) == 0


def test_expired_offer_is_renewed_when_nothing_changed(world):
    r = order(world, 3000)
    row = decide(world, r.trace_id, now=NOW + timedelta(minutes=40))
    assert row.status == "APPROVED" and supplier_orders(world) == 1
    assert step_paths(world, r.trace_id).count("POST /v1/offers") == 2


def test_changed_price_after_approval_orders_nothing(world):
    r = order(world, 3000)
    world.backend.price = 1.10
    row = decide(world, r.trace_id, now=NOW + timedelta(minutes=40))
    assert row.status == "EXPIRED" and supplier_orders(world) == 0 and orders(world, "RESERVED") == []
    assert "now costs" in row.receipt


def test_weekly_spend_resets_on_monday(world):
    for _ in range(4):
        assert order(world, 2000).status == "ORDERED"            # 8,000 this week: at the limit
    assert order(world, 2000).status == "NEEDS_HUMAN"
    monday = NOW + timedelta(days=3, hours=-6)                  # Monday 06:00
    world.clock.now = monday
    r = order(world, 2000, now=monday, text="I need 2,000 litres of lactose-free milk by Wednesday")
    assert r.status == "ORDERED"


# ---------------------------------------------------------------- concurrency

def run_concurrently(n: int, fn):
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


def test_concurrent_requests_cannot_overspend_the_week(world):
    """Six simultaneous 2,000 EUR requests, each within the per-order limit: only four fit in 8,000.

    The backend takes half a second, as a real one can. No database lock may be held across it: runs queued
    behind each other's lookups would take 3+ s here, and a real LLM call would push them past SQLite's timeout.
    """
    world.backend.delay = 0.5
    started = time.monotonic()
    results, errors = run_concurrently(6, lambda i: order(world, 2000).status)
    assert errors == []
    assert time.monotonic() - started < 2.5, "runs were serialised behind each other's lookups"
    assert sorted(results) == ["NEEDS_HUMAN"] * 2 + ["ORDERED"] * 4
    assert supplier_orders(world) == 4 and sum(o.total_net for o in orders(world)) == 8000
    with world.buyer_db() as s:
        seqs = list(s.scalars(select(AuditEvent.seq).order_by(AuditEvent.seq)))
    assert seqs == list(range(1, len(seqs) + 1)) and chain_ok(world)


def test_concurrent_approvals_place_one_order(world):
    r = order(world, 3000)
    world.clock.now = NOW + timedelta(minutes=5)
    results, errors = run_concurrently(4, lambda i: decide(world, r.trace_id).status)
    assert [x for x in results if x] == ["APPROVED"]
    assert len(errors) == 3 and all(isinstance(e, ValueError) for e in errors)
    assert supplier_orders(world) == 1 and chain_ok(world)


def test_a_lost_confirmation_is_resent_and_ordered_exactly_once(world, monkeypatch):
    """Valio Aimo confirms, but the answer never arrives: the agent queues the order and resends it later."""
    from app.gateway_client import GatewayClient, GatewayError
    monkeypatch.setattr(agent, "SEND_PAUSE_S", (0.0,))
    real = GatewayClient.place_order

    def answer_lost(self, *args, **kwargs):
        real(self, *args, **kwargs)
        raise GatewayError("timed out waiting for Valio Aimo")

    monkeypatch.setattr(GatewayClient, "place_order", answer_lost)
    r = order(world, 1000)
    assert r.status == "QUEUED"
    assert [o.status for o in orders(world, "RESERVED")] == ["RESERVED"]      # still counted, not dropped
    assert supplier_orders(world) == 1

    monkeypatch.setattr(GatewayClient, "place_order", real)                   # Valio Aimo reachable again
    with world.buyer_db() as s:
        assert agent.resend_queued(s, world.client, {m.mandate_id: m for m in world.mandates.values()}) == [r.trace_id]
        row = s.get(Run, r.trace_id)
        assert row.status == "ORDERED" and row.order_id.startswith("AIMO-")
    assert supplier_orders(world) == 1                                          # the same order, not a second one
    assert [o.status for o in orders(world)] == ["PLACED"]
    assert chain_ok(world)


def test_an_unreachable_gateway_keeps_the_order_queued(world, monkeypatch):
    from app.gateway_client import GatewayClient, GatewayError
    monkeypatch.setattr(agent, "SEND_PAUSE_S", (0.0,))

    def down(self, *args, **kwargs):
        raise GatewayError("connection refused")

    monkeypatch.setattr(GatewayClient, "place_order", down)
    r = order(world, 1000)
    with world.buyer_db() as s:
        assert agent.resend_queued(s, world.client, {m.mandate_id: m for m in world.mandates.values()}) == []
        assert s.get(Run, r.trace_id).status == "QUEUED"
    assert supplier_orders(world) == 0
