"""The buyer's console: ask for something, watch the agent work with Valio Aimo, approve, and check the record.

Run (with the gateway running on :8100):  .venv/Scripts/python -m uvicorn app.web:app --port 8000
Or both at once:                          .venv/Scripts/python -m scripts.dev
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

import settings as config
from app import agent, charts, costs, keys, kitchen, menu, policy, prices, review, rules
from app.audit import AuditEvent, append_event, event_to_dict, events_for_trace, lock_chain, verify_chain
from app.db import Order, Run, Sale, SessionLocal, StockLevel, Step, init_db, wipe
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
    kitchen.link_typed_requests(_s, agent.ORG_OF)


def _publish_mandates() -> None:
    """Make sure Valio Aimo holds the current version of each mandate (it may have been down when they changed)."""
    for m in MANDATES.values():
        try:
            GATEWAY.publish_mandate(m.model_dump(mode="json"))
        except GatewayError as e:
            log.warning("could not publish %s v%s to Valio Aimo: %s", m.mandate_id, m.version, e)


threading.Thread(target=_publish_mandates, daemon=True).start()


def _resend_loop() -> None:
    """Orders Valio Aimo did not answer are sent again until it does: they are never dropped."""
    while True:
        time.sleep(20)
        try:
            with SessionLocal() as s:
                for trace_id in agent.resend_queued(s, GATEWAY, MANDATE_BY_ID):
                    log.info("queued order %s answered by Valio Aimo", trace_id)
        except Exception:                                    # noqa: BLE001  keep the loop alive; logged in full
            log.exception("resending queued orders failed")


threading.Thread(target=_resend_loop, daemon=True).start()

STATUS = {
    "WORKING": ("Working on it", "busy"),
    "DECIDING": ("Placing the order", "busy"),
    "ORDERED": ("Ordered", "ok"),
    "APPROVED": ("Approved and ordered", "ok"),
    "NEEDS_HUMAN": ("Waiting for approval", "warn"),
    "QUEUED": ("Queued: resending to Valio Aimo", "warn"),
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
    "quantity.unusual": "The amount is normal for this kitchen",
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


def on(d: date | None) -> str:
    return f"{d:%a} {d.day} {d:%b}" if d else "–"


templates.env.filters.update(eur=eur, qty=qty, when=when, day=day, claim=claim_label, person=person_name, on=on,
                             unit=kitchen.unit_label, eur0=lambda v: f"€{v:,.0f}")
templates.env.globals.update(
    STOPPED=kitchen.STOPPED, kitchen_short=kitchen.short_name, lead=kitchen.lead_days,
    supplier_contact=lambda name: kitchen.SUPPLIERS.get(name, {}).get("contact", ""))
def day_on(day_number: int) -> date:
    """The calendar date of a kitchen day."""
    with SessionLocal() as s:
        return kitchen.date_of(kitchen.calendar(s)[1], day_number)


templates.env.globals.update(STATUS=STATUS, ALLERGEN_NAMES=ALLERGEN_NAMES, PEOPLE=PEOPLE, reader=agent.reader_name,
                             has_menu=menu.has_menu, day_on=day_on)


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

SOURCE_LABELS = {"day": "Day's use", "count": "Stock count", "import": "Stock file", "api": "Stock system",
                 "history": "Earlier records"}
REORDER_CHOICES = [
    ("off", "Off", "The kitchen orders this item itself."),
    ("auto", "Agent restocks", "When stock falls below the reorder level, the agent orders up to the 'order up to' "
                               "level, within the purchasing rules."),
    ("ask", "Agent asks first", "The agent prepares the order; the purchasing manager approves it before it is sent."),
]
WEEK_NOTES = {"school": "none at weekends", "hotel": "busier at weekends",
              "restaurant": "quiet Mondays, busiest Friday and Saturday"}


def _count_label(level, start) -> dict | None:
    if level is None:
        return None
    on = kitchen.date_of(start, level.day)
    source = {"import": "From a stock file", "api": "From the stock system"}.get(level.source)
    return {"label": f"{on:%a %d %b}", "source": source or (f"Counted by {person_name(level.by)}" if level.by
                                                            else "Opening count")}


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _headline(groups, upcoming, approver: str) -> str:
    """One sentence saying what needs doing, in place of number tiles."""
    manual = [kitchen.short_name(g.supplier) for g in groups if not g.agent]
    waiting = sum(1 for g in groups for r in g.rows if r.open_run and r.open_run.status == "NEEDS_HUMAN")
    agent = any(g.agent for g in groups)
    due = sum(1 for i in upcoming if i.due)
    parts = []
    if manual:
        parts.append(f"Order from {_and(manual)} this week.")
    if agent:
        tail = (f", and {'one of its orders is' if waiting == 1 else f'{waiting} of its orders are'} "
                f"waiting for {approver}") if waiting else ""
        parts.append(f"The agent is handling Valio Aimo{tail}.")
    if due:
        parts.append(f"{due} deliver{'y' if due == 1 else 'ies'} to check in.")
    return " ".join(parts) or "Nothing needs ordering this week."


def _dashboard(me) -> dict:
    mandate = MANDATES[me.org_key]
    approver = agent.approver_name(mandate)
    with SessionLocal() as s:
        rows = kitchen.sheet(s, me.org_key, approver)
        day, start = kitchen.calendar(s)
        upcoming, _ = kitchen.deliveries(s, me.org_key)
        counted = _count_label(kitchen.last_count(s, me.org_key), start)
        first = kitchen.use_first(s, me.org_key)
        other = kitchen.unlinked_orders(s, mandate.mandate_id)
    groups = kitchen.to_order(rows)
    with SessionLocal() as s:
        waiting = s.scalars(select(Run).where(Run.mandate_id == mandate.mandate_id, Run.status == "NEEDS_HUMAN")
                            .order_by(Run.created_at.desc())).all()
    areas =[(key, label, [r for r in rows if r.supply.storage == key]) for key, label in kitchen.AREAS.items()]
    return {"rows": rows, "day": day, "today": kitchen.date_of(start, day), "groups": groups,
            "upcoming": [i for i in upcoming if i.day <= day + 7], "later": [i for i in upcoming if i.day > day + 7],
            "use_first": first, "other_orders": other, "areas": [a for a in areas if a[2]], "counted": counted,
            "headline": _headline(groups, upcoming, approver), "approver": approver,
            "stock_value": sum(r.value for r in rows), "on_order_value": sum(i.value for i in upcoming),
            "manager": is_manager(me), "busy": any(r.tone == "busy" for r in rows),
            "suggestions": kitchen.suggestions(rows, day), "waiting": waiting,
            "waiting_total": sum(r.total_net or 0 for r in waiting), "can_approve": me.id == mandate.approver}


@app.get("/", include_in_schema=False)
def home(request: Request, imported: int | None = None, unknown: str = "", invalid: str = "", error: str = "",
         done: str = "", text: str = "", delivered: int | None = None, started: int | None = None):
    me = current_person(request)
    if text:                                       # "ask again": the request box lives on Activity now
        return RedirectResponse("/activity?" + urlencode({"text": text[:1000]}) + "#request-form", status_code=303)
    notice = None
    if error:
        notice = {"tone": "bad", "text": error}
    elif imported is not None:
        msg = f"Updated the stock count for {imported} item{'s' if imported != 1 else ''}."
        if unknown:
            msg += f" Not recognised, so skipped: {unknown}."
        if invalid:
            msg += f" Not a valid count: {invalid}."
        notice = {"tone": "warn" if unknown or invalid else "ok", "text": msg}
    elif done:
        notice = {"tone": "ok", "text": done}
    elif delivered is not None:
        parts = [f"{delivered} delivery{'' if delivered == 1 else 'ies'} taken in" if delivered else None,
                 f"the agent started {started} order{'' if started == 1 else 's'}" if started else None]
        msg = "; ".join(p for p in parts if p)
        notice = {"tone": "ok", "text": "A day passed" + (f": {msg}." if msg else ". Nothing needed ordering.")}
    return page(request, "home.html", active="supplies", notice=notice, **_dashboard(me))


@app.get("/supplies/sheet", include_in_schema=False)
def supplies_sheet(request: Request):
    """The dashboard's working area, for the page to refresh while the agent works."""
    me = current_person(request)
    ctx = {**_dashboard(me), "me": me, "my_org": MANDATES[me.org_key]}
    return JSONResponse({"board": templates.get_template("_board.html").render(**ctx),
                         "headline": ctx["headline"], "busy": ctx["busy"]})


def _back(**params) -> RedirectResponse:
    return RedirectResponse("/?" + urlencode({k: v[:300] for k, v in params.items()}), status_code=303)


@app.post("/supplies/agent-order", include_in_schema=False)
async def agent_order(request: Request):
    """Ask the agent to order one or more items now. Each still goes through the mandate's checks."""
    me = current_person(request)
    form = await request.form()
    ids = [int(v) for v in form.getlist("item") if str(v).isdigit()][:20]
    mandate, started, errors = MANDATES[me.org_key], [], []
    with _restock_lock, SessionLocal() as s:
        for supply_id in ids:
            try:
                r = kitchen.agent_order(s, me.org_key, supply_id, str(form.get(f"qty-{supply_id}", "")))
            except ValueError as e:
                errors.append(str(e))
                continue
            trace_id = agent.open_run(s, r.text, mandate, requested_by=me.id, supply_id=r.supply_id, trigger=r.trigger)
            threading.Thread(target=_work, args=(trace_id, r.text, mandate, r.review_reason), daemon=True).start()
            started.append(trace_id)
    if len(started) == 1 and not errors:
        return RedirectResponse(f"/requests/{started[0]}", status_code=303)
    if errors and not started:
        return _back(error="; ".join(errors).capitalize() + ".")
    return _back(done=f"The agent is ordering {len(started)} item{'s' if len(started) != 1 else ''}."
                      + (f" Not started: {'; '.join(errors)}." if errors else ""))


@app.post("/supplies/{supply_id}/ordered", include_in_schema=False)
def mark_ordered(request: Request, supply_id: int, quantity: str = Form("")):
    me = current_person(request)
    with SessionLocal() as s:
        try:
            d = kitchen.mark_ordered(s, me.org_key, supply_id, me.id, quantity)
        except ValueError as e:
            return _back(error=str(e).capitalize() + ".")
        supply, (day, start) = s.get(kitchen.Supply, supply_id), kitchen.calendar(s)
        when = on(kitchen.date_of(start, d.due_day))
    return _back(done=f"Marked {d.quantity:,g} {kitchen.unit_label(supply.unit)} of {supply.name.lower()} as ordered "
                      f"from {supply.supplier}, due {when}.")


@app.post("/deliveries/{key}/check-in", include_in_schema=False)
def check_in(request: Request, key: str):
    me = current_person(request)
    with SessionLocal() as s:
        try:
            supply = kitchen.check_in(s, me.org_key, key, me.id)
        except ValueError as e:
            return _back(error=str(e).capitalize() + ".")
    start_restocks(me.org_key)
    return _back(done=f"Checked in. {supply.name} now has {qty(supply.on_hand)} {kitchen.unit_label(supply.unit)}.")


@app.post("/supplies/count", include_in_schema=False)
async def save_count(request: Request):
    me = current_person(request)
    form = await request.form()
    counts = {int(k[6:]): str(v) for k, v in form.items() if k.startswith("count-") and k[6:].isdigit()}
    with SessionLocal() as s:
        result = kitchen.save_count(s, me.org_key, counts, me.id)
    start_restocks(me.org_key)
    return _back(imported=str(len(result.updated)), invalid="; ".join(result.invalid[:5]))


@app.get("/supplies/export.csv", include_in_schema=False)
def export_supplies(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        csv_text = kitchen.export_csv(kitchen.sheet(s, me.org_key))
    return Response(csv_text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="stock-{me.org_key}.csv"'})


@app.post("/supplies/import", include_in_schema=False)
async def import_supplies(request: Request, file: UploadFile):
    me = current_person(request)
    data = await file.read(1_000_001)
    if len(data) > 1_000_000:
        return _back(error="The file was not imported: it is larger than 1 MB.")
    try:
        counts = kitchen.parse_stock_file(data)
    except ValueError as e:
        return _back(error=f"The file was not imported: {e}.")
    with SessionLocal() as s:
        result = kitchen.import_counts(s, me.org_key, counts, "import", me.id)
    start_restocks(me.org_key)
    return RedirectResponse("/?" + urlencode({"imported": len(result.updated),
                                              "unknown": ", ".join(result.unknown[:10])[:300],
                                              "invalid": "; ".join(result.invalid[:5])[:300]}), status_code=303)


class StockCount(BaseModel):
    item: str = Field(min_length=1, max_length=120)
    on_hand: float | str


class StockCounts(BaseModel):
    kitchen: str = Field(max_length=20)
    counts: list[StockCount] = Field(max_length=1000)


@app.post("/api/stock-counts", tags=["stock"], summary="Receive stock counts from a kitchen's own stock system")
def receive_stock_counts(body: StockCounts, authorization: str = Header(default="")):
    """Counts are matched to items by name. Unknown names and invalid numbers are reported back, never added."""
    if not config.STOCK_API_TOKEN:
        raise HTTPException(503, "stock system connection is not set up (STOCK_API_TOKEN)")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {config.STOCK_API_TOKEN}".encode()):
        raise HTTPException(401, "invalid token")
    if body.kitchen not in MANDATES:
        raise HTTPException(404, "unknown kitchen")
    with SessionLocal() as s:
        result = kitchen.import_counts(s, body.kitchen, [(c.item, c.on_hand) for c in body.counts], "api",
                                       "stock-system")
    start_restocks(body.kitchen)
    return {"updated": result.updated, "unknown": result.unknown, "invalid": result.invalid}


def _my_supply(s, me, supply_id: int):
    supply = s.get(kitchen.Supply, supply_id)
    if supply is None or supply.org_key != me.org_key:
        raise HTTPException(404, "no such item in your kitchen")
    return supply


@app.get("/supplies/{supply_id}", include_in_schema=False)
def item_page(request: Request, supply_id: int, error: str = "", saved: str = ""):
    me = current_person(request)
    mandate = MANDATES[me.org_key]
    with SessionLocal() as s:
        supply = _my_supply(s, me, supply_id)
        row = next(r for r in kitchen.sheet(s, me.org_key, agent.approver_name(mandate)) if r.supply.id == supply_id)
        day, start = kitchen.calendar(s)
        arrivals = [i for i in kitchen.incoming(s, [supply], day, start) if i.day > day - kitchen.HISTORY_DAYS]
    return page(request, "item.html", active="supplies", row=row, incoming=arrivals, manager=is_manager(me),
                chart=charts.stock_chart(row, day, start, arrivals), use=charts.use_chart(row, start),
                date_of=lambda d: kitchen.date_of(start, d), min_due=kitchen.date_of(start, day + 1).isoformat(),
                error=error, saved=saved, SOURCE_LABELS=SOURCE_LABELS, REORDER_CHOICES=REORDER_CHOICES,
                WEEK_NOTE=WEEK_NOTES.get(me.org_key, ""))


@app.post("/supplies/{supply_id}/orders", include_in_schema=False)
def record_order(request: Request, supply_id: int, quantity: str = Form(...), due: str = Form(...),
                 reference: str = Form("")):
    me = current_person(request)
    with SessionLocal() as s:
        supply = _my_supply(s, me, supply_id)
        try:
            kitchen.record_order(s, supply, quantity, due, me.id, reference)
        except ValueError as e:
            return RedirectResponse(f"/supplies/{supply_id}?" + urlencode({"error": str(e).capitalize() + "."}),
                                    status_code=303)
    return RedirectResponse(f"/supplies/{supply_id}?saved=Order+recorded.", status_code=303)


@app.post("/supplies/{supply_id}", include_in_schema=False)
async def edit_supply(request: Request, supply_id: int):
    me = current_person(request)
    body = await request.json()
    with SessionLocal() as s:
        supply = s.get(kitchen.Supply, supply_id)
        if supply is None or supply.org_key != me.org_key:
            return JSONResponse({"error": "not your kitchen's item"}, status_code=403)
        try:
            kitchen.update_cell(s, supply_id, str(body.get("field")), str(body.get("value")), me.id, is_manager(me))
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    start_restocks(me.org_key)
    return JSONResponse({"ok": True})


@app.post("/demo/reset", include_in_schema=False)
def reset_demo(confirm: str = Form("")):
    """Start the simulation again from day 1. Needs the confirm field so a stray request cannot wipe the demo."""
    if confirm != "reset":
        raise HTTPException(400, "confirm the reset")
    with _restock_lock:
        wipe()
        with SessionLocal() as s:
            kitchen.seed(s)
    return _back(done="The demo started again: day 1, fresh stock, no requests or orders. Purchasing rules are unchanged.")


def _delivered_count(s, mandate_id: str) -> int:
    return s.scalar(select(func.count()).select_from(Order).join(Run, Run.trace_id == Order.trace_id)
                    .where(Order.status == "DELIVERED", Run.mandate_id == mandate_id)) or 0


@app.post("/kitchen/next-day", include_in_schema=False)
def next_day(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        before = _delivered_count(s, my_mandate_id(me))
        new_day = kitchen.next_day(s)
        delivered = _delivered_count(s, my_mandate_id(me)) - before
    started = {org_key: len(start_restocks(org_key)) for org_key in MANDATES}
    if menu.has_menu(me.org_key):                 # watch the day's service, then its summary
        return RedirectResponse(f"/service?day={new_day}&play=1&started={started[me.org_key]}", status_code=303)
    return RedirectResponse(f"/?delivered={delivered}&started={started[me.org_key]}", status_code=303)


@app.get("/menu", include_in_schema=False)
def menu_page(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        day = kitchen.today(s)
        views = menu.dishes(s, me.org_key, today=day)
    sold = sum(v.sold_week for v in views)
    cost = sum(v.cost * v.sold_week for v in views)
    net = sum(v.net_price * v.sold_week for v in views)
    groups = [(label, [v for v in views if v.dish.category == key]) for key, label in menu.CATEGORIES.items()]
    return page(request, "menu.html", active="menu", groups=[g for g in groups if g[1]], views=views, sold=sold,
                week_pct=round(100 * cost / net, 1) if net else None, target=menu.FOOD_COST_TARGET, vat=menu.VAT)


@app.get("/costs", include_in_schema=False)
def costs_page(request: Request, logged: str = "", error: str = ""):
    me = current_person(request)
    if not menu.has_menu(me.org_key):
        raise HTTPException(404, "this kitchen has no menu")
    with SessionLocal() as s:
        o = costs.overview(s, me.org_key)
    notice = {"tone": "bad", "text": error} if error else {"tone": "ok", "text": logged} if logged else None
    return page(request, "costs.html", active="costs", o=o, reasons=costs.REASONS, notice=notice,
                closing=costs.CLOSING)


@app.post("/costs/waste", include_in_schema=False)
def log_waste(request: Request, supply_id: int = Form(...), quantity: str = Form(...), reason: str = Form(...),
              note: str = Form("")):
    me = current_person(request)
    try:
        with SessionLocal() as s:
            w = costs.log_waste(s, me.org_key, supply_id, quantity, reason, me.id, note)
            name, unit = s.get(kitchen.Supply, supply_id).name, kitchen.unit_label(s.get(kitchen.Supply, supply_id).unit)
    except ValueError as e:
        return RedirectResponse("/costs?" + urlencode({"error": str(e)[:200]}) + "#waste-form", status_code=303)
    start_restocks(me.org_key)                       # throwing stock away can take an item below its level
    msg = f"Logged {w.quantity:g} {unit} of {name.lower()} as waste ({eur(w.cost)}), and took it off the stock count."
    return RedirectResponse("/costs?" + urlencode({"logged": msg}), status_code=303)


@app.get("/service", include_in_schema=False)
def service_page(request: Request, day: int | None = None, play: int = 0, started: int = 0):
    me = current_person(request)
    if not menu.has_menu(me.org_key):
        raise HTTPException(404, "this kitchen has no menu")
    with SessionLocal() as s:
        today_day, start = kitchen.calendar(s)
        if day is None:                                   # day 0 and earlier are the seeded history
            day = menu.last_service_day(s, me.org_key)
        summary = menu.day_summary(s, me.org_key, day) if day is not None else None
        rows = kitchen.sheet(s, me.org_key, agent.approver_name(MANDATES[me.org_key]))
        views = menu.dishes(s, me.org_key)
        levels = {l.supply_id: l for l in s.scalars(select(StockLevel).where(
            StockLevel.supply_id.in_([r.supply.id for r in rows]), StockLevel.day == day))} if day is not None else {}
        days = sorted(set(s.scalars(select(Sale.day).where(Sale.org_key == me.org_key))), reverse=True)
        first = kitchen.use_first(s, me.org_key)
    if summary is None:
        return page(request, "service.html", active="service", summary=None, days=days)
    # The ingredients that moved most in value today, as bars that drain while the tickets play.
    by_id = {r.supply.id: r for r in rows}
    moved = sorted((l for l in levels.values() if l.used > 0 and l.supply_id in by_id),
                   key=lambda l: -l.used * (by_id[l.supply_id].supply.unit_cost or 0))[:8]
    watch = [{"id": l.supply_id, "name": by_id[l.supply_id].supply.name, "unit": by_id[l.supply_id].unit,
              "start": round(l.on_hand + l.used, 2), "end": l.on_hand,
              "low": by_id[l.supply_id].supply.reorder_below,
              "full": max(by_id[l.supply_id].supply.order_up_to, l.on_hand + l.used)} for l in moved]
    recipes = {v.dish.name: {i.supply.id: i.used for i in v.ingredients} for v in views}
    prices_now = {v.dish.name: v.dish.price for v in views}
    tickets = [{"n": t.number, "time": menu.clock(t.minute), "table": t.table, "covers": t.covers,
                "items": t.items, "out": t.sold_out} for t in summary.tickets]
    low = [r for r in rows if r.supply.on_hand < r.supply.reorder_below]     # even if a delivery is on the way
    return page(request, "service.html", active="service", summary=summary, day=day, days=days,
                on=kitchen.date_of(start, day), is_today=day == today_day, play=bool(play), started=started,
                feed={"tickets": tickets, "watch": watch, "recipes": recipes, "prices": prices_now},
                low=low, use_first=first, target=menu.FOOD_COST_TARGET)


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
    with SessionLocal() as s:
        ctx["retry_blocker"] = agent.retry_blocker(s, ctx["run"]) if ctx["run"].status in agent.NOT_ORDERED else None
        ctx["brief"] = review.brief(s, ctx["run"], ctx["plan"], ctx["intent"], ctx["mandate"]) \
            if ctx["run"].status == "NEEDS_HUMAN" else None
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


@app.post("/requests/{trace_id}/revise", include_in_schema=False)
def revise(request: Request, trace_id: str, text: str = Form(...)):
    """The approver sends a pending request back with changes: the old one is declined (signed, with the new
    wording as the note) and the agent starts over on the changed request, under the same rules."""
    me = current_person(request)
    text = text.strip()[:1000]
    if not text:
        raise HTTPException(400, "write the changed request")
    with SessionLocal() as s:
        row = s.get(Run, trace_id)
        if row is None:
            raise HTTPException(404, "request not found")
        mandate = MANDATE_BY_ID.get(row.mandate_id)
        if mandate is None:
            raise HTTPException(409, f"mandate {row.mandate_id} is not loaded")
        if me.id != mandate.approver:
            raise HTTPException(403, f"only {agent.approver_name(mandate)} can send this back")
        try:
            agent.decide(s, trace_id, mandate, approve=False, gateway=GATEWAY, note=f"Sent back to the agent: {text}")
        except ValueError as e:
            raise HTTPException(409, str(e))
        supply = s.get(kitchen.Supply, row.supply_id) if row.supply_id else None
        reason = kitchen.review_reason(supply)
        new_id = agent.open_run(s, text, mandate, requested_by=me.id, supply_id=row.supply_id,
                                trigger=f"Sent back with changes by {me.name}")
    threading.Thread(target=_work, args=(new_id, text, mandate, reason), daemon=True).start()
    return RedirectResponse(f"/requests/{new_id}", status_code=303)


@app.post("/requests/{trace_id}/retry", include_in_schema=False)
def retry(request: Request, trace_id: str):
    """A request that was not ordered (refused, failed, offer changed, declined): the agent starts over on the same
    words, under the same rules. Nothing carries over from the earlier attempt except the link to it."""
    me = current_person(request)
    with SessionLocal() as s:
        row = s.get(Run, trace_id)
        if row is None:
            raise HTTPException(404, "request not found")
        mandate = MANDATE_BY_ID.get(row.mandate_id)
        if mandate is None or mandate.mandate_id != MANDATES[me.org_key].mandate_id:
            raise HTTPException(403, "this request belongs to another kitchen")
        blocker = agent.retry_blocker(s, row)
        if blocker:
            raise HTTPException(409, blocker)
        supply = s.get(kitchen.Supply, row.supply_id) if row.supply_id else None
        new_id = agent.open_run(s, row.request_text, mandate, requested_by=me.id, supply_id=row.supply_id,
                                trigger=f"Tried again by {me.name} after: {STATUS.get(row.status, (row.status,))[0]}")
    threading.Thread(target=_work, args=(new_id, row.request_text, mandate, kitchen.review_reason(supply)),
                     daemon=True).start()
    return RedirectResponse(f"/requests/{new_id}", status_code=303)


# ---------------------------------------------------------------- ingredient prices

@app.get("/prices", include_in_schema=False)
def prices_page(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        data = prices.overview(s, me.org_key)
    return page(request, "prices.html", data=data, approver=agent.approver_name(MANDATES[me.org_key]), active="prices")


@app.post("/prices/switch", include_in_schema=False)
def prices_switch(request: Request, supply_id: int = Form(...), alt_product_id: str = Form(...)):
    """Hand a cheaper equivalent to the agent: it orders through the same mandate checks and approvals as any request."""
    me = current_person(request)
    mandate = MANDATES[me.org_key]
    with SessionLocal() as s:
        data = prices.overview(s, me.org_key)
        item = next((c for c in data["cheaper"]
                     if c["supply_id"] == supply_id and c["alt_product_id"] == alt_product_id), None)
        if item is None:
            raise HTTPException(404, "that saving is no longer on offer")
        if not item["orderable"]:
            raise HTTPException(409, f"the agent cannot order {item['supply'].lower()}; order it from the supplier")
        text = prices.switch_request_text(item, date.fromisoformat(data["date"]))[:1000]
        trace_id = agent.open_run(s, text, mandate, requested_by=me.id, supply_id=supply_id)
    threading.Thread(target=_work, args=(trace_id, text, mandate), daemon=True).start()
    return RedirectResponse(f"/requests/{trace_id}", status_code=303)


@app.get("/products/{product_id}", include_in_schema=False)
def product_page(request: Request, product_id: str):
    """A product as Valio Aimo's catalog describes it, fetched from Valio Aimo's gateway."""
    if not product_id.replace("-", "").replace("_", "").isalnum() or len(product_id) > 20:
        raise HTTPException(404, "no such product")
    try:
        p = GATEWAY.product(product_id)
    except GatewayError as e:
        raise HTTPException(404 if "404" in str(e) else 502, str(e))
    shop = None
    if config.VALIO_SHOP_URL and (p.get("gtin") or "{id}" in config.VALIO_SHOP_URL):
        shop = config.VALIO_SHOP_URL.replace("{gtin}", p.get("gtin") or "").replace("{id}", p["product_id"])
    return page(request, "product.html", p=p, shop=shop, raw=json.dumps(p, indent=2, ensure_ascii=False),
                active="activity")


@app.get("/approvals", include_in_schema=False)
def approvals(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        rows = s.scalars(select(Run).where(Run.status == "NEEDS_HUMAN", Run.mandate_id == my_mandate_id(me))
                         .order_by(Run.created_at.desc())).all()
        briefs = {r.trace_id: review.brief(s, r, json.loads(r.plan_json) if r.plan_json else None,
                                           json.loads(r.intent_json) if r.intent_json else None,
                                           MANDATE_BY_ID.get(r.mandate_id)) for r in rows}
    reasons = {r.trace_id: [x["message"] for x in json.loads(r.policy_json)["reasons"]] for r in rows}
    approvers = {r.trace_id: MANDATE_BY_ID[r.mandate_id].approver_name for r in rows if r.mandate_id in MANDATE_BY_ID}
    return page(request, "approvals.html", rows=rows, reasons=reasons, approvers=approvers, active="approvals",
                can_approve=is_manager(me), briefs=briefs)


@app.get("/activity", include_in_schema=False)
def activity(request: Request):
    me = current_person(request)
    with SessionLocal() as s:
        rows = s.scalars(select(Run).where(Run.mandate_id == my_mandate_id(me))
                         .order_by(Run.created_at.desc()).limit(200)).all()
    return page(request, "activity.html", rows=rows, examples=EXAMPLES, active="activity",
                prefill=request.query_params.get("text", "")[:1000])


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
