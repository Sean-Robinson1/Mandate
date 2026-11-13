"""Buyer agent: request -> intent -> lookup -> plan -> policy -> order, approval request or refusal.

Every step is written to the hash-chained audit log under one trace id.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import catalog, config, planner, policy
from app.audit import append_event
from app.db import Order
from app.intent import IntentError, parse_intent
from app.llm import LLMClient
from app.models import Decision, IntentSpec, Mandate, Plan, PolicyContext, PolicyResult
from app.valio_client import ValioAPI

AGENT = "buyer-agent"


class RunResult(BaseModel):
    trace_id: str
    status: str                       # ORDERED | NEEDS_HUMAN | REFUSED | NEEDS_CLARIFICATION
    intent: IntentSpec | None = None
    plan: Plan | None = None
    policy: PolicyResult | None = None
    order_id: str | None = None
    receipt: str = ""


def week_spend(session: Session, mandate_id: str, now: datetime) -> float:
    monday = (now - timedelta(days=now.weekday())).date().isoformat()
    rows = session.scalars(select(Order).where(Order.mandate_id == mandate_id, Order.status == "PLACED"))
    return round(sum(o.total_net for o in rows if o.created_at[:10] >= monday), 2)


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
    order = Order(order_id=f"ORD-{trace_id[-8:].upper()}", trace_id=trace_id, mandate_id=mandate.mandate_id,
                  buyer_org=mandate.buyer_org, status="PLACED", total_net=plan.total_net,
                  created_at=now.isoformat(timespec="seconds"),
                  lines_json=json.dumps([l.model_dump(mode="json") for l in plan.lines]))
    session.add(order)
    return order


def run(text: str, mandate: Mandate, session: Session, api: ValioAPI, llm: LLMClient | None = None,
        now: datetime | None = None, trace_id: str | None = None) -> RunResult:
    now = now or datetime.now(config.HELSINKI)
    trace_id = trace_id or uuid.uuid4().hex[:12]

    def log(event_type: str, payload: dict, actor: str = AGENT):
        append_event(session, trace_id=trace_id, actor=actor, event_type=event_type, payload=payload)

    log("request.received", {"text": text, "mandate_id": mandate.mandate_id, "buyer_org": mandate.buyer_org})
    try:
        intent = parse_intent(text, now.date(), llm)
    except IntentError as e:
        log("clarification.requested", {"question": str(e)})
        session.commit()
        return RunResult(trace_id=trace_id, status="NEEDS_CLARIFICATION", receipt=f"I need to ask: {e}")
    log("intent.parsed", intent.model_dump(mode="json"))

    candidates, rejected = catalog.find_candidates(intent, api)
    log("candidates.considered", {
        "priced": [{"product_id": c.product_id, "name": c.name, "eur_per_unit": c.comparison_price,
                    "available_units": c.available_units, "available_date": str(c.available_date)}
                   for c in candidates],
        "rejected_on_requirements": [r.model_dump() for r in rejected]})

    plan = planner.build_plan(intent, candidates, now, rejected)
    log("plan.chosen", plan.model_dump(mode="json"))

    ctx = PolicyContext(now=now, week_spend_eur=week_spend(session, mandate.mandate_id, now))
    result = policy.evaluate(intent, plan, mandate, ctx)
    log("policy.evaluated", {**result.model_dump(mode="json"), "week_spend_eur": ctx.week_spend_eur},
        actor="policy-engine")

    order_id = None
    if result.decision is Decision.AUTO_APPROVE:
        order = place_order(session, trace_id, mandate, plan, now)
        order_id = order.order_id
        order.receipt = write_receipt(intent, plan, result, mandate, order_id)
        log("order.placed", {"order_id": order_id, "total_net": plan.total_net}, actor="gateway")
        status, receipt = "ORDERED", order.receipt
    else:
        receipt = write_receipt(intent, plan, result, mandate, None)
        if result.decision is Decision.NEEDS_HUMAN:
            log("approval.requested", {"approver": mandate.approver, "rules": [r.rule for r in result.reasons]})
            status = "NEEDS_HUMAN"
        else:
            log("order.refused", {"rules": [r.rule for r in result.reasons]})
            status = "REFUSED"
    session.commit()
    return RunResult(trace_id=trace_id, status=status, intent=intent, plan=plan, policy=result,
                     order_id=order_id, receipt=receipt)
