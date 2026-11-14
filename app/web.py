"""The buyer's console: ask for something, watch the agent work with Valio Aimo, approve, and check the record.

Run (with the gateway running on :8100):  .venv/Scripts/python -m uvicorn app.web:app --port 8000
Or both at once:                          .venv/Scripts/python -m scripts.dev
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

import settings as config
from app import agent, keys, policy
from app.audit import AuditEvent, event_to_dict, events_for_trace, verify_chain
from app.db import Order, Run, SessionLocal, Step, init_db
from app.gateway_client import GatewayClient
from app.llm import get_llm
from app.mandates import PEOPLE
from app.timeline import ALLERGEN_NAMES, describe

log = logging.getLogger("console")
HERE = Path(__file__).parent
app = FastAPI(title="Trusted Trade buyer console", version="0.3.0")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

init_db()
MANDATES = keys.load_mandates()
MANDATE_BY_ID = {m.mandate_id: m for m in MANDATES.values()}
GATEWAY = GatewayClient()

STATUS = {
    "WORKING": ("Working on it", "busy"),
    "DECIDING": ("Placing the order", "busy"),
    "ORDERED": ("Ordered", "ok"),
    "APPROVED": ("Approved and ordered", "ok"),
    "NEEDS_HUMAN": ("Waiting for approval", "warn"),
    "NEEDS_CLARIFICATION": ("Needs more detail", "warn"),
    "REFUSED": ("Not ordered", "bad"),
    "DECLINED": ("Declined", "bad"),
    "EXPIRED": ("Offer changed", "bad"),
    "FAILED": ("Could not finish", "bad"),
}
ACTIVE = ("WORKING", "DECIDING")

RULE_LABELS = {
    "mandate.signature": "The mandate is signed by the organisation",
    "mandate.validity": "The mandate is in date",
    "plan.empty": "Something suitable was found",
    "hard.required_claims": "Required claims are met",
    "hard.allergens": "No excluded allergens",
    "hard.allergen_data": "Allergen data is present",
    "category.allowed": "Product category is allowed",
    "spend.per_order": "Within the order limit",
    "spend.per_week": "Within the weekly limit",
    "spend.intent_budget": "Within the requested budget",
    "band.price_premium": "Price is close to the cheapest option",
    "preference.cost": "Preferences don't cost much extra",
    "band.overage": "Not over-ordered",
    "band.shortfall": "The full quantity is covered",
    "band.delivery": "Arrives by the deadline",
    "order.cutoff": "Order cut-off hasn't passed",
    "substitution.relaxed_preferences": "Preferences are respected",
    "substitution.split": "Splitting across products is allowed",
    "uncertainty.intent_confidence": "The request was clear",
    "uncertainty.reliability": "Delivery reliability is good",
    "uncertainty.unconfirmed_quantity": "Stock quantity is confirmed",
}

EVENT_LABELS = {
    "request.received": "Request received",
    "clarification.requested": "Asked for more detail",
    "intent.parsed": "Request understood",
    "offer.received": "Signed offer received from Valio Aimo",
    "lookup.failed": "Could not get an offer",
    "plan.chosen": "Plan chosen",
    "policy.evaluated": "Checked against the mandate",
    "approval.requested": "Sent for approval",
    "approval.decided": "Approver decided (signed)",
    "approval.reopened": "Sent back for review",
    "order.reserved": "Order reserved against the weekly limit",
    "order.confirmed": "Order confirmed by Valio Aimo (signed)",
    "order.refused": "Order refused",
    "order.failed": "Order could not be placed",
    "offer.changed": "Valio Aimo's offer changed",
    "order.placed": "Order placed",
}

EXAMPLES = [
    ("Within the agent's authority", "I need 1,000 litres of lactose-free milk by Monday. Find an option that meets "
                                     "our requirements based on price and availability."),
    ("Over the order limit", "I need 6,000 litres of lactose-free milk by Monday."),
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


def person_name(person_id: str | None) -> str:
    p = PEOPLE.get(person_id or "")
    return p.name if p else ""


templates.env.filters.update(eur=eur, qty=qty, when=when, day=day, claim=claim_label, person=person_name)
templates.env.globals.update(STATUS=STATUS, ALLERGEN_NAMES=ALLERGEN_NAMES, PEOPLE=PEOPLE, reader=agent.reader_name)


# ---------------------------------------------------------------- who is using the console

def current_person(request: Request):
    """The person using the console (a demo sign-in: pick a name; see ASSUMPTIONS.md)."""
    return PEOPLE.get(request.cookies.get("person", "")) or PEOPLE["aino.koskinen"]


def page(request: Request, name: str, **ctx):
    me = current_person(request)
    with SessionLocal() as s:
        pending = s.scalar(select(func.count()).select_from(Run).where(Run.status == "NEEDS_HUMAN"))
        chain = verify_chain(s)
    return templates.TemplateResponse(request, name, {"pending": pending, "chain": chain, "me": me,
                                                      "my_org": MANDATES[me.org_key], **ctx})


@app.post("/session", include_in_schema=False)
def switch_person(person: str = Form(...), back: str = Form("/")):
    if person not in PEOPLE:
        raise HTTPException(400, "unknown person")
    target = back if back.startswith("/") and not back.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("person", person, samesite="lax", httponly=True)
    return response


# ---------------------------------------------------------------- run views

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
        elif rule in passed:
            ok.append({"rule": rule, "label": label, "note": False})
    return fired, ok


def timeline_for(s, row: Run) -> list[dict]:
    plan = json.loads(row.plan_json) if row.plan_json else {}
    names = {l["product_id"]: l["name"] for l in plan.get("lines", [])}
    steps = s.scalars(select(Step).where(Step.trace_id == row.trace_id).order_by(Step.id)).all()
    return [describe(x, names) for x in steps]


def event_summary(e: dict) -> str:
    p, kind = e["payload"], e["event_type"]
    if kind == "offer.received":
        return f"{p.get('offer_id') or 'no offer'}, signature {'verified' if p.get('signature_verified') else 'not checked'}"
    if kind == "plan.chosen":
        return f"{len(p.get('lines', []))} line(s), {eur(p.get('total_net'))} before VAT"
    if kind == "policy.evaluated":
        word = {"AUTO_APPROVE": "Within authority", "NEEDS_HUMAN": "Needs a person", "REJECT": "Not allowed"}
        return f"{word.get(p.get('decision'), p.get('decision'))}; {len(p.get('passed', []))} rules passed, " \
               f"{len(p.get('reasons', []))} triggered"
    if kind == "approval.requested":
        return f"Approver: {person_name(p.get('approver')) or p.get('approver')}"
    if kind == "approval.decided":
        return f"{p.get('decision', '').capitalize()} by {p.get('approver_name') or p.get('approver')}"
    if kind in ("order.confirmed", "order.placed"):
        return f"{p.get('order_id')}, {eur(p.get('total_net'))}"
    if kind == "order.reserved":
        return f"{p.get('po')}, {eur(p.get('total_net'))}"
    if kind in ("order.refused", "order.failed"):
        return p.get("error") or ", ".join(p.get("rules", []))
    if kind == "approval.reopened":
        return ", ".join(p.get("rules", [])) or p.get("error", "")
    if kind in ("lookup.failed", "clarification.requested"):
        return p.get("error") or p.get("question", "")
    if kind == "intent.parsed":
        return f"{qty(p.get('quantity'))} {p.get('unit')} by {p.get('deadline')}, read by {p.get('parser')}"
    return ""


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
def home(request: Request):
    with SessionLocal() as s:
        recent = s.scalars(select(Run).order_by(Run.created_at.desc()).limit(6)).all()
    return page(request, "home.html", examples=EXAMPLES, recent=recent, active="new")


def _work(trace_id: str, text: str, mandate) -> None:
    """Background: the agent's run. Any unexpected error is recorded rather than leaving the run spinning."""
    with SessionLocal() as s:
        try:
            agent.run(text, mandate, s, GATEWAY, llm=get_llm(), trace_id=trace_id)
        except Exception as e:                                 # noqa: BLE001  shown to the user, logged in full
            log.exception("run %s failed", trace_id)
            s.rollback()
            row = s.get(Run, trace_id)
            if row is not None and row.status in ACTIVE:
                row.status, row.receipt = "FAILED", f"Something went wrong: {type(e).__name__}. Nothing was ordered."
                s.commit()


@app.post("/requests", include_in_schema=False)
def submit(request: Request, text: str = Form(...)):
    me = current_person(request)
    text = text.strip()
    if not text:
        raise HTTPException(400, "empty request")
    mandate = MANDATES[me.org_key]
    with SessionLocal() as s:
        trace_id = agent.open_run(s, text[:1000], mandate, requested_by=me.id)
    threading.Thread(target=_work, args=(trace_id, text[:1000], mandate), daemon=True).start()
    return RedirectResponse(f"/requests/{trace_id}", status_code=303)


def _run_context(trace_id: str) -> dict:
    with SessionLocal() as s:
        row = s.get(Run, trace_id)
        if row is None:
            raise HTTPException(404, "request not found")
        steps = timeline_for(s, row)
        order = s.scalar(select(Order).where(Order.trace_id == trace_id, Order.status == "PLACED"))
        events = [event_to_dict(e) for e in events_for_trace(s, trace_id)]
    for e in events:
        e["label"] = EVENT_LABELS.get(e["event_type"], e["event_type"])
        e["summary"] = event_summary(e)
    policy_data = json.loads(row.policy_json) if row.policy_json else {}
    fired, ok = checks_for(policy_data) if policy_data else ([], [])
    approval = json.loads(row.approval_json) if row.approval_json else None
    mandate = MANDATE_BY_ID.get(row.mandate_id)
    confirmation = json.loads(order.confirmation_json) if order and order.confirmation_json else None
    return dict(run=row, steps=steps, events=events, fired=fired, ok=ok, approval=approval, mandate=mandate,
                intent=json.loads(row.intent_json) if row.intent_json else None,
                plan=json.loads(row.plan_json) if row.plan_json else None, confirmation=confirmation,
                approval_valid=bool(approval and mandate and keys.verify_approval(approval, mandate)),
                reopened=row.status == "NEEDS_HUMAN" and bool(events) and events[-1]["event_type"] == "approval.reopened",
                working=row.status in ACTIVE)


@app.get("/requests/{trace_id}", include_in_schema=False)
def run_detail(request: Request, trace_id: str):
    ctx = _run_context(trace_id)
    return page(request, "run.html", **ctx,
                active="approvals" if ctx["run"].status == "NEEDS_HUMAN" else "activity")


@app.get("/requests/{trace_id}/live", include_in_schema=False)
def run_live(request: Request, trace_id: str):
    """The timeline so far, for the page to refresh while the agent works."""
    ctx = _run_context(trace_id)
    html = templates.get_template("_timeline.html").render(**ctx)
    return JSONResponse({"status": ctx["run"].status, "html": html})


@app.post("/requests/{trace_id}/decision", include_in_schema=False)
def decision(request: Request, trace_id: str, action: str = Form(...), note: str = Form("")):
    if action not in ("approve", "decline"):
        raise HTTPException(400, "action must be approve or decline")
    me = current_person(request)
    with SessionLocal() as s:
        row = s.get(Run, trace_id)
        if row is None:
            raise HTTPException(404, "request not found")
        mandate = MANDATE_BY_ID.get(row.mandate_id)
        if mandate is None:
            raise HTTPException(409, f"mandate {row.mandate_id} is not loaded")
        if me.id != mandate.approver:
            raise HTTPException(403, f"only {agent.approver_name(mandate)} can decide this")
        try:
            agent.decide(s, trace_id, mandate, approve=(action == "approve"), gateway=GATEWAY,
                         note=note.strip()[:500])
        except ValueError as e:
            raise HTTPException(409, str(e))
    return RedirectResponse(f"/requests/{trace_id}", status_code=303)


@app.get("/approvals", include_in_schema=False)
def approvals(request: Request):
    with SessionLocal() as s:
        rows = s.scalars(select(Run).where(Run.status == "NEEDS_HUMAN").order_by(Run.created_at.desc())).all()
    reasons = {r.trace_id: [x["message"] for x in json.loads(r.policy_json)["reasons"]] for r in rows}
    approvers = {r.trace_id: MANDATE_BY_ID[r.mandate_id].approver_name for r in rows if r.mandate_id in MANDATE_BY_ID}
    return page(request, "approvals.html", rows=rows, reasons=reasons, approvers=approvers, active="approvals")


@app.get("/activity", include_in_schema=False)
def activity(request: Request):
    with SessionLocal() as s:
        rows = s.scalars(select(Run).order_by(Run.created_at.desc()).limit(200)).all()
    return page(request, "activity.html", rows=rows, active="activity")


@app.get("/mandates", include_in_schema=False)
def mandates(request: Request):
    items = [{"key": k, "m": m, "valid": keys.verify_mandate(m)} for k, m in MANDATES.items()]
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
