"""Food cost and waste: what the kitchen's ingredients cost against what it sells, and what it throws away.

Food cost is ingredient cost as a share of sales before VAT. Two figures:
  - from recipes: the cost of every dish sold, at the prices of the day (what the menu says it should cost);
  - with waste: the same plus everything logged as thrown away.
The gap between the two is the money the waste log explains. The waste itself is recorded elsewhere (the crew's
waste log on the line, and the closing check below); this module only reads it and looks for patterns. (Comparing recipes with stock counts, to find the
gap nobody logged, is a later step: docs/PLAN.md item 4.)
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import settings as config
from app import charts, kitchen, menu
from app.db import Dish, Sale, StockLevel, Supply, Waste

REASONS = {
    "expired": "Past its use-by date",
    "spoiled": "Spoiled or damaged",
    "overprep": "Over-prepared, not sold",
    "dropped": "Dropped or burnt",
    "returned": "Sent back by a guest",
}
PERIOD_DAYS = 7
PAGE_DAYS = 14          # the food cost page looks back two weeks, so a pattern in the waste shows
GOOD_WASTE = 0.04       # a well-run kitchen throws away about 4% of the food it buys (docs/PLAN.md research)
QUIET = 0.85            # a day this far below a normal day (share of the week, kitchen.WEEK) counts as quiet
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


# The demo's past waste tells a story a manager can act on (simulated, see ASSUMPTIONS.md):
#   - the line preps for a normal day every day, so quiet days end with food left over;
#   - salmon (four days' shelf life) bought for the weekend is binned early in the week;
#   - bread, salad and herbs go past their date in small amounts most days;
#   - three bad days with a clear cause.
PREPPED = ("Salmon fillet", "Potatoes", "Beetroot", "Cream", "Lettuce", "Chicken breast", "Beef mince")
SHORT_LIVED = (("Sourdough bread", 0.25), ("Burger buns", 0.2), ("Lettuce", 0.15), ("Dill", 0.3), ("Milk", 0.3))
SPIKES = {   # days before the demo starts: (reason, note, {item: share of a typical day's use})
    10: ("spoiled", "Fridge 2 too warm overnight", {"Cream": 0.7, "Milk": 1.2, "Salmon fillet": 0.4,
                                                    "Goat cheese": 0.8}),
    6: ("dropped", "Tray dropped during service", {"Reindeer, sliced": 0.35}),
    3: ("overprep", "Group of 30 cancelled, their prep binned", {"Salmon fillet": 0.45, "Beetroot": 1.2,
                                                                 "Potatoes": 0.4, "Cream": 0.3,
                                                                 "Reindeer, sliced": 0.4}),
}


def seed_history(session: Session, org_key: str, days: range, crew: list[str]) -> None:
    """Believable waste for the days before the demo starts, so the page has a past to analyse: about 8% of the
    food bought, the high end for a restaurant. Stock is not changed: the seeded stock history already includes
    it. shortcut: simulated (see ASSUMPTIONS.md)."""
    supplies = {s.name: s for s in session.scalars(select(Supply).where(Supply.org_key == org_key,
                                                                        Supply.daily_use > 0))}
    if not supplies:
        return
    _, start = kitchen.calendar(session)
    week = kitchen.WEEK.get(org_key, kitchen.FLAT)

    def bin_it(day, name, share, reason, note, by=CLOSING):
        s = supplies.get(name)
        if s is None:
            return
        amount = _amount(s, s.daily_use * share)
        session.add(Waste(org_key=org_key, supply_id=s.id, day=day, quantity=amount,
                          cost=round(amount * (s.unit_cost or 0), 2), reason=reason, note=note, by=by))

    last = days[-1] if days else 0
    for day in days:
        rng = random.Random(f"{org_key}/waste-history/{day}")
        on = kitchen.date_of(start, day)
        busy = week[on.weekday()]
        if busy == 0:
            continue
        left = 0.1 + 0.8 * max(0.0, 1 - busy)              # prepped for a normal day; quiet days leave more
        note = f"Prepped for a normal day, {on:%A} was quiet" if busy < QUIET else "Left over at close"
        for name in rng.sample(PREPPED, k=4 if busy < QUIET else 2):
            bin_it(day, name, left * rng.uniform(0.7, 1.3), "overprep", note)
        if on.weekday() in (6, 0):                          # Sunday and Monday: the weekend's salmon is past its date
            bin_it(day, "Salmon fillet", rng.uniform(0.15, 0.25), "expired", "Bought for the weekend, past its date")
        for name, share in rng.sample(SHORT_LIVED, k=3):
            bin_it(day, name, share * rng.uniform(0.6, 1.2), "expired", "Best before passed")
        if rng.random() < 0.5:
            bin_it(day, rng.choice(("Beef mince", "Salmon fillet", "Chicken breast")), rng.uniform(0.03, 0.06),
                   "dropped", "Dropped or burnt during service", rng.choice(crew))
        if rng.random() < 0.3:
            bin_it(day, rng.choice(("Beef mince", "Reindeer, sliced")), 0.04, "returned", "Guest sent it back",
                   rng.choice(crew))
        if (spike := SPIKES.get(last + 1 - day)) is not None:
            reason, why, items = spike
            for name, share in items.items():
                bin_it(day, name, share, reason, why, CLOSING if reason != "dropped" else rng.choice(crew))
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
    used = session.execute(select(StockLevel.supply_id, StockLevel.used, Supply.name)
                           .join(Supply, Supply.id == StockLevel.supply_id)
                           .where(Supply.org_key == org_key, Supply.shelf_days > 0, StockLevel.day == day,
                                  StockLevel.used > 0)).all()
    on = kitchen.date_of(kitchen.calendar(session)[1], day)
    busy = kitchen.WEEK.get(org_key, kitchen.FLAT)[on.weekday()]
    quiet = 0.8 * (1 - busy) if busy < QUIET else 0.0       # the line still preps for a normal day, as in the history
    note = f"Prepped for a normal day, {on:%A} was quiet" if quiet else "Left over at close"
    if quiet:
        used = [u for u in used if u.name in PREPPED] or used
    for supply_id, amount, _ in rng.sample(used, k=min(len(used), rng.choice([2, 3]))):
        supply = session.get(Supply, supply_id)
        portion = _amount(supply, min(supply.on_hand, amount * rng.uniform(0.03, 0.06) + supply.daily_use * quiet))
        if 0 < portion <= supply.on_hand:
            out.append(log_waste(session, org_key, supply_id, portion, "overprep", CLOSING, note))
    return out


# ---------------------------------------------------------------- the food cost page

@dataclass
class DayCost:
    day: int
    on: object
    net_sales: float
    recipe_cost: float
    waste: float
    quiet: bool = False                  # a quieter day than usual in this kitchen's week
    why: str = ""                        # the day's biggest waste entry, in words

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
    items: list[dict] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    target: tuple[float, float] = menu.FOOD_COST_TARGET
    good_waste: float = GOOD_WASTE

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

    @property
    def waste_pct(self) -> float:
        """Share of the food bought that was thrown away: waste against waste plus what went into dishes sold."""
        spent = self.recipe_cost + self.waste_cost
        return round(100 * self.waste_cost / spent, 1) if spent else 0.0

    @property
    def waste_per_week(self) -> float:
        return round(7 * self.waste_cost / len(self.days), 2) if self.days else 0.0

    @property
    def avoidable_per_week(self) -> float:
        """What the kitchen would save a week if it wasted only a well-run kitchen's share."""
        spent = self.recipe_cost + self.waste_cost
        extra = self.waste_cost - self.good_waste * spent
        return round(max(0.0, 7 * extra / len(self.days)), 2) if self.days else 0.0

    def waste_chart(self) -> dict:
        """A bar per day's waste, with a well-run kitchen's day as a dashed line. Drawn by the template."""
        W, H, L, T, B = 760, 180, 44, 10, 26
        if not self.days:
            return {}
        good = self.good_waste * (self.recipe_cost + self.waste_cost) / len(self.days)
        ticks = charts.nice_ticks(max([d.waste for d in self.days] + [good]), 3)
        top, step = ticks[-1] or 1, (W - L) / len(self.days)
        y = lambda v: T + (1 - v / top) * (H - T - B)           # noqa: E731
        bars = [{"d": d, "x": round(L + i * step + step * 0.2, 1), "w": round(step * 0.6, 1),
                 "y": round(y(d.waste), 1), "h": round(y(0) - y(d.waste), 1), "mid": round(L + (i + 0.5) * step, 1)}
                for i, d in enumerate(self.days)]
        return {"W": W, "H": H, "L": L, "base": round(y(0), 1), "bars": bars, "good": round(y(good), 1),
                "good_value": round(good, 2), "ticks": [{"y": round(y(v), 1), "v": v} for v in ticks]}

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

    # Each day: was it a quiet one, and what was the biggest loss.
    week = kitchen.WEEK.get(org_key, kitchen.FLAT)
    for d in days:
        d.quiet = week[d.on.weekday()] < QUIET
        biggest = max((w for w in waste if w.day == d.day), key=lambda w: w.cost, default=None)
        if biggest is not None and biggest.supply_id in by_id:
            d.why = biggest.note or REASONS.get(biggest.reason, "")
    days = [d for d in days if d.net_sales or d.waste]

    # What is thrown away: per item, its cost, its share of what left stock, the main reason and the dishes it is in.
    used = dict(session.execute(select(StockLevel.supply_id, func.sum(StockLevel.used))
                                .join(Supply, Supply.id == StockLevel.supply_id)
                                .where(Supply.org_key == org_key, StockLevel.day >= first, StockLevel.day <= day)
                                .group_by(StockLevel.supply_id)).all())
    per_item: dict[int, dict] = {}
    for w in waste:
        row = per_item.setdefault(w.supply_id, {"cost": 0.0, "quantity": 0.0, "reasons": {}})
        row["cost"] += w.cost
        row["quantity"] += w.quantity
        row["reasons"][w.reason] = row["reasons"].get(w.reason, 0.0) + w.cost
    waste_total = sum(r["cost"] for r in per_item.values()) or 1
    items = []
    for k, r in sorted(per_item.items(), key=lambda kv: -kv[1]["cost"]):
        if k not in by_id:
            continue
        main = max(r["reasons"], key=r["reasons"].get)
        items.append({"supply": by_id[k], "cost": round(r["cost"], 2), "quantity": round(r["quantity"], 2),
                      "share": round(100 * r["cost"] / waste_total, 1), "reason": REASONS.get(main, main),
                      "rate": round(100 * r["quantity"] / used[k], 1) if used.get(k) else None,
                      "dishes": sorted({v.dish.name for v in views.values()
                                        if any(i.supply.id == k for i in v.ingredients)})})
    o = Overview(days, dishes, ingredients, [(w, by_id[w.supply_id]) for w in waste if w.supply_id in by_id][:30],
                 by_reason, items[:8])
    o.findings = findings(o)
    return o


def findings(o: Overview) -> list[str]:
    """What the waste says, in plain words: the patterns a manager would look for. Deterministic, no LLM."""
    out = []
    quiet = [d.waste for d in o.days if d.quiet]
    busy = [d.waste for d in o.days if not d.quiet]
    # Medians, so one bad day (a cancelled booking) does not hide or fake the pattern.
    if quiet and busy and statistics.median(quiet) > 1.5 * statistics.median(busy):
        order = "MonTueWedThuFriSatSun"
        names = sorted({f"{d.on:%a}" for d in o.days if d.quiet}, key=order.index)
        out.append(f"Quiet days waste the most: a typical {', '.join(names)} throws away "
                   f"€{statistics.median(quiet):,.0f}, a busier day €{statistics.median(busy):,.0f}. The line preps "
                   f"for a normal day; prepping to the day's forecast would cut most of it.")
    if o.items:
        top = o.items[0]
        rate = f", {top['rate']:g}% of all the {top['supply'].name.lower()} used" if top["rate"] else ""
        out.append(f"{top['supply'].name} is the biggest loss: €{top['cost']:,.0f}{rate}, mostly "
                   f"{top['reason'].lower()}.")
    worst = max(o.days, key=lambda d: d.waste, default=None)
    if worst is not None and o.days and worst.waste > 2 * o.waste_cost / len(o.days):
        out.append(f"Worst day: {worst.on:%a %d %b}, €{worst.waste:,.0f} thrown away. {worst.why}.")
    return out
