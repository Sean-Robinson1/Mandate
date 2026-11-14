"""Each kitchen's stock: items from any supplier, what to order, what is arriving, and when things run out.

Where the numbers come from, like a real kitchen:
  - stock counts: a count walked through each storage area, a stock file, or the kitchen's own stock system;
  - daily use: the crew's figure for a typical day, scaled by the kitchen's week (a school is closed at weekends);
  - deliveries: orders a person placed (and ticked off here), and orders the purchasing agent placed.
    A delivery adds to stock when someone checks it in.

Automatic reordering is an add-on per item, only for suppliers whose ordering gateway the agent can use:
  off:  the kitchen orders it itself;
  auto: when stock runs low the agent restocks on its own, if the mandate allows;
  ask:  the agent prepares the order, and the purchasing manager approves it first.

The demo's 'next day' button uses a day of stock and lets the agent react to what is low.
"""
from __future__ import annotations

import csv
import io
import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

import settings as config
from app.db import Delivery, Order, Run, SimState, StockLevel, Supply

OPEN = ("WORKING", "DECIDING", "NEEDS_HUMAN", "QUEUED")
STOPPED = ("REFUSED", "FAILED", "DECLINED", "EXPIRED", "NEEDS_CLARIFICATION")
CREW_FIELDS = {"on_hand", "daily_use"}                     # anyone in the kitchen
MANAGER_FIELDS = {"reorder_below", "order_up_to", "rule"}  # the purchasing manager
RULES = ("off", "auto", "ask")
CONNECTED = {"Valio Aimo"}           # suppliers with an ordering gateway the agent can use
HISTORY_DAYS = 28
HORIZON = 21                         # days of forecast
ORDER_AHEAD = 3                      # list an item to order this many days before its last day to order
USE_FIRST_DAYS = 3                   # show batches this close to their use-by date
STEP = {"L": 10, "KG": 5, "PCS": 30}
UNIT_WORDS = {"L": "litres", "KG": "kg", "PCS": "pieces"}
AREAS = {"cold": "Cold room", "dry": "Dry store", "clean": "Cleaning"}
# Share of a typical day's use on each weekday, Monday first. shortcut: per kitchen in settings when there are more.
WEEK = {"hotel": (0.9, 0.9, 0.95, 1.0, 1.1, 1.25, 0.9),
        "school": (1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0)}
FLAT = (1.0,) * 7
# shortcut: a suppliers table when kitchens can add their own.
SUPPLIERS = {
    "Valio Aimo": {"lead_days": 2, "terms": "delivers Mon–Sat, order by 14:00", "contact": "Ordering gateway"},
    "Pelto Produce Oy": {"lead_days": 3, "terms": "delivers Tue and Fri", "contact": "Phone 040 123 4567"},
    "Paahtimo Oy": {"lead_days": 2, "terms": "delivers in 2 days", "contact": "Webshop"},
    "Puhdas Oy": {"lead_days": 4, "terms": "delivers weekly", "contact": "Email orders"},
    "Leipomo Oy": {"lead_days": 1, "terms": "delivers every weekday", "contact": "Phone 040 765 4321"},
}

DEFAULT_SUPPLIES = {   # name, area, supplier, agent's term, unit, on hand, use, reorder, up to, rule, € per unit, shelf days
    "hotel": [
        ("Lactose-free milk", "cold", "Valio Aimo", "lactose-free milk", "L", 760, 220, 600, 1600, "auto", 1.05, 7),
        ("Cream", "cold", "Valio Aimo", "cream", "L", 110, 25, 80, 250, "auto", 3.9, 10),
        ("Butter", "cold", "Valio Aimo", "butter", "KG", 34, 6, 25, 80, "ask", 8.5, 60),
        ("Oat drink", "cold", "Valio Aimo", "oat drink", "L", 70, 15, 40, 150, "auto", 1.6, 0),
        ("Eggs", "cold", "Pelto Produce Oy", "", "PCS", 900, 240, 600, 1800, "off", 0.22, 21),
        ("Potatoes", "dry", "Pelto Produce Oy", "", "KG", 180, 35, 100, 300, "off", 0.9, 30),
        ("Coffee beans", "dry", "Paahtimo Oy", "", "KG", 22, 3, 12, 40, "off", 18.0, 0),
        ("Dishwasher detergent", "clean", "Puhdas Oy", "", "L", 40, 4, 20, 60, "off", 4.2, 0),
    ],
    "school": [
        ("Lactose-free milk", "cold", "Valio Aimo", "lactose-free milk", "L", 260, 90, 200, 600, "auto", 1.05, 7),
        ("Butter", "cold", "Valio Aimo", "butter", "KG", 14, 2, 10, 30, "auto", 8.5, 60),
        ("Oat drink", "cold", "Valio Aimo", "oat drink", "L", 35, 10, 25, 80, "ask", 1.6, 0),
        ("Carrots", "cold", "Pelto Produce Oy", "", "KG", 60, 14, 40, 120, "off", 1.1, 21),
        ("Rye bread", "dry", "Leipomo Oy", "", "KG", 45, 12, 30, 80, "off", 4.5, 4),
    ],
}


def unit_label(unit: str) -> str:
    return {"PCS": "pcs", "KG": "kg"}.get(unit, unit)


def short_name(supplier: str) -> str:
    return supplier.removesuffix(" Oy")


def connected(supply: Supply) -> bool:
    """Can the purchasing agent order this item? Only from a supplier with a gateway, and with a product term."""
    return supply.supplier in CONNECTED and bool(supply.ask_as)


def agent_orders(supply: Supply) -> bool:
    return supply.rule != "off" and connected(supply)


def lead_days(supply: Supply) -> int:
    return SUPPLIERS.get(supply.supplier, {}).get("lead_days", 2)


def expected_use(supply: Supply, on: date) -> float:
    return round(supply.daily_use * WEEK.get(supply.org_key, FLAT)[on.weekday()], 2)


# ---------------------------------------------------------------- the calendar

def date_of(start: date, day: int) -> date:
    return start + timedelta(days=day - 1)


def calendar(session: Session) -> tuple[int, date]:
    """Today's kitchen day number and the date of day 1."""
    state = session.get(SimState, 1)
    if state is None or not state.start:
        return (state.day if state else 1), datetime.now(config.HELSINKI).date()
    return state.day, date.fromisoformat(state.start)


def today(session: Session) -> int:
    return calendar(session)[0]


# ---------------------------------------------------------------- first run

def seed(session: Session, start: date | None = None) -> None:
    """Give each kitchen its items with four weeks of history, and start the calendar (first run only)."""
    start = start or datetime.now(config.HELSINKI).date()
    state = session.get(SimState, 1)
    if state is None:
        session.add(SimState(id=1, day=1, start=start.isoformat()))
    elif not state.start:                                    # a database from before days had dates
        state.start = (start - timedelta(days=state.day - 1)).isoformat()
    if session.scalar(select(Supply.id).limit(1)) is None:
        for org, rows in DEFAULT_SUPPLIES.items():
            for i, (name, area, supplier, ask_as, unit, on_hand, use, low, up_to, rule, cost, shelf) in enumerate(rows):
                s = Supply(org_key=org, name=name, storage=area, supplier=supplier, ask_as=ask_as, unit=unit,
                           on_hand=on_hand, daily_use=use, reorder_below=low, order_up_to=up_to, rule=rule,
                           unit_cost=cost, shelf_days=shelf, position=i)
                session.add(s)
                session.flush()
                _seed_history(session, s, start)
    session.commit()


def _seed_history(session: Session, s: Supply, start: date) -> None:
    """Believable past for a new item, worked backwards from today's count so today stays exactly as given.

    Going back in time stock only grows; once it passes 'order up to', a delivery must have arrived that day.
    """
    rng = random.Random(f"{s.org_key}/{s.name}")
    step = STEP.get(s.unit, 1)
    closing = float(s.on_hand)
    for day in range(1, 1 - HISTORY_DAYS, -1):
        raw = expected_use(s, date_of(start, day)) * rng.uniform(0.85, 1.15)
        used = float(round(raw)) if s.unit == "PCS" else round(raw, 1)
        opening, received = closing + used, 0.0
        if opening > s.order_up_to:
            target = s.reorder_below * rng.uniform(0.75, 1.0)
            received = float(max(step, math.floor((opening - target) / step) * step))
            opening -= received
            session.add(Delivery(supply_id=s.id, supplier=s.supplier, quantity=received, ordered_day=day - lead_days(s),
                                 due_day=day, received_day=day, status="RECEIVED"))
        session.add(StockLevel(supply_id=s.id, day=day, on_hand=round(closing, 2), used=used, received=received,
                               source="count" if day == 1 else "history"))
        closing = round(max(0.0, opening), 2)


# ---------------------------------------------------------------- reading the stock

def _measure(order: Order) -> float:
    return round(sum(line.get("measure", 0) for line in json.loads(order.lines_json or "[]")), 2)


def _arrival_day(order: Order, day: int) -> int:
    """The kitchen day an agent order arrives: the real lead time from Valio Aimo's offer, at least one day."""
    if order.arrives_on_day:
        return order.arrives_on_day
    placed = date.fromisoformat(order.created_at[:10])
    lead = (date.fromisoformat(order.delivery_date) - placed).days if order.delivery_date else 1
    return day + max(1, lead)


@dataclass
class Incoming:
    key: str                   # d-<delivery id> | o-<agent order id>: what 'check in' names
    supply: Supply
    day: int
    on: date
    quantity: float
    value: float
    supplier: str
    via: str                   # agent | manual
    status: str                # EXPECTED | RECEIVED
    link: str | None           # the agent's request, if it placed this order
    reference: str
    due: bool = False          # expected, and its day has come: waiting to be checked in


def incoming(session: Session, supplies: list[Supply], day: int, start: date) -> list[Incoming]:
    """Every delivery for these items, expected or received: recorded by hand, or ordered by the agent."""
    by_id = {s.id: s for s in supplies}
    out = []
    for d in session.scalars(select(Delivery).where(Delivery.supply_id.in_(list(by_id)))):
        s = by_id[d.supply_id]
        when = d.received_day if d.status == "RECEIVED" and d.received_day is not None else d.due_day
        out.append(Incoming(f"d-{d.id}", s, when, date_of(start, when), d.quantity, d.quantity * (s.unit_cost or 0),
                            d.supplier, "manual", d.status, None, d.reference))
    runs = dict(session.execute(select(Run.trace_id, Run.supply_id).where(Run.supply_id.in_(list(by_id)))).all())
    if runs:
        for o in session.scalars(select(Order).where(Order.trace_id.in_(list(runs)),
                                                     Order.status.in_(("PLACED", "DELIVERED")))):
            s, arrives = by_id[runs[o.trace_id]], _arrival_day(o, day)
            out.append(Incoming(f"o-{o.order_id}", s, arrives, date_of(start, arrives), _measure(o), o.total_net,
                                s.supplier, "agent", "EXPECTED" if o.status == "PLACED" else "RECEIVED", o.trace_id,
                                o.supplier_order_id or o.order_id))
    for i in out:
        i.due = i.status == "EXPECTED" and i.day <= day
    return sorted(out, key=lambda i: i.day)


def forecast(s: Supply, day: int, start: date, expected: list[Incoming]) -> tuple[list[tuple[int, float]], int | None]:
    """Stock for the next weeks at the typical use, counting what is on the way; and the day it would run out.

    A delivery that has arrived but is not checked in yet counts as stock today."""
    arriving: dict[int, float] = {}
    for i in expected:
        arriving[max(i.day, day)] = arriving.get(max(i.day, day), 0.0) + i.quantity
    stock = s.on_hand + arriving.get(day, 0.0)
    points, out = [(day, stock)], None
    for d in range(day + 1, day + HORIZON + 1):
        stock += arriving.get(d, 0.0) - expected_use(s, date_of(start, d))
        if stock <= 0 and out is None:
            out = d
        stock = max(0.0, stock)
        points.append((d, round(stock, 2)))
    return points, out


@dataclass
class Row:
    supply: Supply
    history: list[StockLevel]
    expected: list[Incoming]               # still on the way, soonest first
    forecast: list[tuple[int, float]]
    runs_out_day: int | None
    runs_out_on: date | None
    avg_use: float | None                  # over the last 7 days
    on_order: float
    arrives_day: int | None
    open_run: Run | None
    last_run: Run | None
    delivered_today: float
    days_left: int | None
    status: str
    tone: str                  # ok | warn | bad | busy
    link: str | None           # the agent request to open from the status
    connected: bool
    order_by_day: int | None = None
    order_by_on: date | None = None
    suggested: float = 0.0
    todo: bool = False         # should be ordered now or soon, and nothing is on the way

    @property
    def low(self) -> bool:
        return self.supply.on_hand + self.on_order < self.supply.reorder_below

    @property
    def unit(self) -> str:
        return unit_label(self.supply.unit)

    @property
    def value(self) -> float:
        return self.supply.on_hand * (self.supply.unit_cost or 0)

    @property
    def area(self) -> str:
        return AREAS.get(self.supply.storage, "Other")

    @property
    def lasts(self) -> int | None:
        return math.floor(self.supply.on_hand / self.supply.daily_use) if self.supply.daily_use > 0 else None


def sheet(session: Session, org_key: str, approver_name: str = "the approver") -> list[Row]:
    supplies = session.scalars(select(Supply).where(Supply.org_key == org_key).order_by(Supply.position)).all()
    if not supplies:
        return []
    day, start = calendar(session)
    ids = [s.id for s in supplies]
    runs = session.scalars(select(Run).where(Run.supply_id.in_(ids)).order_by(Run.created_at)).all()
    ordered = set(session.scalars(select(Order.trace_id).where(Order.trace_id.in_([r.trace_id for r in runs]),
                                                               Order.status.in_(("PLACED", "DELIVERED")))))
    arrivals = incoming(session, supplies, day, start)
    levels = session.scalars(select(StockLevel).where(StockLevel.supply_id.in_(ids),
                                                      StockLevel.day > day - HISTORY_DAYS)
                             .order_by(StockLevel.day)).all()
    rows = []
    for s in supplies:
        mine = [r for r in runs if r.supply_id == s.id]
        open_run = next((r for r in reversed(mine) if r.status in OPEN), None)
        with_order = [r for r in mine if r.trace_id in ordered]
        link = open_run or (with_order[-1] if with_order else None) or (mine[-1] if mine else None)
        history = [l for l in levels if l.supply_id == s.id]
        expected = [i for i in arrivals if i.supply.id == s.id and i.status == "EXPECTED"]
        on_order = round(sum(i.quantity for i in expected), 2)
        points, out = forecast(s, day, start, expected)
        recent = [l.used for l in history if l.day > day - 7]
        unit = unit_label(s.unit)
        order_by = out - lead_days(s) if out else None
        todo = not expected and not open_run and (s.on_hand < s.reorder_below
                                                  or (order_by is not None and order_by - day <= ORDER_AHEAD))
        if open_run and open_run.status == "NEEDS_HUMAN":
            status, tone = f"Waiting for {approver_name}", "warn"
        elif open_run and open_run.status == "QUEUED":
            status, tone = "Queued for Valio Aimo", "warn"
        elif open_run:
            status, tone = "Agent is ordering", "busy"
        elif any(i.due for i in expected):
            status, tone = "Arrived, check in", "busy"
        elif expected:
            status, tone = "On order", "busy"
        elif s.on_hand < s.reorder_below:
            status, tone = "Below reorder level", "bad"
        elif todo and order_by is not None:
            status, tone = ("Order today" if order_by <= day else f"Order by {date_of(start, order_by):%a}"), "warn"
        else:
            status, tone = "OK", "ok"
        row = Row(
            supply=s, history=history, expected=expected, forecast=points, runs_out_day=out,
            runs_out_on=date_of(start, out) if out else None,
            avg_use=round(sum(recent) / len(recent), 2) if recent else None,
            on_order=on_order, arrives_day=expected[0].day if expected else None, open_run=open_run,
            last_run=mine[-1] if mine else None,
            delivered_today=round(sum(i.quantity for i in arrivals
                                      if i.supply.id == s.id and i.status == "RECEIVED" and i.day == day), 2),
            days_left=out - day if out else None, status=status, tone=tone,
            link=link.trace_id if link else None, connected=connected(s),
            order_by_day=order_by, order_by_on=date_of(start, max(order_by, day)) if order_by is not None else None,
            todo=todo)
        row.suggested = restock_quantity(row)
        rows.append(row)
    return rows


@dataclass
class SupplierGroup:
    supplier: str
    terms: str
    contact: str
    agent: bool                # the purchasing agent orders this group
    rows: list[Row] = field(default_factory=list)


def to_order(rows: list[Row]) -> list[SupplierGroup]:
    """What needs ordering, by supplier: items due to be ordered, and items the agent is working on."""
    groups: dict[str, SupplierGroup] = {}
    for r in rows:
        if not (r.todo or r.open_run):
            continue
        s = r.supply
        info = SUPPLIERS.get(s.supplier, {})
        g = groups.setdefault(s.supplier, SupplierGroup(s.supplier, info.get("terms", ""), info.get("contact", ""),
                                                        agent=False))
        g.agent = g.agent or agent_orders(s)
        g.rows.append(r)
    return sorted(groups.values(), key=lambda g: (not g.agent, min(r.order_by_day or 0 for r in g.rows)))


def link_typed_requests(session: Session, org_of: dict[str, str]) -> int:
    """Typed requests from before they were linked to an item: link them now, so their deliveries show and count."""
    linked = 0
    for row in session.scalars(select(Run).where(Run.supply_id.is_(None), Run.intent_json != "")):
        org = org_of.get(row.mandate_id)
        if org is None:
            continue
        intent = json.loads(row.intent_json)
        item = match_supply(session, org, intent.get("product_terms", []), str(intent.get("unit", "")))
        if item is not None:
            row.supply_id = item.id
            linked += 1
    session.commit()
    return linked


def unlinked_orders(session: Session, mandate_id: str) -> list[tuple[Run, Order]]:
    """Agent orders on the way for things the kitchen does not track as stock items."""
    return list(session.execute(select(Run, Order).join(Order, Order.trace_id == Run.trace_id)
                                .where(Run.mandate_id == mandate_id, Run.supply_id.is_(None),
                                       Order.status == "PLACED")).all())


def deliveries(session: Session, org_key: str, days_back: int = 7) -> tuple[list[Incoming], list[Incoming]]:
    """What is on the way (soonest first) and what arrived in the last week (latest first)."""
    supplies = session.scalars(select(Supply).where(Supply.org_key == org_key)).all()
    if not supplies:
        return [], []
    day, start = calendar(session)
    arrivals = incoming(session, list(supplies), day, start)
    upcoming = [i for i in arrivals if i.status == "EXPECTED"]
    recent = [i for i in reversed(arrivals) if i.status == "RECEIVED" and i.day > day - days_back]
    return upcoming, recent


@dataclass
class Batch:
    supply: Supply
    quantity: float
    best_before: date
    days_left: int


def use_first(session: Session, org_key: str) -> list[Batch]:
    """Perishable stock close to its use-by date: stock is used oldest first, so what is left is the newest
    deliveries, and the oldest of those goes first."""
    supplies = session.scalars(select(Supply).where(Supply.org_key == org_key, Supply.shelf_days > 0)).all()
    if not supplies:
        return []
    day, start = calendar(session)
    received = [i for i in incoming(session, list(supplies), day, start) if i.status == "RECEIVED"]
    out = []
    for s in supplies:
        left, oldest = s.on_hand, None
        for i in sorted((i for i in received if i.supply.id == s.id), key=lambda i: -i.day):
            if left <= 0:
                break
            oldest = (i, min(left, i.quantity))
            left -= i.quantity
        if oldest is None or left > 0:          # stock older than our records: no date to show
            continue
        i, qty = oldest
        days = i.day + s.shelf_days - day
        if days <= USE_FIRST_DAYS:
            out.append(Batch(s, round(qty, 2), date_of(start, i.day + s.shelf_days), days))
    return sorted(out, key=lambda b: b.days_left)


def last_count(session: Session, org_key: str) -> StockLevel | None:
    """The latest stock count by a person, a stock file or a connected system."""
    return session.scalar(select(StockLevel).join(Supply, Supply.id == StockLevel.supply_id)
                          .where(Supply.org_key == org_key, StockLevel.source.in_(("count", "import", "api")))
                          .order_by(StockLevel.day.desc(), StockLevel.id.desc()).limit(1))


# ---------------------------------------------------------------- restocking

@dataclass
class Restock:
    supply_id: int
    org_key: str
    text: str
    trigger: str
    review_reason: str | None


def restock_quantity(row: Row) -> float:
    """Top up to the 'order up to' level, rounded up to a sensible step."""
    s = row.supply
    step = STEP.get(s.unit, 1)
    missing = s.order_up_to - s.on_hand - row.on_order
    return float(math.ceil(missing / step) * step) if missing > 0 else 0.0


def restock_text(supply: Supply, quantity: float, on: date | None = None) -> str:
    """The request the agent writes for itself, in the same words a person would use."""
    on = on or datetime.now(config.HELSINKI).date()
    by = on + timedelta(days=3)
    return f"Restock {quantity:,g} {UNIT_WORDS.get(supply.unit, supply.unit)} of {supply.ask_as} by {by:%A}"


def match_supply(session: Session, org_key: str, terms: list[str], unit: str) -> Supply | None:
    """The kitchen item a typed request is for: every product word in its name or product term, same unit."""
    words = [t.lower() for t in terms]
    if not words:
        return None
    return next((s for s in session.scalars(select(Supply).where(Supply.org_key == org_key).order_by(Supply.position))
                 if s.unit == unit and all(w in f"{s.ask_as} {s.name}".lower() for w in words)), None)


def usual_week(session: Session, org_key: str, terms: list[str], unit: str,
               supply_id: int | None = None) -> tuple[str, float] | None:
    """The kitchen's usual weekly use of the item a request is for: its own row if known, else the item whose
    product words match the request (same unit). None if the kitchen does not stock it."""
    match = session.get(Supply, supply_id) if supply_id else None
    if match is None or match.org_key != org_key:
        match = match_supply(session, org_key, terms, unit)
    if match is None or match.unit != unit or match.daily_use <= 0:
        return None
    week = sum(WEEK.get(org_key, FLAT)) * match.daily_use
    return match.name, round(week, 2)


def review_reason(supply: Supply | None) -> str | None:
    """Items the kitchen set to 'ask first' always go to the purchasing manager."""
    if supply is None or supply.rule != "ask":
        return None
    return f"The kitchen set {supply.name.lower()} to 'ask first', so ordering it needs approval."


def due_restocks(session: Session, org_key: str) -> list[Restock]:
    """Items the agent reorders that are below their level, with nothing already on the way or being decided."""
    out = []
    for row in sheet(session, org_key):
        s = row.supply
        if not agent_orders(s) or row.open_run or not row.low:
            continue
        quantity = restock_quantity(row)
        if quantity <= 0:
            continue
        out.append(Restock(
            supply_id=s.id, org_key=org_key, text=restock_text(s, quantity),
            trigger=f"{s.name} is low: {s.on_hand:,g} {row.unit} left"
                    + (f" and {row.on_order:,g} on the way" if row.on_order else "")
                    + f", reorder level {s.reorder_below:,g} {row.unit}",
            review_reason=None if s.rule == "auto" else
            f"The kitchen set {s.name.lower()} to 'ask first', so restocking it needs approval."))
    return out


# ---------------------------------------------------------------- time passing

def _receive(session: Session, key: str, day: int, by: str) -> tuple[Supply | None, float]:
    """Mark one delivery received. Returns its item and quantity, or (None, 0) if it was not waiting."""
    kind, _, ident = key.partition("-")
    if kind == "d" and ident.isdigit():
        d = session.get(Delivery, int(ident))
        if d is None or d.status != "EXPECTED":
            return None, 0.0
        d.status, d.received_day = "RECEIVED", day
        return session.get(Supply, d.supply_id), d.quantity
    if kind == "o":
        o = session.get(Order, ident)
        if o is None or o.status != "PLACED":
            return None, 0.0
        supply_id = session.scalar(select(Run.supply_id).where(Run.trace_id == o.trace_id))
        o.status, o.arrives_on_day = "DELIVERED", o.arrives_on_day or day
        return (session.get(Supply, supply_id) if supply_id else None), _measure(o)
    return None, 0.0


def _add_received(session: Session, supply: Supply, quantity: float, day: int, by: str) -> None:
    supply.on_hand = round(supply.on_hand + quantity, 2)
    level = session.scalar(select(StockLevel).where(StockLevel.supply_id == supply.id, StockLevel.day == day))
    if level is None:
        session.add(StockLevel(supply_id=supply.id, day=day, on_hand=supply.on_hand, received=quantity,
                               source="day", by=by))
    else:
        level.on_hand, level.received = supply.on_hand, round(level.received + quantity, 2)


def check_in(session: Session, org_key: str, key: str, person_id: str) -> Supply:
    """Someone received a delivery: its quantity goes into stock. Raises ValueError if it is not waiting."""
    day = today(session)
    for i in deliveries(session, org_key)[0]:
        if i.key == key:
            if not i.due:
                raise ValueError("that delivery is not due yet")
            break
    else:
        raise ValueError("no such delivery waiting")
    supply, quantity = _receive(session, key, day, person_id)
    if supply is None:
        raise ValueError("no such delivery waiting")
    _add_received(session, supply, quantity, day, person_id)
    session.commit()
    return supply


def next_day(session: Session) -> int:
    """One kitchen day: the day's use comes out of stock. Deliveries due today wait to be checked in;
    any still waiting from an earlier day are taken in first. shortcut: so the demo never stalls."""
    state = session.get(SimState, 1)
    day = state.day
    for order in session.scalars(select(Order).where(Order.status == "PLACED", Order.arrives_on_day.is_(None))):
        order.arrives_on_day = _arrival_day(order, day)
    state.day = day = day + 1
    start = date.fromisoformat(state.start) if state.start else calendar(session)[1]

    received: dict[int, float] = {}
    late = [f"o-{o.order_id}" for o in session.scalars(select(Order).where(Order.status == "PLACED",
                                                                           Order.arrives_on_day < day))]
    late += [f"d-{d.id}" for d in session.scalars(select(Delivery).where(Delivery.status == "EXPECTED",
                                                                         Delivery.due_day < day))]
    for key in late:
        supply, quantity = _receive(session, key, day, "")
        if supply is not None:
            received[supply.id] = received.get(supply.id, 0.0) + quantity
    for supply in session.scalars(select(Supply)):
        got = received.get(supply.id, 0.0)
        stock = supply.on_hand + got
        used = min(stock, expected_use(supply, date_of(start, day)))
        supply.on_hand = round(stock - used, 2)
        session.add(StockLevel(supply_id=supply.id, day=day, on_hand=supply.on_hand, used=round(used, 2),
                               received=round(got, 2), source="day"))
    session.commit()
    return day


# ---------------------------------------------------------------- changes by people and systems

def _number(value) -> float:
    try:
        number = float(str(value).strip().replace(",", ".").replace(" ", "").replace(" ", ""))
    except ValueError:
        raise ValueError("enter a number") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("enter a number of zero or more")
    return round(number, 2)


def _record_count(session: Session, supply: Supply, value: float, source: str, by: str) -> None:
    """A stock count replaces today's figure; what was used and received today stays."""
    day = today(session)
    supply.on_hand = value
    level = session.scalar(select(StockLevel).where(StockLevel.supply_id == supply.id, StockLevel.day == day))
    if level is None:
        session.add(StockLevel(supply_id=supply.id, day=day, on_hand=value, source=source, by=by))
    else:
        level.on_hand, level.source, level.by = value, source, by


def update_cell(session: Session, supply_id: int, field: str, value: str, person_id: str, is_manager: bool) -> Supply:
    """Change one setting. Raises PermissionError or ValueError with a message for the person."""
    supply = session.get(Supply, supply_id)
    if supply is None:
        raise ValueError("no such item")
    if field not in CREW_FIELDS | MANAGER_FIELDS:
        raise ValueError("that cannot be edited")
    if field in MANAGER_FIELDS and not is_manager:
        raise PermissionError("only the purchasing manager can change levels and automatic reordering")
    if field == "rule":
        if value not in RULES:
            raise ValueError("choose off, agent restocks, or ask first")
        if value != "off" and not connected(supply):
            raise ValueError(f"{supply.supplier or 'This supplier'} has no ordering gateway the agent can use")
        supply.rule = value
    elif field == "on_hand":
        _record_count(session, supply, _number(value), "count", person_id)
    else:
        number = _number(value)
        low = number if field == "reorder_below" else supply.reorder_below
        up_to = number if field == "order_up_to" else supply.order_up_to
        if up_to <= low:
            raise ValueError("'order up to' must be above 'reorder below'")
        setattr(supply, field, number)
    supply.updated_by = person_id
    session.commit()
    return supply


def record_order(session: Session, supply: Supply, quantity: str, due: str, person_id: str,
                 reference: str = "") -> Delivery:
    """An order placed outside the agent, so the dashboard knows it is coming."""
    day, start = calendar(session)
    amount = _number(quantity)
    if amount <= 0:
        raise ValueError("enter a quantity above zero")
    try:
        due_day = (date.fromisoformat(due) - start).days + 1
    except ValueError:
        raise ValueError("choose a delivery date") from None
    if due_day <= day:
        raise ValueError("the delivery date must be after today")
    if due_day > day + 90:
        raise ValueError("choose a delivery date within 90 days")
    d = Delivery(supply_id=supply.id, supplier=supply.supplier, quantity=amount, ordered_day=day, due_day=due_day,
                 reference=reference.strip()[:80], created_by=person_id)
    session.add(d)
    session.commit()
    return d


def mark_ordered(session: Session, org_key: str, supply_id: int, person_id: str,
                 quantity: str | None = None) -> Delivery:
    """An order a person placed with the supplier: the suggested quantity unless they typed another,
    due after the supplier's lead time."""
    row = next((r for r in sheet(session, org_key) if r.supply.id == supply_id), None)
    if row is None:
        raise ValueError("no such item in your kitchen")
    amount = _number(quantity) if quantity not in (None, "") else row.suggested
    if amount <= 0:
        raise ValueError(f"enter how much {row.supply.name.lower()} you ordered")
    day, start = calendar(session)
    due = date_of(start, day + lead_days(row.supply))
    return record_order(session, row.supply, str(amount), due.isoformat(), person_id)


def agent_order(session: Session, org_key: str, supply_id: int, quantity: str | None = None) -> Restock:
    """A person asks the agent to order an item now (from the dashboard): checked here, then the agent's
    usual checks against the signed mandate decide whether it may order alone."""
    row = next((r for r in sheet(session, org_key) if r.supply.id == supply_id), None)
    if row is None:
        raise ValueError("no such item in your kitchen")
    s = row.supply
    if not row.connected:
        raise ValueError(f"the agent cannot order from {s.supplier or 'this supplier'}")
    if row.open_run:
        raise ValueError(f"the agent is already working on {s.name.lower()}")
    amount = _number(quantity) if quantity not in (None, "") else row.suggested
    if amount <= 0:
        raise ValueError(f"enter how much {s.name.lower()} to order")
    return Restock(supply_id=s.id, org_key=org_key, text=restock_text(s, amount),
                   trigger=f"Asked from the dashboard: {amount:,g} {row.unit} of {s.name.lower()}",
                   review_reason=None if s.rule == "auto" else
                   f"The kitchen set {s.name.lower()} to 'ask first', so ordering it needs approval.")


@dataclass
class Suggestion:
    """An order the agent proposes: items from one supplier that should be ordered soon, in one delivery."""
    supplier: str
    rows: list[Row]
    total: float
    title: str
    body: str


def _when(row: Row, today_day: int) -> str:
    if row.runs_out_on is None:
        return "is below its reorder level"
    days = row.runs_out_day - today_day
    return "runs out tomorrow" if days <= 1 else f"runs out on {row.runs_out_on:%A}" if days < 7 else \
        f"runs out on {row.runs_out_on.day} {row.runs_out_on:%B}"


def suggestions(rows: list[Row], today_day: int | None = None) -> list[Suggestion]:
    """Deterministic: items the agent can order that are due soon and not yet low enough for it to act alone."""
    out = []
    for g in to_order(rows):
        ready = sorted((r for r in g.rows if r.connected and not r.open_run and r.suggested > 0),
                       key=lambda r: r.runs_out_day or 999)
        if not ready:
            continue
        first = ready[0]
        day = today_day if today_day is not None else (first.forecast[0][0] if first.forecast else 0)
        total = sum(r.suggested * (r.supply.unit_cost or 0) for r in ready)
        lead = lead_days(first.supply)
        cost = f" (about €{total:,.0f})" if total else ""
        if len(ready) == 1:
            title = f"{first.supply.name} {_when(first, day)}"
            body = f"{g.supplier} needs {lead} days to deliver, so now is the time. I can order " \
                   f"{first.suggested:,g} {first.unit}{cost}."
        else:
            names = [r.supply.name for r in ready]
            title = f"{', '.join(names[:-1])} and {names[-1].lower()} run low this week"
            body = f"They all come from {g.supplier}. I can order them together in one delivery{cost}; " \
                   f"{first.supply.name.lower()} {_when(first, day)}."
        out.append(Suggestion(g.supplier, ready, total, title, body))
    return out


@dataclass
class ImportResult:
    updated: list[str]
    unknown: list[str]
    invalid: list[str]


def import_counts(session: Session, org_key: str, counts: list[tuple[str, object]], source: str,
                  by: str) -> ImportResult:
    """Stock counts from a file or another system, matched to items by name. Unknown names are reported, not added."""
    items = {s.name.strip().casefold(): s
             for s in session.scalars(select(Supply).where(Supply.org_key == org_key))}
    result = ImportResult([], [], [])
    for name, raw in counts:
        s = items.get(str(name).strip().casefold())
        if s is None:
            result.unknown.append(str(name).strip()[:120])
            continue
        try:
            _record_count(session, s, _number(raw), source, by)
        except ValueError as e:
            result.invalid.append(f"{s.name}: {e}")
            continue
        result.updated.append(s.name)
    session.commit()
    return result


def save_count(session: Session, org_key: str, counts: dict[int, str], by: str) -> ImportResult:
    """A stock count walked through the storage areas: blank fields mean 'not counted'."""
    items = {s.id: s for s in session.scalars(select(Supply).where(Supply.org_key == org_key))}
    named = [(items[i].name, raw) for i, raw in counts.items() if i in items and str(raw).strip()]
    return import_counts(session, org_key, named, "count", by)


NAME_COLUMNS = ("item", "name", "product", "tuote", "nimike")
COUNT_COLUMNS = ("on hand", "on_hand", "stock", "count", "quantity", "qty", "saldo", "määrä")


def parse_stock_file(data: bytes) -> list[tuple[str, str]]:
    """A stock export from any kitchen system: CSV with comma, semicolon or tab, an item column and a count column."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        raise ValueError("the file is empty")
    head = [h.strip().casefold() for h in rows[0]]
    name_col = next((i for i, h in enumerate(head) if h in NAME_COLUMNS), None)
    count_col = next((i for i, h in enumerate(head) if h in COUNT_COLUMNS), None)
    if name_col is None or count_col is None:
        raise ValueError("the file needs an 'item' column and an 'on hand' column")
    need = max(name_col, count_col)
    return [(r[name_col], r[count_col]) for r in rows[1:1001] if len(r) > need and r[name_col].strip()]


def export_csv(rows: list[Row]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["item", "storage area", "supplier", "unit", "on hand", "daily use", "reorder below", "order up to",
                "unit cost"])
    for r in rows:
        s = r.supply
        w.writerow([s.name, r.area, s.supplier, unit_label(s.unit), f"{s.on_hand:g}", f"{s.daily_use:g}",
                    f"{s.reorder_below:g}", f"{s.order_up_to:g}", f"{s.unit_cost or 0:g}"])
    return out.getvalue()
