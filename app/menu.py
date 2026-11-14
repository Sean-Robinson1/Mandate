"""Menu & margins: what each dish costs to plate, what it earns, and what changing ingredient prices do to it.

Plate cost = each recipe line's quantity x the ingredient's current price (the latest price of the product the kitchen
buys, else the stock item's own unit cost). Menu prices include VAT; margins are worked out before VAT.
Small pure functions do the arithmetic; dashboard() gathers the rows and returns one JSON-friendly dict.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import kitchen
from app.db import Dish, PriceRecord, RecipeLine, Sale, Supply

VAT_FOOD = 0.14            # Finnish VAT on restaurant food
WINDOW_DAYS = 7            # "this week": the last 7 kitchen days, today included
ALERT_LOOKBACK = 14        # price moves within this many days are alerts
ALERT_MIN_PCT = 3.0        # smaller moves are noise
ALT_MAX_AGE = 14           # an alternative's price older than this is not trusted for a saving
PRICE_STEP = 0.5           # suggested menu prices round up to this
PRICE_RANGE = (1.0, 200.0)


# ---------------------------------------------------------------- arithmetic

def net_of(gross: float) -> float:
    return gross / (1 + VAT_FOOD)


def plate_cost(lines: list[tuple[float, float]]) -> float:
    """Sum of quantity x unit price for one portion."""
    return sum(q * p for q, p in lines)


def margin(net_price: float, cost: float) -> tuple[float, float]:
    """Gross margin in € and as % of the net price."""
    eur = net_price - cost
    return eur, (eur / net_price * 100 if net_price > 0 else 0.0)


def suggested_price(cost: float, target_pct: float) -> float:
    """The lowest menu price (with VAT, rounded up to €0.50) that earns the target margin on this plate cost."""
    net = cost / (1 - min(target_pct, 99.0) / 100)
    return math.ceil(net * (1 + VAT_FOOD) / PRICE_STEP - 1e-9) * PRICE_STEP


def price_change(series: list[tuple[int, float]], today: int, lookback: int = ALERT_LOOKBACK
                 ) -> tuple[float, float, int] | None:
    """(old, new, since_day) when the price moved within the lookback, else None. series: (day, price), any order."""
    points = sorted(series)
    if len(points) < 2:
        return None
    cutoff = today - lookback
    before = [p for p in points if p[0] <= cutoff]
    old = before[-1][1] if before else points[0][1]
    new = points[-1][1]
    if old <= 0 or abs(new - old) / old * 100 < ALERT_MIN_PCT:
        return None
    since = next((d for d, p in points if d > (before[-1][0] if before else points[0][0]) and p != old), points[-1][0])
    return old, new, since


# ---------------------------------------------------------------- prices

def _current(supply: Supply, records: list[PriceRecord]) -> tuple[float, str]:
    """(price, product name) of what the kitchen buys now."""
    in_use = [r for r in records if r.in_use]
    if not in_use:
        return supply.unit_cost, supply.name
    latest = max(in_use, key=lambda r: (r.day, r.id))
    return latest.unit_price, latest.product_name or supply.name


def record_offer_prices(session: Session, supply_id: int, day: int, items: list[dict], chosen: set[str]) -> None:
    """Keep the prices Valio Aimo offered for a stock item: the chosen products become the ones in use.

    Only prices per the item's own unit are kept (a per-kg price says nothing about a stock item counted in litres).
    """
    supply = session.get(Supply, supply_id)
    if supply is None:
        return
    for item in items:
        price, unit = item.get("comparison_price"), item.get("comparison_unit")
        if unit != supply.unit or not price or price <= 0:
            continue
        pid = str(item.get("product_id", ""))
        existing = session.scalar(select(PriceRecord).where(PriceRecord.supply_id == supply_id, PriceRecord.day == day,
                                                            PriceRecord.product_id == pid))
        record = existing or PriceRecord(supply_id=supply_id, day=day, product_id=pid)
        record.product_name, record.unit_price = str(item.get("name", ""))[:200], round(float(price), 4)
        record.in_use, record.source = pid in chosen, "valio"
        if existing is None:
            session.add(record)


# ---------------------------------------------------------------- menu prices and actions

def set_menu_price(session: Session, dish_id: int, price: float, person_id: str) -> Dish:
    """Change a dish's menu price (with VAT). Raises ValueError with a message for the person."""
    dish = session.get(Dish, dish_id)
    if dish is None:
        raise ValueError("no such dish")
    try:
        value = float(str(price).replace("€", "").replace(",", ".").strip())
    except ValueError:
        raise ValueError("enter a price in euros, e.g. 24.50") from None
    low, high = PRICE_RANGE
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"enter a price between €{low:.2f} and €{high:.2f}")
    dish.menu_price, dish.updated_by = round(value, 2), person_id
    session.commit()
    return dish


def switch_request_text(item: dict, on: date) -> str:
    """The agent request for a cheaper product: about a week's use, in the stock item's ordering steps."""
    unit = item["unit"]
    step = kitchen.STEP.get(unit, 1)
    quantity = max(step, math.ceil(item["use_week"] / step) * step)
    by = on + timedelta(days=3)
    return f"I need {quantity:,g} {kitchen.UNIT_WORDS.get(unit, unit)} of {item['alt_product']} by {by:%A}"


# ---------------------------------------------------------------- the page

def _r(v: float) -> float:
    return round(v, 2)


def dashboard(session: Session, org_key: str) -> dict:
    """Everything the Menu & margins page shows for one restaurant."""
    day, start = kitchen.calendar(session)
    supplies = {s.id: s for s in session.scalars(select(Supply).where(Supply.org_key == org_key)
                                                 .order_by(Supply.position))}
    dishes = session.scalars(select(Dish).where(Dish.org_key == org_key).order_by(Dish.position, Dish.id)).all()
    dish_ids = [d.id for d in dishes]
    lines = session.scalars(select(RecipeLine).where(RecipeLine.dish_id.in_(dish_ids))).all() if dish_ids else []
    sales = session.scalars(select(Sale).where(Sale.dish_id.in_(dish_ids), Sale.day > day - WINDOW_DAYS,
                                               Sale.day <= day)).all() if dish_ids else []
    records = session.scalars(select(PriceRecord).where(PriceRecord.supply_id.in_(list(supplies)),
                                                        PriceRecord.day > day - kitchen.HISTORY_DAYS,
                                                        PriceRecord.day <= day)).all() if supplies else []
    by_supply: dict[int, list[PriceRecord]] = {}
    for r in records:
        by_supply.setdefault(r.supply_id, []).append(r)
    current = {sid: _current(s, by_supply.get(sid, [])) for sid, s in supplies.items()}
    price = {sid: c[0] for sid, c in current.items()}
    recipe: dict[int, list[RecipeLine]] = {}
    for line in lines:
        if line.supply_id in supplies:
            recipe.setdefault(line.dish_id, []).append(line)
    portions = {d.id: 0 for d in dishes}
    revenue = {d.id: 0.0 for d in dishes}
    for sale in sales:
        portions[sale.dish_id] += sale.portions
        revenue[sale.dish_id] += sale.portions * net_of(sale.menu_price)

    def cost_with(dish_id: int, prices: dict[int, float]) -> float:
        return plate_cost([(l.quantity, prices[l.supply_id]) for l in recipe.get(dish_id, [])])

    # ---- dishes
    rows = []
    for d in dishes:
        cost, net = cost_with(d.id, price), net_of(d.menu_price)
        m_eur, m_pct = margin(net, cost)
        below = m_pct < d.target_margin_pct
        rows.append({
            "id": d.id, "name": d.name, "course": d.course, "menu_price": _r(d.menu_price), "net_price": _r(net),
            "plate_cost": _r(cost), "margin_eur": _r(m_eur), "margin_pct": round(m_pct, 1),
            "target_margin_pct": d.target_margin_pct, "below_target": below, "portions_week": portions[d.id],
            "profit_week": _r(revenue[d.id] - portions[d.id] * cost),
            "suggested_price": suggested_price(cost, d.target_margin_pct) if below else None,
            "cost_lines": [{"supply_id": l.supply_id, "name": supplies[l.supply_id].name, "quantity": l.quantity,
                            "unit": supplies[l.supply_id].unit, "unit_price": _r(price[l.supply_id]),
                            "cost": _r(l.quantity * price[l.supply_id])} for l in recipe.get(d.id, [])],
        })
    rows.sort(key=lambda r: -r["profit_week"])

    use_week = {sid: 0.0 for sid in supplies}
    for d in dishes:
        for l in recipe.get(d.id, []):
            use_week[l.supply_id] += portions[d.id] * l.quantity
    users = {sid: [d for d in dishes if any(l.supply_id == sid for l in recipe.get(d.id, []))] for sid in supplies}

    # ---- price alerts
    alerts = []
    for sid, s in supplies.items():
        series = [(r.day, r.unit_price) for r in by_supply.get(sid, []) if r.in_use]
        moved = price_change(series, day)
        if moved is None:
            continue
        old, new, since = moved
        before_prices = {**price, sid: old}
        affected = []
        for d in users[sid]:
            net = net_of(d.menu_price)
            before, after = cost_with(d.id, before_prices), cost_with(d.id, price)
            qty = sum(l.quantity for l in recipe[d.id] if l.supply_id == sid)
            after_pct = margin(net, after)[1]
            affected.append({"id": d.id, "name": d.name, "margin_pct_before": round(margin(net, before)[1], 1),
                             "margin_pct_after": round(after_pct, 1), "loss_week": _r((new - old) * qty * portions[d.id]),
                             "suggested_price": suggested_price(after, d.target_margin_pct)
                             if after_pct < d.target_margin_pct else None})
        alerts.append({"supply_id": sid, "supply": s.name, "unit": s.unit, "old_price": _r(old), "new_price": _r(new),
                       "change_pct": round((new - old) / old * 100, 1), "since_day": since, "rising": new > old,
                       "weekly_cost_change": _r((new - old) * use_week[sid]),
                       "dishes": sorted(affected, key=lambda a: -abs(a["loss_week"]))})
    alerts.sort(key=lambda a: -abs(a["weekly_cost_change"]))

    # ---- ingredient spend
    food_cost = sum(use_week[sid] * price[sid] for sid in supplies)
    top_spend = []
    for sid, s in supplies.items():
        spend = use_week[sid] * price[sid]
        if spend <= 0:
            continue
        top_spend.append({"supply_id": sid, "supply": s.name, "unit": s.unit, "spend_week": _r(spend),
                          "share_pct": round(spend / food_cost * 100, 1) if food_cost else 0.0,
                          "unit_price": _r(price[sid]),
                          "trend": [[d, _r(p)] for d, p in sorted({r.day: r.unit_price for r in
                                    sorted(by_supply.get(sid, []), key=lambda r: (r.day, r.id)) if r.in_use}.items())]})
    top_spend = sorted(top_spend, key=lambda t: -t["spend_week"])[:8]

    # ---- cheaper equivalents: the cheapest recent alternative per item, if it beats today's price
    cheaper = []
    for sid, s in supplies.items():
        latest: dict[str, PriceRecord] = {}
        for r in sorted(by_supply.get(sid, []), key=lambda r: (r.day, r.id)):
            if not r.in_use and r.day > day - ALT_MAX_AGE:
                latest[r.product_id] = r
        if not latest:
            continue
        best = min(latest.values(), key=lambda r: r.unit_price)
        saving = (price[sid] - best.unit_price) * use_week[sid]
        if best.unit_price >= price[sid] or saving <= 0:
            continue
        cheaper.append({"supply_id": sid, "supply": s.name, "unit": s.unit, "current_product": current[sid][1],
                        "current_price": _r(price[sid]), "alt_product_id": best.product_id,
                        "alt_product": best.product_name or best.product_id, "alt_price": _r(best.unit_price),
                        "use_week": _r(use_week[sid]), "saving_week": _r(saving),
                        "orderable": s.supplier == "Valio Aimo" and bool(s.ask_as)})
    cheaper.sort(key=lambda c: -c["saving_week"])

    # ---- stock to use first
    at_risk = [{"supply_id": b.supply.id, "supply": b.supply.name, "unit": b.supply.unit, "quantity": b.quantity,
                "use_by_day": day + b.days_left, "value": _r(b.quantity * price.get(b.supply.id, b.supply.unit_cost)),
                "dishes": [d.name for d in users.get(b.supply.id, [])]}
               for b in kitchen.use_first(session, org_key)]

    # ---- summary
    revenue_week = sum(revenue.values())
    cost_week = sum(portions[d.id] * cost_with(d.id, price) for d in dishes)
    pressured = max((r for r in rows if r["below_target"]),
                    key=lambda r: r["target_margin_pct"] - r["margin_pct"], default=None)
    summary = {
        "revenue_week_net": _r(revenue_week), "food_cost_week": _r(cost_week),
        "food_cost_pct": round(cost_week / revenue_week * 100, 1) if revenue_week else 0.0,
        "gross_margin_week": _r(revenue_week - cost_week),
        "gross_margin_pct": round((revenue_week - cost_week) / revenue_week * 100, 1) if revenue_week else 0.0,
        "pressured_dish": {k: pressured[k] for k in ("id", "name", "margin_pct", "target_margin_pct")} if pressured else None,
        "biggest_saving": {k: cheaper[0][k] for k in ("supply_id", "supply", "saving_week")} if cheaper else None,
    }
    return {"day": day, "date": kitchen.date_of(start, day).isoformat(), "window_days": WINDOW_DAYS,
            "summary": summary, "dishes": rows, "alerts": alerts, "top_spend": top_spend, "cheaper": cheaper,
            "at_risk": at_risk}
