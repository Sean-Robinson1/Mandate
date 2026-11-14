"""A kitchen's menu: dishes, their recipes, what each portion costs, and a day's service.

Recipes link dishes to the kitchen's own supplies (the stock sheet), so:
  - a portion's cost uses what the kitchen last paid: Valio Aimo's confirmed price when the agent bought it,
    otherwise the item's list price;
  - a dish's allergens come from its ingredients: what the kitchen declares for each supply, plus what Valio Aimo's
    product data says for the product it last delivered;
  - a day's service (the demo's "next day") uses stock through the recipes, and a dish sells out when one of its
    ingredients runs out mid-service.

Only kitchens listed in MENUS have a menu; the others keep using a typical day's use per item.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Dish, Order, PriceRecord, RecipeLine, Run, Sale, SimState, Special, Supply

VAT = 0.135                 # shortcut: Finnish VAT on restaurant food; drinks with alcohol differ (none on this menu)
FOOD_COST_TARGET = (0.28, 0.35)
# Gross margin each course aims for: drinks and desserts carry more margin than mains, as on real menus.
TARGETS = {"starter": 72.0, "main": 70.0, "dessert": 80.0, "drink": 85.0}
PRICE_STEP = 0.5            # suggested menu prices round up to this
PRICE_RANGE = (1.0, 200.0)  # a menu price outside this is a typo
# Specials that use up stock before its date. shortcut: guests pick a dish on special this many times as often
# (no price elasticity data); see ASSUMPTIONS.md.
SPECIAL_PCT = 20
SPECIAL_BOOST = 2.5
SPECIAL_MIN_AT_RISK = 25.0  # EUR of stock that would be thrown away before a special is worth suggesting
CATEGORIES = {"starter": "Starters", "main": "Mains", "dessert": "Desserts", "drink": "Drinks"}

# name, category, price € with VAT, weight within its category, extras € per portion (oil, seasoning, garnish:
# not stock-tracked), [(supply name, quantity per portion, yield %)]
MENUS: dict[str, list] = {
    "restaurant": [
        ("Sourdough, whipped butter", "starter", 6.0, 1.2, 0.3,
         [("Sourdough bread", 0.08, 100), ("Butter", 0.03, 100)]),
        ("Beetroot and goat cheese salad", "starter", 14.0, 1.0, 0.9,
         [("Beetroot", 0.18, 85), ("Goat cheese", 0.06, 100), ("Lettuce", 0.04, 80)]),
        ("Creamy salmon soup", "main", 17.5, 1.3, 0.8,
         [("Salmon fillet", 0.14, 90), ("Potatoes", 0.15, 85), ("Cream", 0.1, 100), ("Onions", 0.03, 90),
          ("Dill", 0.005, 100), ("Butter", 0.01, 100)]),
        ("Pan-fried salmon, dill potatoes", "main", 27.0, 1.2, 1.4,
         [("Salmon fillet", 0.2, 90), ("Potatoes", 0.2, 85), ("Butter", 0.04, 100), ("Cream", 0.05, 100),
          ("Dill", 0.003, 100)]),
        ("Sautéed reindeer, mash, lingonberries", "main", 33.0, 0.8, 1.2,
         [("Reindeer, sliced", 0.16, 100), ("Potatoes", 0.2, 85), ("Butter", 0.03, 100), ("Milk", 0.05, 100),
          ("Lingonberries, frozen", 0.04, 100), ("Onions", 0.02, 90)]),
        ("Bistro burger and fries", "main", 21.0, 1.5, 1.5,
         [("Beef mince", 0.18, 100), ("Burger buns", 1, 100), ("Grated cheese", 0.03, 100), ("Lettuce", 0.02, 80),
          ("Tomatoes", 0.04, 100), ("Onions", 0.02, 90), ("Potatoes", 0.25, 85)]),
        ("Chanterelle pasta", "main", 22.0, 0.8, 1.0,
         [("Pasta", 0.12, 100), ("Chanterelles, frozen", 0.07, 100), ("Cream", 0.12, 100),
          ("Grated cheese", 0.03, 100), ("Butter", 0.01, 100), ("Onions", 0.02, 90)]),
        ("Chicken Caesar salad", "main", 19.5, 1.0, 1.2,
         [("Chicken breast", 0.16, 100), ("Lettuce", 0.1, 80), ("Grated cheese", 0.03, 100),
          ("Sourdough bread", 0.04, 100), ("Eggs", 1, 100)]),
        ("Crème brûlée", "dessert", 10.0, 1.2, 0.5,
         [("Cream", 0.15, 100), ("Eggs", 2, 100), ("Sugar", 0.03, 100)]),
        ("Blueberry pie, vanilla sauce", "dessert", 9.5, 1.0, 0.4,
         [("Blueberries, frozen", 0.08, 100), ("Wheat flour", 0.05, 100), ("Butter", 0.04, 100),
          ("Sugar", 0.04, 100), ("Eggs", 0.5, 100), ("Cream", 0.06, 100)]),
        ("Coffee", "drink", 3.9, 1.0, 0.25, [("Coffee beans", 0.014, 100), ("Milk", 0.04, 100)]),
    ],
}
# How the guests of a typical day order: covers on a normal day (scaled by the kitchen's week), and the share of
# guests who take a starter, a dessert and a coffee. Everyone takes a main.
SERVICE = {"restaurant": {"covers": 120, "starter": 0.35, "dessert": 0.4, "drink": 0.5,
                          "lunch": (11 * 60, 14 * 60), "dinner": (17 * 60, 22 * 60), "lunch_share": 0.4}}
# What the kitchen declares for each supply (GS1 codes). Valio Aimo's product data is added on top.
DECLARED_ALLERGENS = {"restaurant": {
    "Salmon fillet": ["AF"], "Cream": ["AM"], "Butter": ["AM"], "Milk": ["AM"], "Grated cheese": ["AM"],
    "Goat cheese": ["AM"], "Eggs": ["AE"], "Burger buns": ["AW"], "Sourdough bread": ["AW"], "Pasta": ["AW"],
    "Wheat flour": ["AW"]}}


def has_menu(org_key: str) -> bool:
    return org_key in MENUS


def seed(session: Session) -> None:
    """Give each kitchen with a menu its dishes and recipes (once per kitchen)."""
    for org, dishes in MENUS.items():
        if session.scalar(select(Dish.id).where(Dish.org_key == org).limit(1)) is not None:
            continue
        supplies = {s.name: s for s in session.scalars(select(Supply).where(Supply.org_key == org))}
        if not supplies:
            continue
        for i, (name, category, price, weight, extras, lines) in enumerate(dishes):
            dish = Dish(org_key=org, name=name, category=category, price=price, weight=weight, extras=extras,
                        target_margin_pct=TARGETS.get(category, 70.0), position=i)
            session.add(dish)
            session.flush()
            for supply_name, quantity, yield_pct in lines:
                if supply_name in supplies:
                    session.add(RecipeLine(dish_id=dish.id, supply_id=supplies[supply_name].id, quantity=quantity,
                                           yield_pct=yield_pct))
    session.commit()


# ---------------------------------------------------------------- prices and allergens

@dataclass
class Price:
    per_unit: float
    source: str                 # what the figure is: "Valio Aimo order AIMO-…" or "list price"
    link: str | None = None     # the agent's request, when it bought it


def _measure(lines: list[dict]) -> float:
    return sum(l.get("measure", 0) for l in lines)


def _last_orders(session: Session, supply_ids: list[int]) -> dict[int, tuple[Order, list[dict]]]:
    """Each supply's most recent order the agent placed and Valio Aimo confirmed."""
    runs = dict(session.execute(select(Run.trace_id, Run.supply_id).where(Run.supply_id.in_(supply_ids))).all())
    out: dict[int, tuple[Order, list[dict]]] = {}
    if not runs:
        return out
    for o in session.scalars(select(Order).where(Order.trace_id.in_(list(runs)),
                                                 Order.status.in_(("PLACED", "DELIVERED"))).order_by(Order.created_at)):
        out[runs[o.trace_id]] = (o, json.loads(o.lines_json or "[]"))
    return out


def prices(session: Session, supplies: list[Supply]) -> dict[int, Price]:
    """What each supply costs per unit now, best evidence first: a price Valio Aimo signed for the product in use
    (kept by app/prices.py), the last order Valio Aimo confirmed, the recorded price history, the list price."""
    ids = [s.id for s in supplies]
    state = session.get(SimState, 1)
    today = state.day if state else 1
    latest: dict[int, PriceRecord] = {}
    for r in session.scalars(select(PriceRecord).where(PriceRecord.supply_id.in_(ids), PriceRecord.in_use.is_(True),
                                                       PriceRecord.day <= today)):
        best = latest.get(r.supply_id)
        if best is None or (r.day, r.source == "valio", r.id) > (best.day, best.source == "valio", best.id):
            latest[r.supply_id] = r
    last = _last_orders(session, ids)
    out = {}
    for s in supplies:
        rec, (order, lines) = latest.get(s.id), last.get(s.id, (None, []))
        measure = _measure(lines)
        link = order.trace_id if order is not None else None
        if rec is not None and rec.source == "valio":
            out[s.id] = Price(round(rec.unit_price, 4), "Valio Aimo's signed price", link)
        elif order is not None and measure > 0:
            out[s.id] = Price(round(order.total_net / measure, 4),
                              f"Valio Aimo order {order.supplier_order_id or order.order_id}", link)
        elif rec is not None:
            out[s.id] = Price(round(rec.unit_price, 4), "recent price")
        else:
            out[s.id] = Price(s.unit_cost or 0.0, "list price")
    return out


def allergens(session: Session, supplies: list[Supply]) -> dict[int, set[str]]:
    """Each supply's allergens: the kitchen's declaration plus the last delivered product's data from Valio Aimo."""
    last = _last_orders(session, [s.id for s in supplies])
    out = {}
    for s in supplies:
        codes = set(DECLARED_ALLERGENS.get(s.org_key, {}).get(s.name, []))
        for line in last.get(s.id, (None, []))[1]:
            codes |= set(line.get("contains", []))
        out[s.id] = codes
    return out


# ---------------------------------------------------------------- the menu with costs

@dataclass
class Ingredient:
    supply: Supply
    quantity: float             # on the plate
    used: float                 # taken from stock, after trimming losses
    price: Price
    cost: float


@dataclass
class DishView:
    dish: Dish
    ingredients: list[Ingredient]
    allergens: list[str]
    sold_week: int = 0

    @property
    def cost(self) -> float:
        return round(sum(i.cost for i in self.ingredients) + (self.dish.extras or 0), 2)

    @property
    def net_price(self) -> float:
        return round(self.dish.price / (1 + VAT), 2)

    @property
    def food_cost_pct(self) -> float:
        return round(100 * self.cost / self.net_price, 1) if self.net_price else 0.0

    @property
    def margin(self) -> float:
        return round(self.net_price - self.cost, 2)

    @property
    def tone(self) -> str:
        pct = self.food_cost_pct / 100
        return "ok" if pct <= FOOD_COST_TARGET[1] else "warn" if pct <= FOOD_COST_TARGET[1] + 0.05 else "bad"

    @property
    def margin_pct(self) -> float:
        """Gross margin: what is left of the price before VAT after the ingredients."""
        return round(100 - self.food_cost_pct, 1)

    @property
    def target(self) -> float:
        return self.dish.target_margin_pct or TARGETS.get(self.dish.category, 70.0)

    @property
    def below_target(self) -> bool:
        return self.margin_pct < self.target

    @property
    def suggested_price(self) -> float | None:
        """The lowest menu price that reaches the target margin, when the dish is below it."""
        return suggested_price(self.cost, self.target) if self.below_target else None

    @property
    def profit_week(self) -> float:
        return round(self.margin * self.sold_week, 2)


def suggested_price(cost: float, target_pct: float) -> float:
    """The lowest menu price (with VAT, rounded up to €0.50) that earns the target margin on this portion cost."""
    net = cost / (1 - min(target_pct, 99.0) / 100)
    return math.ceil(net * (1 + VAT) / PRICE_STEP - 1e-9) * PRICE_STEP


def set_menu_price(session: Session, org_key: str, dish_id: int, price, person_id: str) -> tuple[Dish, float]:
    """Change a dish's menu price (with VAT), recorded in the audit log. Returns the dish and its old price.
    Raises ValueError with a message for the person."""
    from app.audit import append_event                      # here: audit is only needed for this write
    dish = session.get(Dish, dish_id)
    if dish is None or dish.org_key != org_key:
        raise ValueError("no such dish on your menu")
    try:
        value = float(str(price).replace("€", "").replace(",", ".").strip())
    except ValueError:
        raise ValueError("enter a price in euros, for example 24.50") from None
    low, high = PRICE_RANGE
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"enter a price between €{low:.2f} and €{high:.2f}")
    old = dish.price
    dish.price, dish.updated_by = round(value, 2), person_id
    append_event(session, trace_id=f"menu-{org_key}", actor=person_id, event_type="menu.price_changed",
                 payload={"dish": dish.name, "old_price": old, "new_price": dish.price})
    session.commit()
    return dish, old


def price_impact(session: Session, org_key: str, views: list[DishView]) -> list[dict]:
    """Recent ingredient price moves (app/prices.py) and what each did to the dishes that use the ingredient."""
    from app import prices as price_history                  # here, not at the top: prices imports kitchen
    out = []
    for a in price_history.overview(session, org_key)["alerts"]:
        affected = []
        for v in views:
            used = sum(i.used for i in v.ingredients if i.supply.id == a["supply_id"])
            if not used or not v.net_price:
                continue
            before = v.cost - used * (a["new_price"] - a["old_price"])
            affected.append({"dish": v.dish.name, "id": v.dish.id, "before": round(100 * (1 - before / v.net_price), 1),
                             "after": v.margin_pct, "week": round(used * (a["new_price"] - a["old_price"]) * v.sold_week, 2),
                             "below": v.below_target, "suggested": v.suggested_price})
        if affected:
            out.append({**a, "dishes": sorted(affected, key=lambda d: -abs(d["week"])),
                        "week": round(sum(d["week"] for d in affected), 2)})
    return sorted(out, key=lambda a: -abs(a["week"]))


def _used(line: RecipeLine) -> float:
    return line.quantity * 100 / (line.yield_pct or 100)


def dishes(session: Session, org_key: str, today: int | None = None) -> list[DishView]:
    """The menu with each dish's recipe, cost per portion at current prices, allergens and last week's sales."""
    menu = session.scalars(select(Dish).where(Dish.org_key == org_key).order_by(Dish.position)).all()
    if not menu:
        return []
    lines = session.scalars(select(RecipeLine).where(RecipeLine.dish_id.in_([d.id for d in menu]))
                            .order_by(RecipeLine.id)).all()
    supplies = {s.id: s for s in session.scalars(select(Supply).where(Supply.org_key == org_key))}
    price = prices(session, list(supplies.values()))
    codes = allergens(session, list(supplies.values()))
    sold: dict[int, int] = {}
    if today is not None:
        for dish_id, quantity in session.execute(select(Sale.dish_id, Sale.quantity).where(
                Sale.org_key == org_key, Sale.served == 1, Sale.day > today - 7, Sale.day <= today)):
            sold[dish_id] = sold.get(dish_id, 0) + quantity
    out = []
    for d in menu:
        ingredients = []
        for l in (l for l in lines if l.dish_id == d.id and l.supply_id in supplies):
            s, used = supplies[l.supply_id], _used(l)
            ingredients.append(Ingredient(s, l.quantity, round(used, 4), price[s.id],
                                          round(used * price[s.id].per_unit, 4)))
        found = sorted({c for i in ingredients for c in codes[i.supply.id]})
        out.append(DishView(d, ingredients, found, sold.get(d.id, 0)))
    return out


# ---------------------------------------------------------------- specials against waste

def special_price(price: float, pct: float) -> float:
    """The discounted menu price, rounded down to 10 cents so it reads like a menu price."""
    return math.floor(price * (1 - pct / 100) * 10 + 1e-9) / 10


def active_specials(session: Session, org_key: str, day: int) -> dict[int, Special]:
    rows = session.scalars(select(Special).where(Special.org_key == org_key, Special.from_day <= day,
                                                 Special.until_day >= day).order_by(Special.id))
    return {r.dish_id: r for r in rows}


@dataclass
class SpecialIdea:
    supply: Supply
    best_before: date
    days: int                   # service days left before it is binned
    surplus: float              # what normal service will not use in time, in the supply's unit
    at_risk: float              # EUR of stock that would be thrown away
    dish: DishView
    per_portion: float
    pct: float
    price: float                # special price, with VAT
    extra_portions: int

    @property
    def margin(self) -> float:
        return round(self.price / (1 + VAT) - self.dish.cost, 2)


def special_ideas(session: Session, org_key: str) -> list[SpecialIdea]:
    """Stock that normal service will not use before its date, and a discounted dish that would use it up.

    Deterministic: the expected use per day (the weekly shape) decides the surplus; a person decides whether to run
    the special. The discount drops to 10% if 20% would sell the dish below its ingredient cost."""
    from app import kitchen                                   # here, not at the top: kitchen imports menu
    day, start = kitchen.calendar(session)
    views = [v for v in dishes(session, org_key, day) if v.dish.active]
    running = {dish_id for d in range(day, day + 8) for dish_id in active_specials(session, org_key, d)}
    leftover: dict[int, tuple[Supply, float, int]] = {}
    for supply, qty, binned in kitchen.waste_forecast(session, org_key):
        _, total, first = leftover.get(supply.id, (supply, 0.0, binned))
        leftover[supply.id] = (supply, total + qty, min(first, binned))
    out = []
    for s, surplus, binned in leftover.values():
        surplus = round(surplus, 2)
        at_risk = round(surplus * (s.unit_cost or 0), 2)
        if at_risk < SPECIAL_MIN_AT_RISK:
            continue
        days = max(1, binned + 1 - day)                       # service days before the closing check bins it
        users = [(v, sum(i.used for i in v.ingredients if i.supply.id == s.id)) for v in views]
        users = [(v, per) for v, per in users if per > 0 and v.dish.id not in running]
        if not users:
            continue
        v, per = max(users, key=lambda u: (u[0].dish.category == "main", u[1]))
        pct = next((p for p in (SPECIAL_PCT, 10) if special_price(v.dish.price, p) / (1 + VAT) > v.cost), None)
        if pct is None:
            continue
        out.append(SpecialIdea(s, kitchen.date_of(start, binned), days, surplus, at_risk, v, round(per, 3), pct,
                               special_price(v.dish.price, pct), math.ceil(surplus / per)))
    return sorted(out, key=lambda i: -i.at_risk)


def start_special(session: Session, org_key: str, dish_id: int, pct: float, days: int, reason: str,
                  person_id: str) -> Special:
    """Run a dish at a discount from tomorrow for `days` service days, recorded in the audit log.
    Raises ValueError with a message for the person."""
    from app import kitchen
    from app.audit import append_event
    dish = session.get(Dish, dish_id)
    if dish is None or dish.org_key != org_key:
        raise ValueError("no such dish on your menu")
    if not (0 < pct <= 50) or not (1 <= days <= 7):
        raise ValueError("a special is 1-50% off for 1-7 days")
    day = kitchen.today(session)
    special = Special(org_key=org_key, dish_id=dish_id, pct=pct, from_day=day + 1, until_day=day + days,
                      reason=reason[:200], by=person_id)
    session.add(special)
    append_event(session, trace_id=f"menu-{org_key}", actor=person_id, event_type="menu.special_started",
                 payload={"dish": dish.name, "pct": pct, "price": special_price(dish.price, pct),
                          "from_day": day + 1, "until_day": day + days, "reason": reason[:200]})
    session.commit()
    return special


# ---------------------------------------------------------------- a day's service

@dataclass
class Ticket:
    number: int
    minute: int
    table: int
    covers: int
    items: list[tuple[str, int]] = field(default_factory=list)       # dish name, portions
    sold_out: list[str] = field(default_factory=list)                 # dishes that ran out on this ticket


def _pick(rng: random.Random, options: list[DishView], boost: frozenset[int] = frozenset()) -> DishView | None:
    weights = [o.dish.weight * (SPECIAL_BOOST if o.dish.id in boost else 1.0) for o in options]
    return rng.choices(options, weights=weights)[0] if options else None


def serve(session: Session, org_key: str, day: int, on: date, week_share: float, stock: dict[int, float]) -> dict[int, float]:
    """Simulate one day's service: guests arrive in tickets, each dish uses its recipe from stock, and a dish
    sells out when an ingredient runs short. Writes the day's sales; returns what each supply used.

    `stock` is each supply's stock at the start of service (after the morning's deliveries). shortcut: a real
    kitchen gets this from its till (POS) export.
    """
    cfg = SERVICE.get(org_key)
    menu = [v for v in dishes(session, org_key) if v.dish.active]
    if not cfg or not menu:
        return {}
    used = {i.supply.id: 0.0 for v in menu for i in v.ingredients}   # every recipe supply, even if unused today
    if week_share <= 0:
        return used
    rng = random.Random(f"{org_key}/service/{on.isoformat()}")
    on_special = active_specials(session, org_key, day)
    boost = frozenset(on_special)

    def price_of(v: DishView) -> float:
        return special_price(v.dish.price, on_special[v.dish.id].pct) if v.dish.id in on_special else v.dish.price

    left = dict(stock)
    off: set[int] = set()                                   # sold out for the rest of the day

    def make(v: DishView) -> bool:
        if any(left.get(i.supply.id, 0.0) + 1e-9 < i.used for i in v.ingredients):
            return False
        for i in v.ingredients:
            left[i.supply.id] = left.get(i.supply.id, 0.0) - i.used
            used[i.supply.id] = used.get(i.supply.id, 0.0) + i.used
        return True

    target = max(1, round(cfg["covers"] * week_share * rng.uniform(0.9, 1.1)))
    sizes, seated = [], 0
    while seated < target:
        n = min(target - seated, rng.choices([1, 2, 3, 4, 6], weights=[15, 45, 15, 20, 5])[0])
        sizes.append(n)
        seated += n
    lunch = round(len(sizes) * cfg["lunch_share"])
    minutes = sorted([rng.randint(*cfg["lunch"]) for _ in range(lunch)] +
                     [rng.randint(*cfg["dinner"]) for _ in range(len(sizes) - lunch)])
    tickets = []
    for number, (minute, covers) in enumerate(zip(minutes, sizes), start=1):
        t = Ticket(number, minute, rng.randint(1, 24), covers)
        wanted: list[DishView] = []
        for _ in range(covers):
            wanted.append(_pick(rng, [v for v in menu if v.dish.category == "main" and v.dish.id not in off], boost))
            for category in ("starter", "dessert", "drink"):
                if rng.random() < cfg[category]:
                    wanted.append(_pick(rng, [v for v in menu if v.dish.category == category and v.dish.id not in off],
                                        boost))
        portions: dict[int, int] = {}
        for v in (w for w in wanted if w is not None):
            if v.dish.id in off:
                continue
            if make(v):
                portions[v.dish.id] = portions.get(v.dish.id, 0) + 1
            else:
                off.add(v.dish.id)
                t.sold_out.append(v.dish.name)
                session.add(Sale(org_key=org_key, day=day, ticket=number, minute=minute, table_no=t.table,
                                 covers=covers, dish_id=v.dish.id, quantity=1, price=price_of(v), cost=v.cost,
                                 served=0))
        for v in menu:
            if v.dish.id in portions:
                session.add(Sale(org_key=org_key, day=day, ticket=number, minute=minute, table_no=t.table,
                                 covers=covers, dish_id=v.dish.id, quantity=portions[v.dish.id], price=price_of(v),
                                 cost=v.cost))
        tickets.append(t)
    return {k: round(v, 3) for k, v in used.items()}


def seed_history(session: Session, org_key: str, days: range, week: tuple, start: date) -> None:
    """Past services for a new demo, so the menu, service and food cost pages have a history from day one.
    Sales only: the seeded stock history already accounts for the stock. shortcut: simulated (ASSUMPTIONS.md)."""
    plenty = {s.id: 1e9 for s in session.scalars(select(Supply).where(Supply.org_key == org_key))}
    for day in days:
        on = start + timedelta(days=day - 1)
        serve(session, org_key, day, on, week[on.weekday()], plenty)
    session.commit()


@dataclass
class DaySummary:
    day: int
    tickets: list[Ticket]
    covers: int
    revenue: float              # with VAT
    net_revenue: float
    food_cost: float            # recipes at the prices of the day
    sold_out: list[tuple[str, str]]      # dish, time it ran out
    top: list[tuple[str, int]]           # most sold dishes

    @property
    def food_cost_pct(self) -> float:
        return round(100 * self.food_cost / self.net_revenue, 1) if self.net_revenue else 0.0


def clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def day_summary(session: Session, org_key: str, day: int) -> DaySummary | None:
    """One day's service as recorded: tickets in time order, takings, food cost and what sold out."""
    sales = session.scalars(select(Sale).where(Sale.org_key == org_key, Sale.day == day)
                            .order_by(Sale.ticket, Sale.id)).all()
    if not sales:
        return None
    names = dict(session.execute(select(Dish.id, Dish.name).where(Dish.org_key == org_key)).all())
    tickets: dict[int, Ticket] = {}
    counts: dict[str, int] = {}
    revenue = cost = 0.0
    sold_out = []
    for s in sales:
        t = tickets.setdefault(s.ticket, Ticket(s.ticket, s.minute, s.table_no, s.covers))
        name = names.get(s.dish_id, "?")
        if s.served:
            t.items.append((name, s.quantity))
            counts[name] = counts.get(name, 0) + s.quantity
            revenue += s.price * s.quantity
            cost += s.cost * s.quantity
        else:
            t.sold_out.append(name)
            sold_out.append((name, clock(s.minute)))
    ordered = sorted(tickets.values(), key=lambda t: (t.minute, t.number))
    return DaySummary(day, ordered, sum(t.covers for t in ordered), round(revenue, 2), round(revenue / (1 + VAT), 2),
                      round(cost, 2), sold_out, sorted(counts.items(), key=lambda kv: -kv[1])[:5])


def last_service_day(session: Session, org_key: str) -> int | None:
    return session.scalar(select(Sale.day).where(Sale.org_key == org_key).order_by(Sale.day.desc()).limit(1))
