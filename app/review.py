"""What an approver needs to decide on an order the agent may not place alone, in plain terms.

Everything here is read from records the kitchen already keeps: the plan and Valio Aimo's signed offer, the stock
sheet, the price history (app/prices.py), past confirmed orders, this week's spend, and the menu. Nothing here
decides anything: the decision is the approver's, and Valio Aimo checks their signature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

import settings as config
from app import agent, kitchen, menu
from app.db import PriceRecord, Run, Supply

USUAL_DAYS = 28             # "what we usually pay" looks back this far
SAME_PRICE_PCT = 3.0        # closer than this to the usual price counts as "about the same"


@dataclass
class Brief:
    quantity: float
    unit: str                           # display unit: L, kg, pcs
    product: str                        # what would be ordered, in words
    total: float                        # before VAT
    total_with_tax: float
    price: float | None                 # € per unit, this order
    usual: float | None = None          # € per unit, average paid recently
    usual_from: str = ""                # where the usual price comes from, in words
    diff_pct: float | None = None       # this order versus usual, + is dearer
    delivery: date | None = None
    deadline: date | None = None
    supply: Supply | None = None
    on_hand: float | None = None
    lasts_days: int | None = None
    runs_out: date | None = None
    weeks_of_use: float | None = None
    week_spent: float | None = None     # already this week, before this order
    week_limit: float | None = None     # what the agent may spend alone in a week
    order_limit: float | None = None    # what the agent may spend alone on one order
    dishes: list[str] = field(default_factory=list)
    if_declined: str = ""

    @property
    def price_tone(self) -> str:
        if self.diff_pct is None or abs(self.diff_pct) < SAME_PRICE_PCT:
            return ""
        return "ok" if self.diff_pct < 0 else "warn"

    @property
    def price_words(self) -> str:
        if self.diff_pct is None:
            return "No earlier price to compare with"
        if abs(self.diff_pct) < SAME_PRICE_PCT:
            return "About what you usually pay"
        return f"{abs(self.diff_pct):.0f}% {'below' if self.diff_pct < 0 else 'above'} what you usually pay"

    @property
    def on_time(self) -> bool | None:
        return None if not (self.delivery and self.deadline) else self.delivery <= self.deadline

    @property
    def week_after(self) -> float | None:
        return None if self.week_spent is None else round(self.week_spent + self.total, 2)


def _usual_price(session: Session, supply: Supply, today: int) -> tuple[float | None, str]:
    """The average price paid for this item recently: confirmed Valio Aimo prices and the recorded price history
    of the product in use, over the last four weeks."""
    rows = session.scalars(select(PriceRecord).where(
        PriceRecord.supply_id == supply.id, PriceRecord.in_use.is_(True),
        PriceRecord.day <= today, PriceRecord.day > today - USUAL_DAYS)).all()
    if rows:
        by_day: dict[int, float] = {}
        for r in sorted(rows, key=lambda r: (r.source == "valio", r.id)):   # a signed price wins on its day
            by_day[r.day] = r.unit_price
        return round(sum(by_day.values()) / len(by_day), 2), f"average over the last {USUAL_DAYS // 7} weeks"
    price = menu.prices(session, [supply])[supply.id]
    if price.per_unit:
        return round(price.per_unit, 2), "the last price paid" if price.link else "the list price"
    return None, ""


def brief(session: Session, run: Run, plan: dict | None, intent: dict | None, mandate) -> Brief | None:
    if not plan or not plan.get("lines"):
        return None
    lines = plan["lines"]
    unit_code = plan.get("unit") or (intent or {}).get("unit") or ""
    quantity = plan.get("covered_quantity") or sum(l.get("measure", 0) for l in lines)
    total = plan.get("total_net") or run.total_net or 0.0
    product = lines[0]["name"] if len(lines) == 1 else f"{lines[0]['name']} and {len(lines) - 1} more"
    deliveries = [date.fromisoformat(l["delivery_date"]) for l in lines if l.get("delivery_date")]
    b = Brief(quantity=round(quantity, 2), unit=kitchen.unit_label(unit_code), product=product, total=round(total, 2),
              total_with_tax=round(plan.get("total_with_tax") or 0.0, 2),
              price=round(total / quantity, 2) if quantity else None,
              delivery=max(deliveries) if deliveries else None,
              deadline=date.fromisoformat(intent["deadline"]) if intent and intent.get("deadline") else None)

    org = agent.ORG_OF.get(run.mandate_id)
    supply = session.get(Supply, run.supply_id) if run.supply_id else None
    if supply is None and org and intent:
        supply = kitchen.match_supply(session, org, intent.get("product_terms", []), unit_code)
    day, start = kitchen.calendar(session)
    if supply is not None:
        b.supply = supply
        b.usual, b.usual_from = _usual_price(session, supply, day)
        if b.usual and b.price:
            b.diff_pct = round(100 * (b.price - b.usual) / b.usual, 1)
        row = next((r for r in kitchen.sheet(session, supply.org_key) if r.supply.id == supply.id), None)
        if row is not None:
            b.on_hand, b.lasts_days, b.runs_out = supply.on_hand, row.lasts, row.runs_out_on
        week = sum(kitchen.WEEK.get(supply.org_key, kitchen.FLAT)) * supply.daily_use
        if week > 0:
            b.weeks_of_use = round(quantity / week, 1)
        b.dishes = [v.dish.name for v in menu.dishes(session, supply.org_key)
                    if any(i.supply.id == supply.id for i in v.ingredients)]
        if b.runs_out:
            b.if_declined = f"Nothing is ordered, and {supply.name.lower()} is expected to run out on " \
                            f"{b.runs_out:%A} {b.runs_out.day} {b.runs_out:%B} unless it is ordered another way."
    if not b.if_declined:
        b.if_declined = "Nothing is ordered. The agent will not try again by itself."
    if mandate is not None:
        b.week_spent = agent.week_spend(session, mandate.mandate_id, datetime.now(config.HELSINKI))
        b.week_limit = mandate.limits.per_week_auto_eur
        b.order_limit = mandate.limits.per_order_auto_eur
    return b
