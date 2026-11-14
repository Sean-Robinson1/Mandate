"""The buyer's console: ask for something, watch the agent work with Valio Aimo, approve, and check the record.

Run (with the gateway running on :8100):  .venv/Scripts/python -m uvicorn app.web:app --port 8000
Or both at once:                          .venv/Scripts/python -m scripts.dev
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

import settings as config
from app import agent, keys, kitchen, policy, rules
from app.audit import AuditEvent, append_event, event_to_dict, events_for_trace, lock_chain, verify_chain
from app.db import Order, Run, SessionLocal, Step, init_db
from app.gateway_client import GatewayClient, GatewayError
from app.llm import get_llm
from app.mandates import PEOPLE
from app.timeline import ALLERGEN_NAMES, describe

log = logging.getLogger("console")
HERE = Path(__file__).parent
app = FastAPI(title="Trusted Trade buyer console", version="0.4.0")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

init_db()
MANDATES = keys.load_mandates()
MANDATE_BY_ID = {m.mandate_id: m for m in MANDATES.values()}
GATEWAY = GatewayClient()
_restock_lock = threading.Lock()          # one restock check at a time, so a low row starts one order, not two
with SessionLocal() as _s:
    kitchen.seed(_s)


def _publish_mandates() -> None:
    """Make sure Valio Aimo holds the current version of each mandate (it may have been down when they changed)."""
    for m in MANDATES.values():
        try:
            GATEWAY.publish_mandate(m.model_dump(mode="json"))
        except GatewayError as e:
            log.warning("could not publish %s v%s to Valio Aimo: %s", m.mandate_id, m.version, e)


threading.Thread(target=_publish_mandates, daemon=True).start()

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
    "request.review_required": "The kitchen lets the agent restock this alone",
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
    "mandate.amended": "Mandate changed and signed",
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
    return dt.strftime(fmt.replace("%d", str(dt.day))).lstrip("0")


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


def is_manager(person) -> bool:
    """The purchasing manager: the approver named in their organisation's mandate."""
    return person.id == MANDATES[person.org_key].approver


_chain_cache: dict = {"at": 0.0, "key": None, "result": None}
CHAIN_CACHE_SECONDS = 10


def cached_chain(s):
    """The sidebar's chain check. Re-hashing the whole log on every page grows with the log, so the result is reused
    for a few seconds while the log has not grown. The audit page always re-hashes from scratch."""
    key = s.scalar(select(func.max(AuditEvent.seq)))
    now = time.monotonic()
    if _chain_cache["key"] != key or now - _chain_cache["at"] > CHAIN_CACHE_SECONDS:
        _chain_cache.update(at=now, key=key, result=verify_chain(s))
    return _chain_cache["result"]


def my_mandate_id(me) -> str:
    return MANDATES[me.org_key].mandate_id


def page(request: Request, name: str, status_code: int = 200, fresh_chain: bool = False, **ctx):
    me = current_person(request)
    with SessionLocal() as s:
        pending = s.scalar(select(func.count()).select_from(Run)
                           .where(Run.status == "NEEDS_HUMAN", Run.mandate_id == my_mandate_id(me)))
        chain = verify_chain(s) if fresh_chain else cached_chain(s)
    return templates.TemplateResponse(request, name, {"pending": pending, "chain": chain, "me": me,
                                                      "my_org": MANDATES[me.org_key], **ctx},
                                      status_code=status_code)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """Pages get a readable error page; the JSON endpoints the pages call keep their JSON."""
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    titles = {403: "Not allowed", 404: "Not found", 409: "Already changed"}
    detail = str(exc.detail) if exc.detail else ""
    return page(request, "error.html", status_code=exc.status_code,
                title=titles.get(exc.status_code, "Something went wrong"),
                detail=detail[:1].upper() + detail[1:])


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

def _sheet_context(me) -> dict:
    mandate = MANDATES[me.org_key]
    with SessionLocal() as s:
        rows = kitchen.sheet(s, me.org_key, agent.approver_name(mandate))
        day = kitchen.today(s)
    return {"rows": rows, "day": day, "manager": is_manager(me), "busy": any(r.tone == "busy" for r in rows)}


@app.get("/", include_in_schema=False)
def home(request: Request, text: str = "", delivered: int | None = None, started: int | None = None):
    me = current_person(request)
    with SessionLocal() as s:
        recent = s.scalars(select(Run).where(Run.mandate_id == my_mandate_id(me))
                           .order_by(Run.created_at.desc()).limit(6)).all()
    summary = {"delivered": delivered, "started": started} if delivered is not None else None
    return page(request, "home.html", examples=EXAMPLES, recent=recent, active="supplies", prefill=text[:1000],
                summary=summary, **_sheet_context(me))


@app.get("/supplies/sheet", include_in_schema=False)
def supplies_sheet(request: Request):
    """The sheet alone, for the page to refresh while the agent works."""
    me = current_person(request)
    ctx = _sheet_context(me)
    return JSONResponse({"html": templates.get_template("_sheet.html").render(**ctx), "busy": ctx["busy"]})


@app.post("/supplies/{supply_id}", include_in_schema=False)
async def edit_supply(request: Request, supply_id: int):
    me = current_person(request)
    body = await request.json()
    with SessionLocal() as s:
        supply = s.get(kitchen.Supply, supply_id)
        if supply is None or supply.org_key != me.org_key:
            return JSONResponse({"error": "not your kitchen's sheet"}, status_code=403)
        try:
            kitchen.update_cell(s, supply_id, str(body.get("field")), str(body.get("value")), me.id, is_manager(me))
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    start_restocks(me.org_key)
    return JSONResponse({"ok": True})


def _delivered_count(s, mandate_id: str) -> int:
    return s.scalar(select(func.count()).select_from(Order).join(Run, Run.trace_id == Order.trace_id)
                    .where(Order.status == "DELIVERED", Run.mandate_id == mandate_id)) or 0


@app.post("/kitchen/next-day", include_in_schema=False)
def next_day(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        before = _delivered_count(s, my_mandate_id(me))
        kitchen.next_day(s)
        delivered = _delivered_count(s, my_mandate_id(me)) - before
    started = {org_key: len(start_restocks(org_key)) for org_key in MANDATES}
    return RedirectResponse(f"/?delivered={delivered}&started={started[me.org_key]}", status_code=303)


def start_restocks(org_key: str) -> list[str]:
    """Let the agent act on every row that is low. Returns the trace ids it started."""
    mandate = MANDATES[org_key]
    started = []
    with _restock_lock, SessionLocal() as s:
        for r in kitchen.due_restocks(s, org_key):
            trace_id = agent.open_run(s, r.text, mandate, supply_id=r.supply_id, trigger=r.trigger)
            threading.Thread(target=_work, args=(trace_id, r.text, mandate, r.review_reason), daemon=True).start()
            started.append(trace_id)
    return started


def _work(trace_id: str, text: str, mandate, review_reason: str | None = None) -> None:
    """Background: the agent's run. Any unexpected error is recorded rather than leaving the run spinning."""
    with SessionLocal() as s:
        try:
            agent.run(text, mandate, s, GATEWAY, llm=get_llm(), trace_id=trace_id, review_reason=review_reason)
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
    me = current_person(request)
    with SessionLocal() as s:
        rows = s.scalars(select(Run).where(Run.status == "NEEDS_HUMAN", Run.mandate_id == my_mandate_id(me))
                         .order_by(Run.created_at.desc())).all()
    reasons = {r.trace_id: [x["message"] for x in json.loads(r.policy_json)["reasons"]] for r in rows}
    approvers = {r.trace_id: MANDATE_BY_ID[r.mandate_id].approver_name for r in rows if r.mandate_id in MANDATE_BY_ID}
    return page(request, "approvals.html", rows=rows, reasons=reasons, approvers=approvers, active="approvals",
                can_approve=is_manager(me))


@app.get("/activity", include_in_schema=False)
def activity(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        rows = s.scalars(select(Run).where(Run.mandate_id == my_mandate_id(me))
                         .order_by(Run.created_at.desc()).limit(200)).all()
    return page(request, "activity.html", rows=rows, active="activity")


@app.get("/mandates", include_in_schema=False)
def mandates_redirect():
    return RedirectResponse("/rules", status_code=301)


def _held_by_valio_aimo(mandate_id: str) -> int | None:
    try:
        return GATEWAY.mandate_version(mandate_id)
    except GatewayError:
        return None


@app.get("/rules", include_in_schema=False)
def rules_page(request: Request, signed: int | None = None):
    me = current_person(request)
    with SessionLocal() as s:
        items = [{"key": k, "m": m, "valid": keys.verify_mandate(m), "history": keys.mandate_history(k),
                  "valio_version": _held_by_valio_aimo(m.mandate_id), "suggestions": rules.suggestions(s, m),
                  "can_edit": me.id == m.approver}
                 for k, m in sorted(MANDATES.items(), key=lambda kv: kv[0] != me.org_key)]
    return page(request, "mandates.html", items=items, signed=signed, active="rules")


def _editable(request: Request, key: str):
    me = current_person(request)
    mandate = MANDATES.get(key)
    if mandate is None:
        raise HTTPException(404, "no such mandate")
    if me.id != mandate.approver:
        raise HTTPException(403, f"only {agent.approver_name(mandate)} can change these rules")
    return me, mandate


@app.get("/rules/{key}/edit", include_in_schema=False)
def edit_rules(request: Request, key: str):
    me, mandate = _editable(request, key)
    values = rules.form_values(mandate)
    values.update({k: v for k, v in request.query_params.items() if k in values})   # e.g. a suggestion's figure
    return page(request, "rules_edit.html", key=key, m=mandate, fields=rules.FIELDS, values=values,
                allergens=rules.ALLERGENS, errors=[], active="rules")


@app.post("/rules/{key}/preview", include_in_schema=False)
async def preview_rules(request: Request, key: str):
    me, mandate = _editable(request, key)
    form = dict(await request.form())
    proposed, errors = rules.parse(form, mandate)
    changes = rules.diff(mandate, proposed) if not errors else []
    if errors or not changes:
        values = {**rules.form_values(mandate), **form}
        for f in rules.FIELDS:                     # unticked boxes are absent from the form: show them unticked
            if f.kind == "bool":
                values[f.path] = bool(form.get(f.path))
            elif f.kind == "allergens":
                values[f.path] = [a for a in rules.ALLERGENS if form.get(f"{f.path}.{a}")]
        return page(request, "rules_edit.html", key=key, m=mandate, fields=rules.FIELDS, values=values,
                    allergens=rules.ALLERGENS, errors=errors or ["Nothing changed."], active="rules")
    return page(request, "rules_preview.html", key=key, m=mandate, changes=changes, form=form, active="rules")


@app.post("/rules/{key}/sign", include_in_schema=False)
async def sign_rules(request: Request, key: str):
    me, mandate = _editable(request, key)
    form = dict(await request.form())
    note = str(form.pop("change_note", "")).strip()[:300]
    proposed, errors = rules.parse(form, mandate)
    changes = rules.diff(mandate, proposed)
    if errors or not changes:
        raise HTTPException(400, "; ".join(errors) or "nothing changed")
    new = keys.amend_mandate(key, mandate, rules.changes_only(mandate, proposed), me.id, note)
    MANDATES[key] = new
    MANDATE_BY_ID[new.mandate_id] = new
    with SessionLocal() as s:
        lock_chain(s)
        append_event(s, trace_id=f"{new.mandate_id}-v{new.version}", actor=f"human:{me.id}",
                     event_type="mandate.amended",
                     payload={"mandate_id": new.mandate_id, "version": new.version, "note": note,
                              "changes": [{k: c[k] for k in ("label", "old", "new")} for c in changes],
                              "signature": new.signature, "signer_key_id": new.signer_key_id})
        s.commit()
    try:
        GATEWAY.publish_mandate(new.model_dump(mode="json"))
    except GatewayError as e:
        log.warning("Valio Aimo did not take mandate %s v%s yet: %s", new.mandate_id, new.version, e)
    return RedirectResponse(f"/rules?signed={new.version}", status_code=303)


@app.get("/audit", include_in_schema=False)
def audit(request: Request):
    with SessionLocal() as s:
        events = [event_to_dict(e) for e in
                  s.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(300))]
    for e in events:
        e["label"] = EVENT_LABELS.get(e["event_type"], e["event_type"])
    return page(request, "audit.html", events=events, active="audit", fresh_chain=True)


# ---------------------------------------------------------------- JSON

@app.get("/audit/verify", tags=["audit"], summary="Recompute the audit hash chain")
def audit_verify():
    with SessionLocal() as s:
        c = verify_chain(s)
    return {"ok": c.ok, "events_checked": c.events_checked, "first_bad_seq": c.first_bad_seq,
            "problem": c.problem, "head_hash": c.head_hash}
