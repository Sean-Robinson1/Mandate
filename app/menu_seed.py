"""The demo restaurant's menu, recipes, past sales and ingredient prices. All of it is simulated (see ASSUMPTIONS.md).

Bistro Satama's menu is about 25 dishes. Recipes use only the restaurant's stock items, in their own units, per portion.
Sales follow the bistro's week (quiet Mondays, busy weekends) and are calibrated so that recipe use per day is close to
each item's typical daily use on the stock board. Prices start at each item's unit cost and stay put except for four
deliberate moves, so the price-change alerts have something real to show:

  Cream +12% from day -2, Emmental +8% from day -6, Salmon +9% from day -4, Butter -6% from day -10.

Valio Aimo items also carry one or two alternative catalog products (real ids and names from the catalog index) at
their own simulated prices. Everything is deterministic: the same seed gives the same restaurant every run.
"""
from __future__ import annotations

import random
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Dish, PriceRecord, RecipeLine, Sale, Supply

ORG = "bistro"

M, C, B, O, CF, E, Y, EG = ("Lactose-free milk", "Cream", "Butter", "Oat drink", "Crème fraîche", "Emmental cheese",
                            "Greek yoghurt", "Eggs")
S, BF, CH, MU, L, LI, P, F, R, CO = ("Salmon fillet", "Beef sirloin", "Chicken thigh", "Mushrooms", "Lettuce",
                                     "Lingonberries", "Potatoes", "Wheat flour", "Rye bread", "Coffee beans")

# Gross margin each course aims for: drinks and desserts carry more margin than mains, as on real menus.
TARGETS = {"Starter": 72.0, "Main": 70.0, "Dessert": 80.0, "Drink": 85.0}

# name, course, menu price with VAT, portions on a typical day (the week pattern scales it), recipe per portion
MENU = [
    ("Creamy salmon soup", "Starter", 13.90, 9, {S: .12, C: .12, P: .15, B: .02, M: .10, R: .05}),
    ("Forest mushroom soup", "Starter", 10.90, 6, {MU: .12, C: .14, B: .03, M: .10, R: .06, E: .02}),
    ("Smoked salmon on rye", "Starter", 12.50, 5, {S: .10, CF: .05, R: .08, B: .015, L: .03, Y: .03}),
    ("Chicken Caesar salad", "Starter", 14.50, 6, {CH: .14, L: .12, E: .05, EG: 1, R: .05, CF: .04, Y: .03}),
    ("Beef tartare", "Starter", 17.50, 4, {BF: .14, EG: 1, R: .06, B: .01, L: .02}),
    ("Pan-fried salmon, butter sauce", "Main", 26.50, 8, {S: .22, B: .05, C: .08, P: .30, L: .04}),
    ("Sirloin steak, pepper sauce", "Main", 29.50, 5, {BF: .25, C: .08, B: .04, P: .30, MU: .03}),
    ("Chicken thigh, mushroom cream", "Main", 21.50, 6, {CH: .22, MU: .05, C: .12, P: .30, B: .03, E: .02}),
    ("Creamy chicken & mushroom pasta", "Main", 17.90, 6, {F: .12, EG: 1, C: .20, MU: .05, CH: .10, E: .05, B: .02}),
    ("Mushroom & Emmental omelette", "Main", 15.50, 3, {EG: 3, MU: .06, E: .08, B: .03, L: .05, R: .05}),
    ("Salmon & potato gratin", "Main", 17.90, 4, {S: .18, P: .30, C: .18, E: .06, M: .05, B: .02}),
    ("Chicken schnitzel, lingonberries", "Main", 20.50, 5, {CH: .18, F: .04, EG: 1, R: .06, B: .04, LI: .03, P: .30}),
    ("Potato rösti, salmon & crème fraîche", "Main", 18.50, 3, {P: .30, S: .08, CF: .08, EG: 1, B: .03, L: .03}),
    ("Beef & potato hash, fried egg", "Main", 17.50, 3, {BF: .12, P: .30, EG: 1, B: .03, CF: .04}),
    ("Oat-cream mushroom stew", "Main", 16.50, 3, {MU: .12, O: .25, P: .30, R: .06, L: .03}),
    ("Vanilla crème brûlée", "Dessert", 9.50, 6, {C: .15, EG: 2, M: .04}),
    ("Pancakes, lingonberries & cream", "Dessert", 9.00, 6, {F: .07, M: .20, EG: 1, B: .02, LI: .04, C: .05}),
    ("Yoghurt panna cotta", "Dessert", 9.50, 6, {Y: .16, C: .08, LI: .03}),
    ("Brown butter financier", "Dessert", 8.50, 4, {B: .06, F: .05, EG: 1, CF: .02}),
    ("Baked cheesecake", "Dessert", 10.50, 5, {CF: .08, Y: .10, C: .05, EG: 1, B: .03, F: .04}),
    ("Lingonberry whipped porridge", "Dessert", 8.00, 3, {LI: .06, F: .05, M: .15, C: .03}),
    ("Filter coffee", "Drink", 3.90, 35, {CO: .012, M: .03}),
    ("Cappuccino", "Drink", 5.20, 26, {CO: .016, M: .18}),
    ("Oat latte", "Drink", 5.60, 13, {CO: .016, O: .22}),
    ("Glass of milk", "Drink", 2.90, 10, {M: .25}),
]

# Deliberate price moves: (first day of the new price, factor on the unit cost).
MOVES = {C: (-2, 1.12), E: (-6, 1.08), S: (-4, 1.09), B: (-10, 0.94)}

# Valio Aimo items: the product bought today, then alternatives as (catalog id, name if not in the index, price factor).
PRODUCTS = {
    M: [("464398", "Valio Eila semi skimmed milk drink 1 l UHT lactose free", 1.0),
        ("469363", "Arla Semi skimmed milkdrink 1 L UHT lactose free Suomesta", 0.95),
        ("465039", "Valio Eila semi skimmed milk drink 1 l lactose free", 1.06)],
    C: [("463366", "Valio whipping cream 38 % 10 l UHT lactose free", 1.0),
        ("461273", "Valio whipping cream 20 l novobox lactose free", 0.92),
        ("463299", "Millac Gold Cooking & Whipping 33 lactose-free blend of vegetable fat and cream UHT 1L", 0.78)],
    B: [("464411", "Valio butter 500 g unsalted", 1.0),
        ("476815", "20 x Arla normal salted low lactose butter 500 g", 0.85),
        ("463437", "Valio Eila butter 500 g salted lactose free", 1.08)],
    O: [("469512", "Elovena 1l oat drink barista", 1.0),
        ("468465", "Oddlygood oat drink 1l UHT gluten free", 0.94)],
    CF: [("465122", "AIMO CREME FRAICHE 2KG 28% LACTOSE FREE", 1.0),
         ("465189", "Arla Pro 5 kg lactose free 28% Creme Fraiche", 0.95)],
    E: [("467659", "Kuusamon Juusto red label Emmental cheese slices 1 kg", 1.0),
        ("466302", "Porlammin 2kg Grated Emmental-Edam Cheese", 0.82),
        ("465423", "4 x Arla Pro 1,5 kg organic emmental 29% shredded cheese", 1.14)],
    Y: [("464615", "Valio A+ greek natural yoghurt 1 kg lactose free", 1.0),
        ("462718", "Valio Turkish cooking yoghurt 2 kg lactose free", 0.86)],
}


def _week():
    from app import kitchen
    return kitchen.WEEK.get(ORG, kitchen.FLAT)


def _date_of(start: date, day: int) -> date:
    from app import kitchen
    return kitchen.date_of(start, day)


def _portions(dish: Dish, base: float, day: int, on: date) -> int:
    """One dish's simulated portions on one day: the typical day, the weekday pattern, and some noise."""
    rng = random.Random(f"satama-sales/{dish.name}/{day}")
    return max(0, round(base * _week()[on.weekday()] * rng.uniform(0.75, 1.25)))


def _wobble(supply_name: str, product_id: str, day: int) -> float:
    """Small week-to-week drift (under 1.5%), so price lines are not perfectly flat; well under the alert threshold."""
    week = (day + 100) // 7
    return 1 + random.Random(f"satama-price/{supply_name}/{product_id}/{week}").uniform(-0.012, 0.012)


def _products(supply: Supply) -> list[tuple[str, str, float]]:
    """(product id, display name, price factor) for each product that can fill this stock item; the first is in use."""
    rows = PRODUCTS.get(supply.name) if supply.supplier == "Valio Aimo" else None
    if not rows:
        return [("", supply.name, 1.0)]
    from gateway import catalog
    known = catalog.products_by_id([pid for pid, _, _ in rows])
    return [(pid, catalog.display_name(known[pid]) if pid in known else name, factor) for pid, name, factor in rows]


def _price(supply: Supply, product_id: str, factor: float, day: int, in_use: bool) -> float:
    move = MOVES.get(supply.name)
    moved = move[1] if in_use and move and day >= move[0] else 1.0
    return round(supply.unit_cost * factor * moved * _wobble(supply.name, product_id, day), 4)


def _base_portions() -> dict[str, float]:
    return {name: base for name, _, _, base, _ in MENU}


def seed_menu(session: Session, start: date) -> None:
    """First run: the menu with recipes, four weeks of simulated sales and of simulated ingredient prices."""
    from app import kitchen
    if session.scalar(select(Dish.id).where(Dish.org_key == ORG).limit(1)) is not None:
        return
    supplies = {s.name: s for s in session.scalars(select(Supply).where(Supply.org_key == ORG))}
    if not supplies:
        return
    days = range(2 - kitchen.HISTORY_DAYS, 2)                 # the same days as the stock history: -26 .. 1
    for position, (name, course, price, base, recipe) in enumerate(MENU):
        dish = Dish(org_key=ORG, name=name, course=course, menu_price=price, target_margin_pct=TARGETS[course],
                    position=position, updated_by="seed")
        session.add(dish)
        session.flush()
        for item, quantity in recipe.items():
            if item in supplies:
                session.add(RecipeLine(dish_id=dish.id, supply_id=supplies[item].id, quantity=quantity))
        for day in days:
            on = _date_of(start, day)
            session.add(Sale(dish_id=dish.id, day=day, portions=_portions(dish, base, day, on), menu_price=price))
    for supply in supplies.values():
        for i, (pid, pname, factor) in enumerate(_products(supply)):
            for day in days:
                session.add(PriceRecord(supply_id=supply.id, day=day, product_id=pid, product_name=pname,
                                        unit_price=_price(supply, pid, factor, day, i == 0), in_use=i == 0,
                                        source="history"))
    session.flush()


def simulate_day(session: Session, day: int) -> None:
    """A new kitchen day: that day's simulated sales, and each ingredient's latest prices carried forward unchanged."""
    from app import kitchen
    dishes = session.scalars(select(Dish).where(Dish.org_key == ORG)).all()
    if not dishes:
        return
    _, start = kitchen.calendar(session)
    on = _date_of(start, day)
    base = _base_portions()
    sold = set(session.scalars(select(Sale.dish_id).where(Sale.day == day)))
    for dish in dishes:
        if dish.id not in sold:
            # A dish added later sells like an average main.
            session.add(Sale(dish_id=dish.id, day=day, portions=_portions(dish, base.get(dish.name, 4), day, on),
                             menu_price=dish.menu_price))
    supply_ids = list(session.scalars(select(Supply.id).where(Supply.org_key == ORG)))
    for sid in supply_ids:
        latest_day = session.scalar(select(PriceRecord.day).where(PriceRecord.supply_id == sid, PriceRecord.day < day)
                                    .order_by(PriceRecord.day.desc()).limit(1))
        if latest_day is None:
            continue
        today = set(session.scalars(select(PriceRecord.product_id).where(PriceRecord.supply_id == sid,
                                                                         PriceRecord.day == day)))
        for r in session.scalars(select(PriceRecord).where(PriceRecord.supply_id == sid,
                                                           PriceRecord.day == latest_day)).all():
            if r.product_id in today:                         # a live Valio Aimo price for today already exists
                continue
            session.add(PriceRecord(supply_id=sid, day=day, product_id=r.product_id, product_name=r.product_name,
                                    unit_price=r.unit_price, in_use=r.in_use, source=r.source))
    session.flush()
