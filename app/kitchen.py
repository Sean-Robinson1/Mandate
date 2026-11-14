"""Each kitchen's supplies sheet, and the simulated days that move it.

Who edits what, like a real kitchen:
  - anyone in the kitchen records stock counts and daily use;
  - the purchasing manager sets the levels and each row's rule:
      auto: when stock runs low the agent restocks on its own, if the mandate allows;
      ask:  the agent prepares the order, and the purchasing manager approves it first.

The demo's 'next day' button uses a day of stock, delivers what is due, and lets the agent react to what is low.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

import settings as config
from app.db import Order, Run, SimState, Supply

OPEN = ("WORKING", "DECIDING", "NEEDS_HUMAN")
CREW_FIELDS = {"on_hand", "daily_use"}                     # anyone in the kitchen
MANAGER_FIELDS = {"reorder_below", "order_up_to", "rule"}  # the purchasing manager

DEFAULT_SUPPLIES = {
    "hotel": [
        ("Lactose-free milk", "lactose-free milk", "L", 760, 220, 600, 1600, "auto"),
        ("Cream", "cream", "L", 110, 25, 80, 250, "auto"),
        ("Butter", "butter", "KG", 34, 6, 25, 80, "ask"),
        ("Oat drink", "oat drink", "L", 70, 15, 40, 150, "auto"),
    ],
    "school": [
        ("Lactose-free milk", "lactose-free milk", "L", 260, 90, 200, 600, "auto"),
        ("Butter", "butter", "KG", 14, 2, 10, 30, "auto"),
        ("Oat drink", "oat drink", "L", 35, 10, 25, 80, "ask"),
    ],
}


def seed(session: Session) -> None:
    """Give each kitchen its starting sheet and start the simulated calendar (first run only)."""
    if session.get(SimState, 1) is None:
        session.add(SimState(id=1, day=1))
    if session.scalar(select(Supply.id).limit(1)) is None:
        for org, rows in DEFAULT_SUPPLIES.items():
            for i, (name, ask_as, unit, on_hand, use, low, up_to, rule) in enumerate(rows):
                session.add(Supply(org_key=org, name=name, ask_as=ask_as, unit=unit, on_hand=on_hand, daily_use=use,
                                   reorder_below=low, order_up_to=up_to, rule=rule, position=i))
    session.commit()


def today(session: Session) -> int:
    state = session.get(SimState, 1)
    return state.day if state else 1


# ---------------------------------------------------------------- reading the sheet

def _measure(order: Order) -> float:
    return round(sum(line.get("measure", 0) for line in json.loads(order.lines_json or "[]")), 2)


@dataclass
class Row:
    supply: Supply
    on_order: float
    arrives_day: int | None
    open_run: Run | None
    delivered_today: float
    days_left: float | None
    status: str
    tone: str                  # ok | warn | bad | busy
    link: str | None           # the request to open from the status cell

    @property
    def low(self) -> bool:
        return self.supply.on_hand + self.on_order < self.supply.reorder_below


def sheet(session: Session, org_key: str, approver_name: str = "the approver") -> list[Row]:
    supplies = session.scalars(select(Supply).where(Supply.org_key == org_key).order_by(Supply.position)).all()
    ids = [s.id for s in supplies]
    runs = session.scalars(select(Run).where(Run.supply_id.in_(ids)).order_by(Run.created_at)).all() if ids else []
    orders = {o.trace_id: o for o in session.scalars(select(Order).where(Order.trace_id.in_([r.trace_id for r in runs])))}
    day = today(session)
    rows = []
    for s in supplies:
        mine = [r for r in runs if r.supply_id == s.id]
        open_run = next((r for r in reversed(mine) if r.status in OPEN), None)
        placed = [orders[r.trace_id] for r in mine if r.trace_id in orders and orders[r.trace_id].status == "PLACED"]
        delivered = [orders[r.trace_id] for r in mine if r.trace_id in orders
                     and orders[r.trace_id].status == "DELIVERED" and orders[r.trace_id].arrives_on_day == day]
        with_order = [r for r in mine if r.trace_id in orders and orders[r.trace_id].status in ("PLACED", "DELIVERED")]
        link = (open_run or (with_order[-1] if with_order else None) or (mine[-1] if mine else None))
        on_order = round(sum(_measure(o) for o in placed), 2)
        arrives = min((o.arrives_on_day for o in placed if o.arrives_on_day), default=None)
        unit = s.unit
        if open_run and open_run.status == "NEEDS_HUMAN":
            status, tone = f"Waiting for {approver_name}", "warn"
        elif open_run:
            status, tone = "Agent is ordering…", "busy"
        elif placed:
            when = f"day {arrives}" if arrives else "soon"
            status, tone = f"{on_order:,g} {unit} on the way, arrives {when}", "ok"
        elif s.on_hand < s.reorder_below:
            status, tone = "Low", "bad"
        else:
            status, tone = "OK", "ok"
        rows.append(Row(supply=s, on_order=on_order, arrives_day=arrives, open_run=open_run,
                        delivered_today=round(sum(_measure(o) for o in delivered), 2),
                        days_left=round(s.on_hand / s.daily_use, 1) if s.daily_use > 0 else None,
                        status=status, tone=tone, link=link.trace_id if link else None))
    return rows


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
    step = 10 if s.unit == "L" else 5
    missing = s.order_up_to - s.on_hand - row.on_order
    return float(math.ceil(missing / step) * step) if missing > 0 else 0.0


def restock_text(supply: Supply, quantity: float, on: date | None = None) -> str:
    """The request the agent writes for itself, in the same words a person would use."""
    on = on or datetime.now(config.HELSINKI).date()
    by = on + timedelta(days=3)
    unit = "litres" if supply.unit == "L" else "kg"
    return f"Restock {quantity:,g} {unit} of {supply.ask_as} by {by:%A}"


def due_restocks(session: Session, org_key: str) -> list[Restock]:
    """Rows below their reorder level with nothing already on the way or being decided."""
    out = []
    for row in sheet(session, org_key):
        s = row.supply
        if row.open_run or not row.low:
            continue
        quantity = restock_quantity(row)
        if quantity <= 0:
            continue
        out.append(Restock(
            supply_id=s.id, org_key=org_key, text=restock_text(s, quantity),
            trigger=f"{s.name} is low: {s.on_hand:,g} {s.unit} left"
                    + (f" and {row.on_order:,g} on the way" if row.on_order else "")
                    + f", reorder level {s.reorder_below:,g} {s.unit}",
            review_reason=None if s.rule == "auto" else
            f"The kitchen set {s.name.lower()} to 'ask first', so restocking it needs approval."))
    return out


# ---------------------------------------------------------------- time passing

def next_day(session: Session) -> int:
    """One kitchen day: deliveries that are due arrive, and the day's use comes out of stock."""
    state = session.get(SimState, 1)
    day = state.day
    # Orders confirmed since the last tick get their arrival day: the real lead time, at least one day.
    for order in session.scalars(select(Order).where(Order.status == "PLACED", Order.arrives_on_day.is_(None))):
        placed = date.fromisoformat(order.created_at[:10])
        lead = (date.fromisoformat(order.delivery_date) - placed).days if order.delivery_date else 1
        order.arrives_on_day = day + max(1, lead)
    state.day = day = day + 1

    supply_of = dict(session.execute(select(Run.trace_id, Run.supply_id).where(Run.supply_id.is_not(None))).all())
    for order in session.scalars(select(Order).where(Order.status == "PLACED", Order.arrives_on_day <= day)):
        order.status = "DELIVERED"
        supply = session.get(Supply, supply_of.get(order.trace_id)) if supply_of.get(order.trace_id) else None
        if supply is not None:
            supply.on_hand = round(supply.on_hand + _measure(order), 2)
    for supply in session.scalars(select(Supply)):
        supply.on_hand = round(max(0.0, supply.on_hand - supply.daily_use), 2)
    session.commit()
    return day


def update_cell(session: Session, supply_id: int, field: str, value: str, person_id: str, is_manager: bool) -> Supply:
    """Change one cell. Raises PermissionError or ValueError with a message for the person."""
    supply = session.get(Supply, supply_id)
    if supply is None:
        raise ValueError("no such row")
    if field not in CREW_FIELDS | MANAGER_FIELDS:
        raise ValueError("that column cannot be edited")
    if field in MANAGER_FIELDS and not is_manager:
        raise PermissionError("only the purchasing manager can change levels and rules")
    if field == "rule":
        if value not in ("auto", "ask"):
            raise ValueError("rule must be auto or ask")
        supply.rule = value
    else:
        try:
            number = float(str(value).replace(",", ".").replace(" ", ""))
        except ValueError:
            raise ValueError("enter a number") from None
        if not math.isfinite(number) or number < 0:
            raise ValueError("enter a number of zero or more")
        low = number if field == "reorder_below" else supply.reorder_below
        up_to = number if field == "order_up_to" else supply.order_up_to
        if up_to <= low:
            raise ValueError("'order up to' must be above 'reorder below'")
        setattr(supply, field, round(number, 2))
    supply.updated_by = person_id
    session.commit()
    return supply
