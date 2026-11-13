"""Web console: submit requests, review plans, approve or decline, inspect the audit log.

Run:  .venv/Scripts/python -m uvicorn app.web:app --reload
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app import agent, config, policy, trust
from app.audit import AuditEvent, event_to_dict, events_for_trace, verify_chain
from app.db import Run, SessionLocal, init_db
from app.llm import get_llm
from app.mandates import DEMO_MANDATES
from app.valio_client import ValioClient

HERE = Path(__file__).parent
app = FastAPI(title="Trusted Trade Gateway", version="0.2.0")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

init_db()
MANDATES = {key: trust.sign_mandate(m) for key, m in DEMO_MANDATES.items()}
MANDATE_BY_ID = {m.mandate_id: m for m in MANDATES.values()}
_api = ValioClient()

STATUS = {
    "ORDERED": ("Ordered automatically", "ok"),
    "APPROVED": ("Approved and ordered", "ok"),
    "NEEDS_HUMAN": ("Waiting for approval", "warn"),
    "NEEDS_CLARIFICATION": ("Needs clarification", "warn"),
    "REFUSED": ("Refused", "bad"),
    "DECLINED": ("Declined", "bad"),
    "FAILED": ("Lookup failed", "bad"),
}

RULE_LABELS = {
    "mandate.signature": "Mandate signature is valid",
    "mandate.validity": "Mandate is in date",
    "plan.empty": "Something orderable was found",
    "hard.required_claims": "Required product claims are met",
    "hard.allergens": "No excluded allergens",
    "hard.allergen_data": "Allergen data is present",
    "category.allowed": "Product category is allowed",
    "spend.per_order": "Within the per-order limit",
    "spend.per_week": "Within the weekly limit",
    "spend.intent_budget": "Within the requested budget",
    "band.price_premium": "Price is within the agreed band",
    "preference.cost": "Preferences do not cost much extra",
    "band.overage": "Not over-ordered",
    "band.shortfall": "Full quantity is covered",
    "band.delivery": "Arrives by the deadline",
    "order.cutoff": "Order cut-off has not passed",
    "substitution.relaxed_preferences": "Preferences are respected",
    "substitution.split": "Split across products is allowed",
    "uncertainty.intent_confidence": "Request was understood with confidence",
    "uncertainty.reliability": "Delivery reliability is acceptable",
    "uncertainty.unconfirmed_quantity": "Stock quantity is confirmed",
}

EVENT_LABELS = {
    "request.received": "Request received",
    "clarification.requested": "Asked the buyer to clarify",
    "intent.parsed": "Request understood",
    "candidates.considered": "Prices and availability requested from Valio Aimo",
    "plan.chosen": "Plan built",
    "policy.evaluated": "Checked against the mandate",
    "approval.requested": "Sent for approval",
    "approval.decided": "Approver decided",
    "approval.reopened": "Sent back for review: something changed",
    "order.placed": "Order placed",
    "order.refused": "Order refused",
    "lookup.failed": "Valio Aimo lookup failed",
}

STATUS_WORD = {"AUTO_APPROVE": "Within authority", "NEEDS_HUMAN": "Needs a human", "REJECT": "Not allowed"}

ALLERGEN_NAMES = {"AN": "tree nuts", "AP": "peanuts", "AM": "milk", "AE": "egg", "AF": "fish",
                  "AW": "gluten cereals", "AY": "soy", "AS": "sesame", "AC": "crustaceans"}

EXAMPLES = [
    ("Within authority", "I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our "
                         "requirements based on price and availability."),
    ("Over the spend limit", "I need 6,000 litres of lactose-free milk by Monday."),
    ("Brand preference", "We need 600 litres of lactose-free Arla milk by Monday."),
    ("Unclear request", "Can you get us some milk for next week?"),
]


# ---------------------------------------------------------------- formatting

def eur(value) -> str:
    return f"€{value:,.2f}" if value is not None else "–"


def qty(value) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".") if value is not None else "–"


def when(iso: str | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not iso:
        return "–"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo:
        dt = dt.astimezone(config.HELSINKI)
    return dt.strftime(fmt).lstrip("0")


def day(iso: str | None) -> str:
    return when(iso, "%a %d %b") if iso else "–"


def claim_label(claim: str) -> str:
    if claim.startswith("FREE_FROM_"):
        return claim[10:].replace("_", " ").capitalize() + "-free"
    return claim.replace("_", " ").capitalize()


templates.env.filters.update(eur=eur, qty=qty, when=when, day=day, claim=claim_label)
templates.env.globals.update(STATUS=STATUS, ALLERGEN_NAMES=ALLERGEN_NAMES)


def page(request: Request, name: str, **ctx):
    with SessionLocal() as s:
        pending = s.scalar(select(func.count()).select_from(Run).where(Run.status == "NEEDS_HUMAN"))
        chain = verify_chain(s)
    return templates.TemplateResponse(request, name, {"pending": pending, "chain": chain, **ctx})


def checks_for(policy_data: dict) -> tuple[list[dict], list[dict]]:
    """Split the policy result into rules that fired and rules that passed, in evaluation order."""
    reasons = {r["rule"]: r for r in policy_data.get("reasons", [])}
    passed = set(policy_data.get("passed", []))
    fired, ok = [], []
    for rule in policy.RULE_IDS:
        label = RULE_LABELS.get(rule, rule)
        if rule in reasons:
            r = reasons[rule]
            fired.append({"rule": rule, "label": label, "message": r["message"],
                          "tone": "bad" if r["outcome"] == "REJECT" else "warn"})
        elif f"{rule}:warning" in passed:
            ok.append({"rule": rule, "label": "Supplier-delivered: exact stock not confirmed", "note": True})
        else:
            ok.append({"rule": rule, "label": label, "note": False})
    return fired, ok


def event_summary(e: dict) -> str:
    p, kind = e["payload"], e["event_type"]
    if kind == "candidates.considered":
        return f"{len(p.get('priced', []))} products priced, {len(p.get('rejected_on_requirements', []))} ruled out on requirements"
    if kind == "plan.chosen":
        return f"{len(p.get('lines', []))} line(s), {eur(p.get('total_net'))} before VAT"
    if kind == "policy.evaluated":
        return f"{STATUS_WORD.get(p.get('decision'), p.get('decision'))}; {len(p.get('passed', []))} rules passed, {len(p.get('reasons', []))} triggered"
    if kind == "approval.requested":
        return f"Approver: {p.get('approver')}"
    if kind == "approval.decided":
        return f"{p.get('decision', '').capitalize()} by {p.get('approver')}, signed"
    if kind == "order.placed":
        return f"{p.get('order_id')}, {eur(p.get('total_net'))}"
    if kind == "order.refused":
        return ", ".join(p.get("rules", []))
    if kind == "approval.reopened":
        return f"New since it was sent: {', '.join(p.get('rules', []))}"
    if kind == "lookup.failed":
        return p.get("error", "")
    if kind == "clarification.requested":
        return p.get("question", "")
    if kind == "intent.parsed":
        return f"{qty(p.get('quantity'))} {p.get('unit')} by {p.get('deadline')}, parser {p.get('parser')}"
    return ""


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
def home(request: Request):
    with SessionLocal() as s:
        recent = s.scalars(select(Run).order_by(Run.created_at.desc()).limit(5)).all()
    return page(request, "home.html", mandates=MANDATES, examples=EXAMPLES, recent=recent, active="new")


@app.post("/requests", include_in_schema=False)
def submit(text: str = Form(...), mandate: str = Form("hotel")):
    if mandate not in MANDATES or not text.strip():
        raise HTTPException(400, "unknown mandate or empty request")
    with SessionLocal() as s:
        result = agent.run(text.strip(), MANDATES[mandate], s, _api, llm=get_llm())
    return RedirectResponse(f"/requests/{result.trace_id}", status_code=303)


@app.get("/requests/{trace_id}", include_in_schema=False)
def run_detail(request: Request, trace_id: str):
    with SessionLocal() as s:
        row = s.get(Run, trace_id)
        if row is None:
            raise HTTPException(404, "request not found")
        events = [event_to_dict(e) for e in events_for_trace(s, trace_id)]
    for e in events:
        e["label"] = EVENT_LABELS.get(e["event_type"], e["event_type"])
        e["summary"] = event_summary(e)
    intent = json.loads(row.intent_json) if row.intent_json else None
    plan = json.loads(row.plan_json) if row.plan_json else None
    policy_data = json.loads(row.policy_json) if row.policy_json else {}
    fired, ok = checks_for(policy_data) if policy_data else ([], [])
    approval = json.loads(row.approval_json) if row.approval_json else None
    mandate = MANDATE_BY_ID.get(row.mandate_id)
    reopened = row.status == "NEEDS_HUMAN" and bool(events) and events[-1]["event_type"] == "approval.reopened"
    return page(request, "run.html", run=row, intent=intent, plan=plan, fired=fired, ok=ok, events=events,
                approval=approval, mandate=mandate, reopened=reopened,
                approval_valid=bool(approval and mandate and trust.verify_approval(approval, mandate.approver)),
                active="approvals" if row.status == "NEEDS_HUMAN" else "activity")


@app.post("/requests/{trace_id}/decision", include_in_schema=False)
def decision(trace_id: str, action: str = Form(...), note: str = Form("")):
    if action not in ("approve", "decline"):
        raise HTTPException(400, "action must be approve or decline")
    with SessionLocal() as s:
        row = s.get(Run, trace_id)
        if row is None:
            raise HTTPException(404, "request not found")
        mandate = MANDATE_BY_ID.get(row.mandate_id)
        if mandate is None:
            raise HTTPException(409, f"mandate {row.mandate_id} is not loaded")
        try:
            agent.decide(s, trace_id, mandate, approve=(action == "approve"), note=note.strip()[:500])
        except ValueError as e:
            raise HTTPException(409, str(e))
    return RedirectResponse(f"/requests/{trace_id}", status_code=303)


@app.get("/approvals", include_in_schema=False)
def approvals(request: Request):
    with SessionLocal() as s:
        rows = s.scalars(select(Run).where(Run.status == "NEEDS_HUMAN").order_by(Run.created_at.desc())).all()
    reasons = {r.trace_id: [x["message"] for x in json.loads(r.policy_json)["reasons"]] for r in rows}
    return page(request, "approvals.html", rows=rows, reasons=reasons, active="approvals")


@app.get("/activity", include_in_schema=False)
def activity(request: Request):
    with SessionLocal() as s:
        rows = s.scalars(select(Run).order_by(Run.created_at.desc()).limit(200)).all()
    return page(request, "activity.html", rows=rows, active="activity")


@app.get("/mandates", include_in_schema=False)
def mandates(request: Request):
    items = [{"key": k, "m": m, "valid": trust.verify_mandate(m),
              "public_key": trust.org_signer(m.mandate_id).public_key_b64()} for k, m in MANDATES.items()]
    return page(request, "mandates.html", items=items, active="mandates")


@app.get("/audit", include_in_schema=False)
def audit(request: Request):
    with SessionLocal() as s:
        events = [event_to_dict(e) for e in
                  s.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(300))]
    for e in events:
        e["label"] = EVENT_LABELS.get(e["event_type"], e["event_type"])
    return page(request, "audit.html", events=events, active="audit")


# ---------------------------------------------------------------- JSON

@app.get("/audit/verify", tags=["audit"], summary="Recompute the audit hash chain")
def audit_verify():
    with SessionLocal() as s:
        c = verify_chain(s)
    return {"ok": c.ok, "events_checked": c.events_checked, "first_bad_seq": c.first_bad_seq,
            "problem": c.problem, "head_hash": c.head_hash}
