"""The restaurant's purchasing agent.

  request -> intent -> search Valio Aimo -> signed offer -> plan -> mandate check
          -> order through Valio Aimo, or a person's approval, or a refusal

The agent works with Valio Aimo only through its gateway's HTTP interface. Every step is shown in the console as
it happens (app/timeline.py) and recorded in the hash-chained audit log under one trace id.

Transactions: slow steps (LLM, network) hold no database lock. The decision (weekly spend, policy, reserving the
order) is one short transaction under the audit-chain lock, so concurrent requests are decided one at a time.
The order is then sent to Valio Aimo with no lock held; the reservation already counts towards the week's spend
and is released if Valio Aimo refuses.
"""
from __future__ import annotations

import hashlib
import time
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

import settings as config
from app import keys, kitchen, menu, planner, policy
from app.mandates import DEMO_MANDATES
from app.audit import append_event, lock_chain
from app.db import Order, Run
from app.gateway_client import GatewayClient, GatewayError, OrderRefusedError, UntrustedResponse
from app.intent import IntentError, parse_intent
from app.llm import LLMClient
from app.models import (Candidate, Decision, IntentSpec, Mandate, Plan, PolicyContext, PolicyReason, PolicyResult,
                        Rejection, Unit)
from app.timeline import StepLog
from protocol.signing import canonical_bytes
from protocol.wire import Offer, OrderConfirmation, OrderLine, OrderRequest, SearchResult

AGENT = "buyer-agent"


class RunResult(BaseModel):
    trace_id: str
    status: str                       # ORDERED | NEEDS_HUMAN | REFUSED | NEEDS_CLARIFICATION | FAILED
    intent: IntentSpec | None = None
    plan: Plan | None = None
    policy: PolicyResult | None = None
    order_id: str | None = None       # Valio Aimo's order id
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


# ---------------------------------------------------------------- helpers

def _local(now: datetime) -> datetime:
    """Business clock: Helsinki. Naive datetimes are taken to be Helsinki time already."""
    return now.astimezone(config.HELSINKI) if now.tzinfo else now


def week_spend(session: Session, mandate_id: str, now: datetime) -> float:
    """Orders reserved or placed under this mandate since Monday 00:00 of the current week (Helsinki time)."""
    local = _local(now)
    monday = (local - timedelta(days=local.weekday())).date().isoformat()
    total = session.scalar(select(func.coalesce(func.sum(Order.total_net), 0.0)).where(
        Order.mandate_id == mandate_id, Order.status.in_(("RESERVED", "PLACED", "DELIVERED")),
        Order.created_at >= monday))
    return round(float(total), 2)


ORG_OF = {m.mandate_id: key for key, m in DEMO_MANDATES.items()}


def _context(session: Session, mandate: Mandate, now: datetime, review_reason: str | None = None,
             intent: IntentSpec | None = None, supply_id: int | None = None) -> PolicyContext:
    usual = None
    if intent is not None and mandate.mandate_id in ORG_OF:      # the kitchen's own usage, from its stock records
        usual = kitchen.usual_week(session, ORG_OF[mandate.mandate_id], intent.product_terms, intent.unit.value,
                                   supply_id)
    return PolicyContext(now=now, week_spend_eur=week_spend(session, mandate.mandate_id, now),
                         mandate_signature_valid=keys.verify_mandate(mandate), review_reason=review_reason,
                         usual_item=usual[0] if usual else None, usual_week_qty=usual[1] if usual else None)


def _eur(v: float) -> str:
    return f"€{v:,.2f}"


def approver_name(mandate: Mandate) -> str:
    return mandate.approver_name or mandate.approver


def to_candidates(intent: IntentSpec, found: SearchResult, offer: Offer | None) -> tuple[list[Candidate], list[Rejection]]:
    """Combine Valio Aimo's catalog facts with its offer. Litres or kg per pack come from the offer's prices."""
    rejected = [Rejection(product_id=n.product_id, name=n.name, reason=n.reason) for n in found.not_matching]
    if offer is None:
        return [], rejected
    rejected += [Rejection(product_id=n.product_id, name=n.name, reason=n.reason) for n in offer.unavailable]
    facts = {p.product_id: p for p in found.products}
    out = []
    for item in offer.items:
        p = facts.get(item.product_id)
        if p is None:
            continue
        if intent.unit in (Unit.L, Unit.KG):
            if item.comparison_unit != intent.unit.value or not item.comparison_price or item.comparison_price <= 0:
                rejected.append(Rejection(product_id=p.product_id, name=p.name,
                                          reason=f"not priced per {intent.unit.value}"))
                continue
            measure, comparison = round(item.unit_price / item.comparison_price, 2), item.comparison_price
        else:
            measure, comparison = 1.0, item.unit_price
        out.append(Candidate(
            product_id=p.product_id, name=p.name, vendor=p.vendor, brand=p.brand, gtin=p.gtin, category=p.category,
            sales_unit=item.sales_unit, unit_price=item.unit_price, unit_price_with_tax=item.unit_price_with_tax,
            measure_per_unit=measure, comparison_unit=item.comparison_unit or intent.unit.value,
            comparison_price=comparison, available_units=item.available_units,
            quantity_confirmed=item.quantity_confirmed, available_date=item.available_date,
            closing_time=item.closing_time, sales_block=item.sales_block, logistic_model=item.logistic_model,
            claims=p.claims, contains=p.contains, may_contain=p.may_contain, has_allergen_data=p.has_allergen_data,
            labels=p.labels, reliability=p.reliability, co2_per_kg=p.co2_per_kg))
    return out, rejected


def reader_name(parser: str) -> str:
    """'llm:claude_cli:haiku+rules' -> 'Claude Haiku'; 'llm:gemini:gemini-2.5-flash' -> 'Gemini'; 'rules' -> 'rules'."""
    if parser == "rules":
        return "rules"
    provider, _, model = parser.removeprefix("llm:").removesuffix("+rules").partition(":")
    if provider == "claude_cli":
        return f"Claude {model.capitalize()}" if model else "Claude"
    return provider.capitalize()


def describe_intent(intent: IntentSpec) -> str:
    what = " ".join(intent.product_terms)
    musts = [c[10:].replace("_", " ").lower() + "-free" if c.startswith("FREE_FROM_") else c.lower()
             for c in intent.required_claims]
    reader = reader_name(intent.parser)
    checked = ", confirmed by rules" if intent.parser.endswith("+rules") else ""
    return (f"{intent.quantity:,g} {intent.unit.value} of {', '.join(musts) + ' ' if musts else ''}{what}, "
            f"by {intent.deadline:%a %d %b} · read by {reader}{checked} · {intent.confidence:.0%} sure")


def describe_plan(plan: Plan) -> tuple[str, str]:
    if not plan.lines:
        return "Nothing suitable can be delivered in time", "; ".join(plan.notes[:1])
    lines = "; ".join(f"{l.sales_units:,} × {l.name}" for l in plan.lines)
    return (f"Chose {lines}",
            f"{plan.covered_quantity:,g} {plan.unit.value} for {_eur(plan.total_net)} before VAT · "
            f"delivery {plan.latest_delivery:%a %d %b}")


def describe_policy(result: PolicyResult, mandate: Mandate) -> tuple[str, str, str]:
    if result.decision is Decision.AUTO_APPROVE:
        return "Within the mandate", f"All {len(result.passed)} checks passed, so the agent may order alone", "ok"
    if result.decision is Decision.NEEDS_HUMAN:
        return (f"Needs {approver_name(mandate)}'s approval", " ".join(r.message for r in result.reasons), "warn")
    hard = [r for r in result.reasons if r.outcome is Decision.REJECT]
    return "Not allowed by the mandate", " ".join(r.message for r in hard), "bad"


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
        out.append(f"All {len(result.passed)} policy rules passed, so the agent ordered it itself"
                   + (f": Valio Aimo order {order_id}." if order_id else "."))
    elif result.decision is Decision.NEEDS_HUMAN:
        out.append(f"No order placed. Waiting for sign-off by {approver_name(mandate)}.")
    else:
        out.append("No order placed. This cannot be approved; the rule above is a hard limit.")
    return "\n".join(out)


def _reserve(session: Session, trace_id: str, mandate: Mandate, plan: Plan, now: datetime) -> Order:
    """The buyer's purchase order, counted towards the week's spend before it is sent."""
    order = Order(order_id=f"PO-{trace_id.upper()}", trace_id=trace_id, mandate_id=mandate.mandate_id,
                  buyer_org=mandate.buyer_org, status="RESERVED", total_net=plan.total_net,
                  created_at=_local(now).isoformat(timespec="seconds"),
                  lines_json=json.dumps([l.model_dump(mode="json") for l in plan.lines]))
    session.add(order)
    return order


def _signed_order(trace_id: str, mandate: Mandate, plan: Plan, offer_id: str, approval: dict | None) -> dict:
    request = OrderRequest(offer_id=offer_id, mandate_id=mandate.mandate_id, buyer_reference=trace_id,
                           lines=[OrderLine(product_id=l.product_id, sales_units=l.sales_units) for l in plan.lines],
                           approval=approval)
    return keys.sign_order(request.model_dump(mode="json", exclude={"signature", "signer_key_id"}), mandate)


SEND_ATTEMPTS = 3
SEND_PAUSE_S = (0.0, 1.0, 3.0)         # before each attempt


def _send(session: Session, gw: GatewayClient, steps: StepLog, row: Run, order: Order, mandate: Mandate, plan: Plan,
          offer_id: str, approval: dict | None) -> str:
    """Send the reserved order to Valio Aimo and record the outcome. Returns CONFIRMED, REFUSED, QUEUED or FAILED.

    The signed order is saved before it is sent. If Valio Aimo cannot be reached it is tried again; if it still
    cannot be reached the order stays reserved and queued, and resend_queued() sends the same signed order later.
    Valio Aimo answers a resend of an order it already confirmed with the same confirmation, so nothing is lost
    or ordered twice when an answer goes missing on the way back."""
    signed_order = _signed_order(row.trace_id, mandate, plan, offer_id, approval)
    order.request_json = json.dumps(signed_order)
    session.commit()
    error: Exception | None = None
    for attempt in range(SEND_ATTEMPTS):
        time.sleep(SEND_PAUSE_S[min(attempt, len(SEND_PAUSE_S) - 1)])
        try:
            confirmation, signed = gw.place_order(signed_order, mandate.model_dump(mode="json"))
            break
        except OrderRefusedError as e:
            return _refused(session, steps, row, order, e)
        except UntrustedResponse as e:
            error = e
            break
        except GatewayError as e:
            error = e
    else:
        lock_chain(session)
        append_event(session, trace_id=row.trace_id, actor="gateway-client", event_type="order.queued",
                     payload={"error": str(error)[:500], "attempts": SEND_ATTEMPTS})
        steps.agent("Valio Aimo could not be reached",
                    "The signed order is saved and will be sent again automatically until Valio Aimo answers. "
                    "It is not lost and cannot be placed twice.", "warn")
        row.receipt += "\nValio Aimo could not be reached; the order is queued and will be sent again automatically."
        session.commit()
        return "QUEUED"
    if error is not None:                                    # an answer we cannot trust: never assume it ordered
        lock_chain(session)
        order.status = "REJECTED"
        append_event(session, trace_id=row.trace_id, actor="gateway-client", event_type="order.failed",
                     payload={"error": str(error)[:500]})
        steps.agent("The order could not be placed", str(error), "bad")
        row.receipt += f"\nThe order could not be placed: {error}"
        session.commit()
        return "FAILED"
    return _confirmed(session, steps, row, order, offer_id, confirmation, signed)


def _refused(session: Session, steps: StepLog, row: Run, order: Order, e: OrderRefusedError) -> str:
    lock_chain(session)
    order.status = "REJECTED"
    failed = [c for c in e.refusal.checks if not c.ok]
    append_event(session, trace_id=row.trace_id, actor="valio-aimo", event_type="order.refused",
                 payload={"error": e.refusal.error, "failed_checks": [c.model_dump() for c in failed]})
    steps.agent("Valio Aimo refused the order", e.refusal.error, "bad")
    row.receipt += f"\nValio Aimo refused the order: {e.refusal.error}"
    session.commit()
    return "REFUSED"


def _confirmed(session: Session, steps: StepLog, row: Run, order: Order, offer_id: str,
               confirmation: OrderConfirmation, signed: dict) -> str:
    lock_chain(session)
    order.status, order.supplier_order_id = "PLACED", confirmation.order_id
    order.delivery_date, order.confirmation_json = confirmation.delivery_date.isoformat(), json.dumps(signed)
    row.order_id = confirmation.order_id
    append_event(session, trace_id=row.trace_id, actor="valio-aimo", event_type="order.confirmed",
                 payload={"order_id": confirmation.order_id, "offer_id": offer_id, "total_net": confirmation.total_net,
                          "delivery_date": confirmation.delivery_date.isoformat(),
                          "signature": confirmation.signature, "signer_key_id": confirmation.signer_key_id})
    steps.agent("Checked Valio Aimo's signature on the confirmation",
                f"Valid · order {confirmation.order_id} arrives {confirmation.delivery_date:%a %d %b}", "ok")
    session.commit()
    return "CONFIRMED"


def resend_queued(session: Session, gateway: GatewayClient, mandates: dict[str, Mandate]) -> list[str]:
    """Send every queued order again (the same signed order). Returns the trace ids that got an answer.

    No lock is held during the call to Valio Aimo; each outcome is written in its own short transaction."""
    done = []
    queued = session.scalars(select(Run).where(Run.status == "QUEUED")).all()
    for row in queued:
        order = session.scalar(select(Order).where(Order.trace_id == row.trace_id, Order.status == "RESERVED"))
        mandate = mandates.get(row.mandate_id)
        if order is None or not order.request_json or mandate is None:
            continue
        steps = StepLog(session, row.trace_id)
        gw = gateway.recording(steps.exchange)
        signed_order = json.loads(order.request_json)
        try:
            confirmation, signed = gw.place_order(signed_order, mandate.model_dump(mode="json"))
        except OrderRefusedError as e:
            _refused(session, steps, row, order, e)
            row.status = "REFUSED"
        except UntrustedResponse:
            continue                                   # try again later; never assume it ordered
        except GatewayError:
            continue
        else:
            _confirmed(session, steps, row, order, signed_order["offer_id"], confirmation, signed)
            row.status = "APPROVED" if row.approval_json else "ORDERED"
            row.receipt += f"\nSent again: Valio Aimo confirmed order {row.order_id}."
        session.commit()
        done.append(row.trace_id)
    return done


# ---------------------------------------------------------------- run

NOT_ORDERED = ("REFUSED", "FAILED", "EXPIRED", "DECLINED")


def retry_blocker(session: Session, row: Run) -> str | None:
    """Why this request cannot be tried again now, or None when it can.

    A retry is a new request with the same words, checked from scratch. It is held back while the same stock item
    already has a request in progress, so trying again never orders an item twice.
    """
    if row.status not in NOT_ORDERED:
        return "Only a request that was not ordered can be tried again."
    if row.supply_id is not None:
        busy = session.scalar(select(Run.trace_id).where(Run.supply_id == row.supply_id, Run.status.in_(kitchen.OPEN),
                                                         Run.trace_id != row.trace_id))
        if busy:
            return "This item already has a request in progress. Open it from the stock board instead."
    return None


def open_run(session: Session, text: str, mandate: Mandate, now: datetime | None = None,
             requested_by: str = "", supply_id: int | None = None, trigger: str = "") -> str:
    """Create the run so the console can show it at once; run() then does the work."""
    now = now or datetime.now(config.HELSINKI)
    trace_id = uuid.uuid4().hex[:12]
    session.add(Run(trace_id=trace_id, created_at=_local(now).isoformat(timespec="seconds"), request_text=text,
                    mandate_id=mandate.mandate_id, buyer_org=mandate.buyer_org, status="WORKING",
                    requested_by=requested_by, supply_id=supply_id, trigger=trigger))
    session.commit()
    return trace_id


def run(text: str, mandate: Mandate, session: Session, gateway: GatewayClient, llm: LLMClient | None = None,
        now: datetime | None = None, trace_id: str | None = None, review_reason: str | None = None) -> RunResult:
    """review_reason: set when the buyer asked to approve this kind of order first (an 'ask first' supply)."""
    now = now or datetime.now(config.HELSINKI)
    trace_id = trace_id or open_run(session, text, mandate, now)
    row = session.get(Run, trace_id)
    trail = _Trail(trace_id)
    steps = StepLog(session, trace_id)
    gw = gateway.recording(steps.exchange)

    def finish(status: str, receipt: str) -> None:
        """Write the outcome and the buffered audit events. The chain lock is already held."""
        row.status, row.receipt = status, receipt
        trail.flush(session)
        session.commit()

    trail.log("request.received", {"text": text, "mandate_id": mandate.mandate_id, "buyer_org": mandate.buyer_org})

    # ---- slow steps: no lock held
    try:
        intent = parse_intent(text, now.date(), llm)
    except IntentError as e:
        trail.log("clarification.requested", {"question": str(e)})
        steps.agent("Needs more detail before ordering", str(e), "warn")
        lock_chain(session)
        finish("NEEDS_CLARIFICATION", str(e))
        return RunResult(trace_id=trace_id, status="NEEDS_CLARIFICATION", receipt=f"I need to ask: {e}")
    trail.log("intent.parsed", intent.model_dump(mode="json"))
    row.intent_json = intent.model_dump_json()
    if row.supply_id is None and mandate.mandate_id in ORG_OF:      # a typed request: link it to the kitchen's item
        item = kitchen.match_supply(session, ORG_OF[mandate.mandate_id], intent.product_terms, intent.unit.value)
        row.supply_id = item.id if item else None
    steps.agent("Understood the request", describe_intent(intent),
                "warn" if intent.confidence < mandate.bands.min_intent_confidence else "")

    try:
        gw.public_key()
        found = gw.search(intent.product_terms, intent.category, intent.exclude_terms, intent.required_claims,
                          intent.excluded_allergens)
        offer, signed_offer = (gw.offer([p.product_id for p in found.products], intent.deadline, mandate.buyer_org)
                               if found.products else (None, None))
    except (GatewayError, UntrustedResponse) as e:
        trail.log("lookup.failed", {"error": f"{type(e).__name__}: {e}"[:500]}, actor="gateway-client")
        steps.agent("Could not get an offer from Valio Aimo", str(e), "bad")
        lock_chain(session)
        finish("FAILED", f"The agent could not get a trustworthy offer from Valio Aimo ({e}), so it stopped.")
        return RunResult(trace_id=trace_id, status="FAILED", intent=intent, receipt=row.receipt)
    if offer is not None:
        steps.agent("Checked Valio Aimo's signature on the offer",
                    f"Valid · prices fixed until {_local(offer.expires_at):%H:%M}", "ok")
        row.offer_json = json.dumps(signed_offer)
    trail.log("offer.received", {
        "offer_id": offer.offer_id if offer else None, "signature_verified": offer is not None,
        "expires_at": offer.expires_at.isoformat() if offer else None,
        "products_matching": len(found.products), "ruled_out": [n.model_dump() for n in found.not_matching]},
        actor="gateway-client")

    candidates, rejected = to_candidates(intent, found, offer)
    plan = planner.build_plan(intent, candidates, now, rejected)
    if found.not_returned:
        plan.notes.append(f"{found.not_returned} more matching products were not returned by the search; "
                          "a cheaper one may exist among them.")
    trail.log("plan.chosen", plan.model_dump(mode="json"))
    steps.agent(*describe_plan(plan))

    # ---- decision: one short transaction under the chain lock
    lock_chain(session)
    with steps.held():
        if offer is not None and row.supply_id is not None:     # keep what Valio Aimo charged, for Menu & margins
            menu.record_offer_prices(session, row.supply_id, kitchen.today(session),
                                     [i.model_dump(mode="json") for i in offer.items], {l.product_id for l in plan.lines})
        ctx = _context(session, mandate, now, review_reason, intent, row.supply_id)
        result = policy.evaluate(intent, plan, mandate, ctx)
        trail.log("policy.evaluated", {**result.model_dump(mode="json"), "week_spend_eur": ctx.week_spend_eur},
                  actor="policy-engine")
        title, detail, tone = describe_policy(result, mandate)
        steps.agent(title, detail, tone)
        row.total_net = plan.total_net
        row.plan_json, row.policy_json = plan.model_dump_json(), result.model_dump_json()
        receipt = write_receipt(intent, plan, result, mandate, None)
        if result.decision is Decision.NEEDS_HUMAN:
            trail.log("approval.requested", {"approver": mandate.approver, "rules": [r.rule for r in result.reasons]})
            steps.person(f"Waiting for {approver_name(mandate)}", "Nothing is ordered until they decide.", "warn")
            finish("NEEDS_HUMAN", receipt)
            return RunResult(trace_id=trace_id, status="NEEDS_HUMAN", intent=intent, plan=plan, policy=result,
                             receipt=receipt)
        if result.decision is Decision.REJECT:
            trail.log("order.refused", {"rules": [r.rule for r in result.reasons]})
            finish("REFUSED", receipt)
            return RunResult(trace_id=trace_id, status="REFUSED", intent=intent, plan=plan, policy=result,
                             receipt=receipt)
        order = _reserve(session, trace_id, mandate, plan, now)
        trail.log("order.reserved", {"po": order.order_id, "total_net": plan.total_net})
        finish("WORKING", receipt)

    # ---- place the order with Valio Aimo: no lock held
    outcome = _send(session, gw, steps, row, order, mandate, plan, offer.offer_id, approval=None)
    status = {"CONFIRMED": "ORDERED", "REFUSED": "REFUSED", "FAILED": "FAILED", "QUEUED": "QUEUED"}[outcome]
    row.status = status
    if outcome == "CONFIRMED":
        row.receipt = write_receipt(intent, plan, result, mandate, row.order_id)
    session.commit()
    return RunResult(trace_id=trace_id, status=status, intent=intent, plan=plan, policy=result,
                     order_id=row.order_id, receipt=row.receipt)


# ---------------------------------------------------------------- decide

def _fingerprint(reason: PolicyReason | dict) -> str:
    """What the approver was shown for one rule: the rule, its outcome and its figures."""
    r = reason.model_dump(mode="json") if isinstance(reason, PolicyReason) else reason
    return canonical_bytes({"rule": r["rule"], "outcome": r["outcome"], "details": r["details"]}).decode()


def _fresh_offer_if_expired(gw: GatewayClient, steps: StepLog, row: Run, plan: Plan, intent: IntentSpec,
                            mandate: Mandate, now: datetime) -> tuple[str | None, str]:
    """The offer id to order against. An expired offer is renewed; any change in price or stock is reported."""
    offer = Offer.model_validate_json(row.offer_json)
    if now < offer.expires_at - timedelta(seconds=30):
        return offer.offer_id, ""
    fresh, signed = gw.offer([l.product_id for l in plan.lines], intent.deadline, mandate.buyer_org)
    items = {i.product_id: i for i in fresh.items}
    changes = []
    for line in plan.lines:
        item = items.get(line.product_id)
        if item is None:
            changes.append(f"{line.name} is no longer offered")
        elif abs(item.unit_price - line.unit_price) > 0.0001:
            changes.append(f"{line.name} now costs {_eur(item.unit_price)} instead of {_eur(line.unit_price)}")
        elif item.available_units < line.sales_units or not item.available_date or item.available_date > intent.deadline:
            changes.append(f"{line.name} can no longer be delivered in that quantity by {intent.deadline:%a %d %b}")
    if changes:
        return None, "; ".join(changes)
    row.offer_json = json.dumps(signed)
    steps.agent("The offer had expired, so the agent asked for a new one",
                f"Same prices and stock · new offer {fresh.offer_id}, fixed until {_local(fresh.expires_at):%H:%M}", "ok")
    return fresh.offer_id, ""


def decide(session: Session, trace_id: str, mandate: Mandate, approve: bool, gateway: GatewayClient,
           note: str = "", now: datetime | None = None) -> Run:
    """A person signs off or declines a pending request.

    The policy is re-evaluated at sign-off, because time has passed and other orders may have been placed:
      - a hard rule now failing (e.g. the order cut-off passed) refuses the order; approval cannot override it;
      - a reason the approver was not shown (e.g. the weekly limit is now exceeded) sends the request back
        for review instead of ordering, and nothing is signed.
    If Valio Aimo's offer expired meanwhile, a new one is requested; changed prices or stock also send it back.
    A request can be decided once: a second, concurrent decision gets a ValueError.
    """
    now = now or datetime.now(config.HELSINKI)
    claimed = session.execute(update(Run).where(Run.trace_id == trace_id, Run.status == "NEEDS_HUMAN")
                              .values(status="DECIDING")).rowcount
    session.commit()
    if claimed != 1:
        raise ValueError("this request is not waiting for approval")
    row = session.get(Run, trace_id, populate_existing=True)
    intent = IntentSpec.model_validate_json(row.intent_json)
    plan = Plan.model_validate_json(row.plan_json)
    shown = json.loads(row.policy_json)["reasons"]
    who = approver_name(mandate)
    stamp = f"{_local(now):%d %b %Y %H:%M}"
    steps = StepLog(session, trace_id)
    gw = gateway.recording(steps.exchange)

    def sign(decision: str) -> dict:
        record = keys.sign_approval({
            "trace_id": trace_id, "mandate_id": mandate.mandate_id, "approver": mandate.approver,
            "approver_name": who, "decision": decision, "decided_at": _local(now).isoformat(timespec="seconds"),
            "lines": [{"product_id": l.product_id, "sales_units": l.sales_units} for l in plan.lines],
            "total_net": plan.total_net, "rules_signed_off": [r["rule"] for r in shown], "note": note,
            "plan_sha256": hashlib.sha256(row.plan_json.encode()).hexdigest(),
        }, mandate)
        row.approval_json = json.dumps(record)
        append_event(session, trace_id=trace_id, actor=f"human:{mandate.approver}", event_type="approval.decided",
                     payload=record)
        return record

    def reopen(reason_text: str, payload: dict) -> Run:
        row.status = "NEEDS_HUMAN"
        row.receipt += f"\nApproval by {who} on {stamp} not applied: {reason_text}"
        append_event(session, trace_id=trace_id, actor="policy-engine", event_type="approval.reopened",
                     payload={"attempted_by": mandate.approver, **payload})
        steps.agent("Sent back for review: something changed", reason_text, "warn")
        session.commit()
        return row

    lock_chain(session)
    with steps.held():
        if not approve:
            sign("DECLINED")
            row.status = "DECLINED"
            row.receipt += f"\nDeclined by {who} on {stamp}." + (f" Note: {note}" if note else "")
            steps.person(f"Declined by {who}", note or "Nothing was ordered.", "bad")
            session.commit()
            return row

        recheck = policy.evaluate(intent, plan, mandate, _context(session, mandate, now, None, intent, row.supply_id))
        hard = [r for r in recheck.reasons if r.outcome is Decision.REJECT]
        seen = {_fingerprint(r) for r in shown}
        unseen = [r for r in recheck.reasons if r.outcome is Decision.NEEDS_HUMAN and _fingerprint(r) not in seen]
        if unseen and not hard:
            row.policy_json = recheck.model_dump_json()
            return reopen("the situation changed since it was sent for approval. New: "
                          + "; ".join(r.message for r in unseen),
                          {"rules": [r.rule for r in unseen],
                           "reasons": [r.model_dump(mode="json") for r in recheck.reasons]})

        approval = sign("APPROVED")             # the approver signs off exactly the lines and reasons they were shown
        steps.person(f"Approved by {who}", (note + " · " if note else "") + f"Signed with {who}'s key", "ok")
        if hard:
            row.status = "REFUSED"
            row.receipt += "\nApproved, but refused at sign-off: " + "; ".join(r.message for r in hard)
            append_event(session, trace_id=trace_id, actor="policy-engine", event_type="order.refused",
                         payload={"rules": [r.rule for r in hard], "at": "sign-off"})
            steps.agent("Refused at sign-off", " ".join(r.message for r in hard), "bad")
            session.commit()
            return row
        order = _reserve(session, trace_id, mandate, plan, now)
        session.commit()

    # ---- network: no lock held
    try:
        offer_id, changed = _fresh_offer_if_expired(gw, steps, row, plan, intent, mandate, now)
    except (GatewayError, UntrustedResponse) as e:
        lock_chain(session)
        session.delete(order)
        row.approval_json = ""
        return reopen(f"Valio Aimo could not be reached to renew the expired offer ({e}). Nothing was ordered; "
                      "try again.", {"error": str(e)[:300]})
    if offer_id is None:
        # The approved plan no longer matches what Valio Aimo offers: it cannot be ordered as approved.
        lock_chain(session)
        session.delete(order)
        row.status = "EXPIRED"
        row.receipt += f"\nApproved by {who} on {stamp}, but Valio Aimo's offer changed: {changed}. Nothing was ordered."
        append_event(session, trace_id=trace_id, actor="gateway-client", event_type="offer.changed",
                     payload={"changes": changed})
        steps.agent("Valio Aimo's offer changed after approval", f"{changed}. Nothing was ordered; ask again for a "
                    "new plan.", "bad")
        session.commit()
        return row

    outcome = _send(session, gw, steps, row, order, mandate, plan, offer_id, approval)
    if outcome == "CONFIRMED":
        row.status = "APPROVED"
        row.receipt += (f"\nApproved by {who} on {stamp}; Valio Aimo order {row.order_id}."
                        + (f" Note: {note}" if note else ""))
        order.receipt = row.receipt
    else:
        row.status = {"REFUSED": "REFUSED", "QUEUED": "QUEUED"}.get(outcome, "FAILED")
    session.commit()
    return row
