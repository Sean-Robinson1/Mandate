"""Food cost and waste: what the kitchen's ingredients cost against what it sells, and what it throws away.

Food cost is ingredient cost as a share of sales before VAT. Two figures:
  - from recipes: the cost of every dish sold, at the prices of the day (what the menu says it should cost);
  - with waste: the same plus everything logged as thrown away.
The gap between the two is the money the waste log explains. (Comparing recipes with stock counts, to find the
gap nobody logged, is a later step: docs/PLAN.md item 4.)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

import settings as config
from app import kitchen, menu
from app.db import Dish, Sale, StockLevel, Supply, Waste

REASONS = {
    "expired": "Past its use-by date",
    "spoiled": "Spoiled or damaged",
    "overprep": "Over-prepared, not sold",
    "dropped": "Dropped or burnt",
    "returned": "Sent back by a guest",
}
PERIOD_DAYS = 7
CLOSING = "closing check"


def _price(session: Session, supply: Supply) -> float:
    return menu.prices(session, [supply])[supply.id].per_unit


def _take_from_stock(session: Session, supply: Supply, quantity: float, day: int) -> None:
    supply.on_hand = round(max(0.0, supply.on_hand - quantity), 3)
    level = session.scalar(select(StockLevel).where(StockLevel.supply_id == supply.id, StockLevel.day == day)
                           .order_by(StockLevel.id.desc()).limit(1))
    if level is None:
        session.add(StockLevel(supply_id=supply.id, day=day, on_hand=supply.on_hand, used=round(quantity, 3),
                               source="day"))
    else:
        level.on_hand, level.used = supply.on_hand, round(level.used + quantity, 3)


def log_waste(session: Session, org_key: str, supply_id: int, quantity, reason: str, by: str,
              note: str = "") -> Waste:
    """Someone threw stock away: it leaves stock and is costed at today's price. Raises ValueError on bad input."""
    supply = session.get(Supply, supply_id)
    if supply is None or supply.org_key != org_key:
        raise ValueError("no such item in your kitchen")
    if reason not in REASONS:
        raise ValueError("choose a reason")
    amount = kitchen._number(quantity)
    if amount <= 0:
        raise ValueError("enter how much was thrown away")
    if amount > supply.on_hand + 1e-9:
        raise ValueError(f"there is only {supply.on_hand:g} {kitchen.unit_label(supply.unit)} of "
                         f"{supply.name.lower()} in stock")
    day = kitchen.today(session)
    w = Waste(org_key=org_key, supply_id=supply.id, day=day, quantity=round(amount, 3),
              cost=round(amount * _price(session, supply), 2), reason=reason, note=note.strip()[:200], by=by,
              at=datetime.now(config.HELSINKI).replace(microsecond=0).isoformat())
    session.add(w)
    _take_from_stock(session, supply, amount, day)
    session.commit()
    return w


def _amount(supply: Supply, raw: float) -> float:
    """A believable thrown-away amount: whole pieces, otherwise two decimals."""
    return float(max(1, round(raw))) if supply.unit == "PCS" else round(raw, 2)


# Past waste for a new demo: (reason, weight, share of a day's use thrown away)
HISTORY_REASONS = [("expired", 4, (0.15, 0.4)), ("overprep", 3, (0.05, 0.12)), ("spoiled", 1.5, (0.15, 0.4)),
                   ("dropped", 1, (0.02, 0.05)), ("returned", 0.5, (0.02, 0.04))]
HISTORY_NOTES = {"expired": "Best before passed", "overprep": "Left over at close", "spoiled": "Found at the stock count",
                 "dropped": "Dropped during service", "returned": "Guest sent it back"}


def seed_history(session: Session, org_key: str, days: range, crew: list[str]) -> None:
    """Believable waste for the days before the demo starts, so the page has a past. Stock is not changed: the
    seeded stock history already includes it. shortcut: simulated (see ASSUMPTIONS.md)."""
    supplies = session.scalars(select(Supply).where(Supply.org_key == org_key, Supply.shelf_days > 0,
                                                    Supply.daily_use > 0)).all()
    if not supplies:
        return
    for day in days:
        rng = random.Random(f"{org_key}/waste-history/{day}")
        for _ in range(rng.choice([1, 2, 2, 3, 3, 4])):
            reason, _, (lo, hi) = rng.choices(HISTORY_REASONS, weights=[r[1] for r in HISTORY_REASONS])[0]
            s = rng.choice(supplies)
            amount = _amount(s, s.daily_use * rng.uniform(lo, hi))
            by = CLOSING if reason in ("expired", "overprep") else rng.choice(crew)
            session.add(Waste(org_key=org_key, supply_id=s.id, day=day, quantity=amount,
                              cost=round(amount * (s.unit_cost or 0), 2), reason=reason, note=HISTORY_NOTES[reason],
                              by=by))
    session.commit()


def close_day(session: Session, org_key: str, day: int) -> list[Waste]:
    """The closing check after service: stock past its use-by date is binned, and a little over-prepared food is
    thrown away. shortcut: simulated; in a real kitchen the closing crew logs this on a tablet."""
    out = []
    for b in kitchen.use_first(session, org_key):
        if b.days_left < 0 and b.quantity > 0:
            out.append(log_waste(session, org_key, b.supply.id, min(b.quantity, b.supply.on_hand), "expired", CLOSING,
                                 f"Best before {b.best_before:%d %b}"))
    rng = random.Random(f"{org_key}/waste/{day}")
    used = session.execute(select(StockLevel.supply_id, StockLevel.used).join(Supply, Supply.id == StockLevel.supply_id)
                           .where(Supply.org_key == org_key, Supply.shelf_days > 0, StockLevel.day == day,
                                  StockLevel.used > 0)).all()
    for supply_id, amount in rng.sample(used, k=min(len(used), rng.choice([1, 2]))):
        supply = session.get(Supply, supply_id)
        portion = _amount(supply, amount * rng.uniform(0.02, 0.05))
        if 0 < portion <= supply.on_hand:
            out.append(log_waste(session, org_key, supply_id, portion, "overprep", CLOSING, "Left over at close"))
    return out


# ---------------------------------------------------------------- the food cost page

@dataclass
class DayCost:
    day: int
    on: object
    net_sales: float
    recipe_cost: float
    waste: float

    @property
    def pct(self) -> float | None:
        return round(100 * self.recipe_cost / self.net_sales, 1) if self.net_sales else None

    @property
    def pct_with_waste(self) -> float | None:
        return round(100 * (self.recipe_cost + self.waste) / self.net_sales, 1) if self.net_sales else None


@dataclass
class Overview:
    days: list[DayCost]
    dishes: list[dict]
    ingredients: list[dict]
    waste: list[tuple[Waste, Supply]]
    by_reason: list[tuple[str, float, int]]
    target: tuple[float, float] = menu.FOOD_COST_TARGET
    supplies: list[Supply] = field(default_factory=list)

    @property
    def net_sales(self) -> float:
        return round(sum(d.net_sales for d in self.days), 2)

    @property
    def recipe_cost(self) -> float:
        return round(sum(d.recipe_cost for d in self.days), 2)

    @property
    def waste_cost(self) -> float:
        return round(sum(d.waste for d in self.days), 2)

    @property
    def pct(self) -> float | None:
        return round(100 * self.recipe_cost / self.net_sales, 1) if self.net_sales else None

    @property
    def pct_with_waste(self) -> float | None:
        return round(100 * (self.recipe_cost + self.waste_cost) / self.net_sales, 1) if self.net_sales else None

    def tone(self, pct: float | None) -> str:
        if pct is None:
            return ""
        return "ok" if pct <= self.target[1] * 100 else "warn" if pct <= self.target[1] * 100 + 5 else "bad"


def overview(session: Session, org_key: str, period: int = PERIOD_DAYS) -> Overview:
    day, start = kitchen.calendar(session)
    first = day - period + 1
    sales = session.scalars(select(Sale).where(Sale.org_key == org_key, Sale.served == 1, Sale.day >= first,
                                               Sale.day <= day)).all()
    waste = session.scalars(select(Waste).where(Waste.org_key == org_key, Waste.day >= first, Waste.day <= day)
                            .order_by(Waste.day.desc(), Waste.id.desc())).all()
    supplies = session.scalars(select(Supply).where(Supply.org_key == org_key).order_by(Supply.position)).all()
    by_id = {s.id: s for s in supplies}

    days = []
    for d in range(first, day + 1):
        mine = [s for s in sales if s.day == d]
        days.append(DayCost(d, kitchen.date_of(start, d), round(sum(s.price * s.quantity for s in mine) / (1 + menu.VAT), 2),
                            round(sum(s.cost * s.quantity for s in mine), 2),
                            round(sum(w.cost for w in waste if w.day == d), 2)))

    names = dict(session.execute(select(Dish.id, Dish.name).where(Dish.org_key == org_key)).all())
    per_dish: dict[int, dict] = {}
    for s in sales:
        row = per_dish.setdefault(s.dish_id, {"name": names.get(s.dish_id, "?"), "sold": 0, "net": 0.0, "cost": 0.0})
        row["sold"] += s.quantity
        row["net"] += s.price * s.quantity / (1 + menu.VAT)
        row["cost"] += s.cost * s.quantity
    dishes = sorted(({**r, "net": round(r["net"], 2), "cost": round(r["cost"], 2),
                      "pct": round(100 * r["cost"] / r["net"], 1) if r["net"] else 0.0} for r in per_dish.values()),
                    key=lambda r: -r["cost"])

    # Where the ingredient money goes: today's recipes and prices times the portions sold in the period.
    views = {v.dish.id: v for v in menu.dishes(session, org_key)}
    spend: dict[int, float] = {}
    for s in sales:
        for i in (views[s.dish_id].ingredients if s.dish_id in views else []):
            spend[i.supply.id] = spend.get(i.supply.id, 0.0) + i.cost * s.quantity
    total = sum(spend.values()) or 1
    ingredients = [{"supply": by_id[k], "spend": round(v, 2), "share": round(100 * v / total, 1)}
                   for k, v in sorted(spend.items(), key=lambda kv: -kv[1]) if k in by_id][:8]

    reasons: dict[str, list[float]] = {}
    for w in waste:
        reasons.setdefault(w.reason, []).append(w.cost)
    by_reason = sorted(((REASONS.get(k, k), round(sum(v), 2), len(v)) for k, v in reasons.items()), key=lambda r: -r[1])
    return Overview([d for d in days if d.net_sales or d.waste], dishes, ingredients,
                    [(w, by_id[w.supply_id]) for w in waste if w.supply_id in by_id][:25], by_reason,
                    supplies=list(supplies))
