"""Buyer agent: request -> intent -> lookup -> plan -> policy -> order, approval request or refusal.

Every step is written to the hash-chained audit log under one trace id.

Transactions: the slow steps (LLM, Valio Aimo API, planning) touch no database rows. Their audit events are
buffered with the time they happened. The decision itself (weekly spend, policy, order) and all the events
are then written in one short transaction holding the audit-chain lock, so concurrent requests are decided
one at a time against an up-to-date weekly spend, and a crash part-way leaves nothing half-written.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import catalog, config, planner, policy, trust
from app.audit import append_event, lock_chain
from app.db import Order, Run
from app.intent import IntentError, parse_intent
from app.llm import LLMClient
from app.models import Decision, IntentSpec, Mandate, Plan, PolicyContext, PolicyReason, PolicyResult
from app.signing import canonical_bytes
from app.valio_client import ValioAPI

AGENT = "buyer-agent"


class RunResult(BaseModel):
    trace_id: str
    status: str                       # ORDERED | NEEDS_HUMAN | REFUSED | NEEDS_CLARIFICATION | FAILED
    intent: IntentSpec | None = None
    plan: Plan | None = None
    policy: PolicyResult | None = None
    order_id: str | None = None
    receipt: str = ""


@dataclass
class _Trail:
    """Audit events buffered until the decision transaction, each stamped when it happened."""

    trace_id: str
    events: list[tuple[str, str, dict, datetime]] = field(default_factory=list)

    def log(self, event_type: str, payload: dict, actor: str = AGENT) -> None:
        self.events.append((actor, event_type, payload, datetime.now(timezone.utc)))

    def flush(self, session: Session) -> None:
        for actor, event_type, payload, at in self.events:
            append_event(session, trace_id=self.trace_id, actor=actor, event_type=event_type, payload=payload, now=at)
        self.events.clear()


def _local(now: datetime) -> datetime:
    """Business clock: Helsinki. Naive datetimes are taken to be Helsinki time already."""
    return now.astimezone(config.HELSINKI) if now.tzinfo else now


def week_spend(session: Session, mandate_id: str, now: datetime) -> float:
    """Net value of orders placed under this mandate since Monday 00:00 of the current week (Helsinki time)."""
    local = _local(now)
    monday = (local - timedelta(days=local.weekday())).date().isoformat()
    total = session.scalar(select(func.coalesce(func.sum(Order.total_net), 0.0)).where(
        Order.mandate_id == mandate_id, Order.status == "PLACED", Order.created_at >= monday))
    return round(float(total), 2)


def _context(session: Session, mandate: Mandate, now: datetime) -> PolicyContext:
    return PolicyContext(now=now, week_spend_eur=week_spend(session, mandate.mandate_id, now),
                         mandate_signature_valid=trust.verify_mandate(mandate))


def write_receipt(intent: IntentSpec, plan: Plan, result: PolicyResult, mandate: Mandate,
                  order_id: str | None) -> str:
    unit = plan.unit.value
    out = [f'Request: "{intent.raw_text}"',
           f"Understood as: {intent.quantity:g} {unit} of {' '.join(intent.product_terms)}"
           + (f" ({', '.join(intent.required_claims)})" if intent.required_claims else "")
           + f", delivered by {intent.deadline:%A %d %B %Y}. Parser: {intent.parser}, "
             f"confidence {intent.confidence:.0%}."]
    out += [f"Assumption: {a}" for a in intent.ambiguities]
    if plan.lines:
        out.append("Plan:")
        out += [f"  - {l.sales_units} x {l.sales_unit} {l.name} ({l.vendor}) = {l.measure:g} {unit} at "
                f"{l.comparison_price:.2f} EUR/{unit}, {l.line_total:.2f} EUR, delivery {l.delivery_date.isoformat()}"
                for l in plan.lines]
        out.append(f"Total: {plan.covered_quantity:g} {unit} for {plan.total_net:.2f} EUR before VAT "
                   f"({plan.total_with_tax:.2f} EUR with VAT).")
    else:
        out.append("Plan: nothing orderable meets the requirements by the deadline.")
    if plan.shortfall:
        out.append(f"Shortfall: {plan.shortfall:g} {unit} could not be covered.")
    out += [f"Why: {n}" for n in plan.notes]
    out.append(f"Looked at {len(plan.lines) + len(plan.rejected)} products; {len(plan.rejected)} not used.")
    out.append(f"Mandate: {mandate.mandate_id} ({mandate.buyer_org}). Decision: {result.decision.value}.")
    out += [f"  - [{r.rule}] {r.message}" for r in result.reasons]
    if result.decision is Decision.AUTO_APPROVE:
        out.append(f"All {len(result.passed)} policy rules passed, so the agent placed order {order_id} itself.")
    elif result.decision is Decision.NEEDS_HUMAN:
        out.append(f"No order placed. Waiting for sign-off by {mandate.approver}.")
    else:
        out.append("No order placed. This cannot be approved; the rule above is a hard limit.")
    return "\n".join(out)


def place_order(session: Session, trace_id: str, mandate: Mandate, plan: Plan, now: datetime) -> Order:
    """Local mock: the Valio Aimo mock API has no order endpoint (see ASSUMPTIONS.md)."""
    order = Order(order_id=f"ORD-{trace_id.upper()}", trace_id=trace_id, mandate_id=mandate.mandate_id,
                  buyer_org=mandate.buyer_org, status="PLACED", total_net=plan.total_net,
                  created_at=_local(now).isoformat(timespec="seconds"),
                  lines_json=json.dumps([l.model_dump(mode="json") for l in plan.lines]))
    session.add(order)
    return order


def run(text: str, mandate: Mandate, session: Session, api: ValioAPI, llm: LLMClient | None = None,
        now: datetime | None = None, trace_id: str | None = None) -> RunResult:
    now = now or datetime.now(config.HELSINKI)
    trace_id = trace_id or uuid.uuid4().hex[:12]
    trail = _Trail(trace_id)
    row = Run(trace_id=trace_id, created_at=_local(now).isoformat(timespec="seconds"), request_text=text,
              mandate_id=mandate.mandate_id, buyer_org=mandate.buyer_org)

    def finish(status: str, receipt: str) -> None:
        """Write the run and its buffered events in one transaction. The chain lock is already held."""
        row.status, row.receipt = status, receipt
        session.add(row)
        trail.flush(session)
        session.commit()

    trail.log("request.received", {"text": text, "mandate_id": mandate.mandate_id, "buyer_org": mandate.buyer_org})

    # ---- slow steps: no database writes
    try:
        intent = parse_intent(text, now.date(), llm)
    except IntentError as e:
        trail.log("clarification.requested", {"question": str(e)})
        lock_chain(session)
        finish("NEEDS_CLARIFICATION", str(e))
        return RunResult(trace_id=trace_id, status="NEEDS_CLARIFICATION", receipt=f"I need to ask: {e}")
    trail.log("intent.parsed", intent.model_dump(mode="json"))
    row.intent_json = intent.model_dump_json()

    try:
        candidates, rejected = catalog.find_candidates(intent, api)
    except Exception as e:                      # API down, rate-limited, or an unexpected response shape
        trail.log("lookup.failed", {"error": f"{type(e).__name__}: {e}"[:500]}, actor="gateway")
        lock_chain(session)
        finish("FAILED", "The price and availability lookup at Valio Aimo failed, so the agent stopped.")
        return RunResult(trace_id=trace_id, status="FAILED", intent=intent, receipt=row.receipt)
    trail.log("candidates.considered", {
        "priced": [{"product_id": c.product_id, "name": c.name, "eur_per_unit": c.comparison_price,
                    "available_units": c.available_units, "available_date": str(c.available_date)}
                   for c in candidates],
        "rejected_on_requirements": [r.model_dump() for r in rejected]})

    plan = planner.build_plan(intent, candidates, now, rejected)
    unpriced = sum(1 for r in rejected if r.reason.startswith(catalog.NOT_PRICED))
    if unpriced:
        plan.notes.append(f"{unpriced} more matching products were not priced (shortlist limit "
                          f"{catalog.MAX_SHORTLIST}); a cheaper one may exist among them.")
    trail.log("plan.chosen", plan.model_dump(mode="json"))

    # ---- decision: one short transaction under the chain lock
    lock_chain(session)
    ctx = _context(session, mandate, now)
    result = policy.evaluate(intent, plan, mandate, ctx)
    trail.log("policy.evaluated", {**result.model_dump(mode="json"), "week_spend_eur": ctx.week_spend_eur},
              actor="policy-engine")

    order_id = None
    if result.decision is Decision.AUTO_APPROVE:
        order = place_order(session, trace_id, mandate, plan, now)
        order_id = order.order_id
        order.receipt = write_receipt(intent, plan, result, mandate, order_id)
        trail.log("order.placed", {"order_id": order_id, "total_net": plan.total_net}, actor="gateway")
        status, receipt = "ORDERED", order.receipt
    else:
        receipt = write_receipt(intent, plan, result, mandate, None)
        if result.decision is Decision.NEEDS_HUMAN:
            trail.log("approval.requested", {"approver": mandate.approver, "rules": [r.rule for r in result.reasons]})
            status = "NEEDS_HUMAN"
        else:
            trail.log("order.refused", {"rules": [r.rule for r in result.reasons]})
            status = "REFUSED"
    row.total_net, row.order_id = plan.total_net, order_id
    row.plan_json, row.policy_json = plan.model_dump_json(), result.model_dump_json()
    finish(status, receipt)
    return RunResult(trace_id=trace_id, status=status, intent=intent, plan=plan, policy=result,
                     order_id=order_id, receipt=receipt)


def _fingerprint(reason: PolicyReason | dict) -> str:
    """What the approver was shown for one rule: the rule, its outcome and its figures."""
    r = reason.model_dump(mode="json") if isinstance(reason, PolicyReason) else reason
    return canonical_bytes({"rule": r["rule"], "outcome": r["outcome"], "details": r["details"]}).decode()


def decide(session: Session, trace_id: str, mandate: Mandate, approve: bool, note: str = "",
           now: datetime | None = None) -> Run:
    """A human signs off or declines a pending request. Produces a signed approval record.

    The policy is re-evaluated at sign-off, because time has passed and other orders may have been placed:
      - a hard rule now failing (e.g. the order cut-off passed) refuses the order; approval cannot override it;
      - a reason the approver was not shown (e.g. the weekly limit is now exceeded) sends the request back
        for review instead of ordering, and nothing is signed.
    A request can be decided once: a second, concurrent decision gets a ValueError.
    """
    now = now or datetime.now(config.HELSINKI)
    claimed = session.execute(update(Run).where(Run.trace_id == trace_id, Run.status == "NEEDS_HUMAN")
                              .values(status="DECIDING")).rowcount
    if claimed != 1:
        session.rollback()
        raise ValueError("this request is not waiting for approval")
    lock_chain(session)
    row = session.get(Run, trace_id, populate_existing=True)
    intent = IntentSpec.model_validate_json(row.intent_json)
    plan = Plan.model_validate_json(row.plan_json)
    shown = json.loads(row.policy_json)["reasons"]
    approver = mandate.approver
    stamp = f"{now:%d %b %Y %H:%M}"

    def sign(decision: str, reasons: list[dict]) -> dict:
        record = trust.sign_approval({
            "trace_id": trace_id, "mandate_id": mandate.mandate_id, "approver": approver,
            "decision": decision, "decided_at": now.isoformat(timespec="seconds"),
            "plan_sha256": hashlib.sha256(row.plan_json.encode()).hexdigest(), "total_net": plan.total_net,
            "rules_signed_off": [r["rule"] for r in reasons], "note": note,
        }, approver)
        row.approval_json = json.dumps(record)
        append_event(session, trace_id=trace_id, actor=f"human:{approver}", event_type="approval.decided",
                     payload=record)
        return record

    if not approve:
        sign("DECLINED", shown)
        row.status = "DECLINED"
        row.receipt += f"\nDeclined by {approver} on {stamp}." + (f" Note: {note}" if note else "")
        session.commit()
        return row

    recheck = policy.evaluate(intent, plan, mandate, _context(session, mandate, now))
    hard = [r for r in recheck.reasons if r.outcome is Decision.REJECT]
    seen = {_fingerprint(r) for r in shown}
    unseen = [r for r in recheck.reasons if r.outcome is Decision.NEEDS_HUMAN and _fingerprint(r) not in seen]

    if unseen and not hard:
        row.status = "NEEDS_HUMAN"
        row.policy_json = recheck.model_dump_json()
        row.receipt += (f"\nApproval by {approver} on {stamp} not applied: the situation changed since the request "
                        "was sent for approval. New: " + "; ".join(r.message for r in unseen))
        append_event(session, trace_id=trace_id, actor="policy-engine", event_type="approval.reopened",
                     payload={"attempted_by": approver, "rules": [r.rule for r in unseen],
                              "reasons": [r.model_dump(mode="json") for r in recheck.reasons]})
        session.commit()
        return row

    sign("APPROVED", shown)                     # the approver signs off exactly what they were shown
    if hard:
        row.status = "REFUSED"
        row.receipt += "\nApproved, but refused at sign-off: " + "; ".join(r.message for r in hard)
        append_event(session, trace_id=trace_id, actor="policy-engine", event_type="order.refused",
                     payload={"rules": [r.rule for r in hard], "at": "sign-off"})
        session.commit()
        return row

    order = place_order(session, trace_id, mandate, plan, now)
    row.status, row.order_id = "APPROVED", order.order_id
    row.receipt += (f"\nApproved by {approver} on {stamp}; order {order.order_id} placed."
                    + (f" Note: {note}" if note else ""))
    order.receipt = row.receipt
    append_event(session, trace_id=trace_id, actor="gateway", event_type="order.placed",
                 payload={"order_id": order.order_id, "total_net": plan.total_net, "approved_by": approver})
    session.commit()
    return row
