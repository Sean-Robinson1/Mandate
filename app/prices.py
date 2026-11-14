"""Ingredient prices: what each stock item costs now, how that has moved, and where a cheaper equivalent exists.

Every signed Valio Aimo offer the agent receives is kept per stock item and product (record_offer_prices), so the
price history becomes real over time. Before that, a simulated four-week history starts each item at its unit cost,
with a few deliberate moves so the alerts have something to show (see ASSUMPTIONS.md).

A price is only ever compared with earlier prices of the same product from the same source: switching products, or the
first real Valio Aimo price arriving after simulated history, is not a "price change". Recipes and dish margins plug in
on top of this once the menu lands (docs/PLAN.md items 2 and 5).
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import kitchen
from app.db import PriceRecord, Supply

LOOKBACK_DAYS = 14          # price moves within this many days are alerts
MIN_CHANGE_PCT = 3.0        # smaller moves are noise
ALT_MAX_AGE = 14            # an alternative's price older than this is not trusted for a saving

# Simulated history: (days ago the new price started, factor) for a few items, by stock item name.
MOVES = {"Cream": (3, 1.12), "Butter": (11, 0.94), "Salmon fillet": (5, 1.09), "Grated cheese": (7, 1.08)}
# Valio Aimo products per stock item name: the one in use first, then alternatives, as (catalog id, price factor).
PRODUCTS = {
    "Lactose-free milk": [("464398", 1.0), ("469363", 0.95), ("465039", 1.06)],
    "Cream": [("463366", 1.0), ("461273", 0.92), ("463299", 0.78)],
    "Butter": [("464411", 1.0), ("476815", 0.85), ("463437", 1.08)],
    "Oat drink": [("469512", 1.0), ("468465", 0.94)],
    "Salmon fillet": [("461903", 1.0), ("461885", 0.9), ("461814", 1.05)],
    "Grated cheese": [("464798", 1.0), ("465241", 0.93), ("463060", 1.12)],
}


# ---------------------------------------------------------------- arithmetic

def change(series: list[tuple[int, float]], today: int, lookback: int = LOOKBACK_DAYS) -> tuple[float, float, int] | None:
    """(old, new, since_day) when a price moved by at least MIN_CHANGE_PCT within the lookback, else None.

    series: (day, price) of one product from one source. old = the last price on or before the lookback start
    (or the first one inside it); since_day = the first day the price differed from old.
    """
    points = sorted(series)
    if len(points) < 2:
        return None
    before = [p for p in points if p[0] <= today - lookback]
    start_day, old = (before[-1] if before else points[0])
    new = points[-1][1]
    if old <= 0 or abs(new - old) / old * 100 < MIN_CHANGE_PCT:
        return None
    since = next((d for d, p in points if d > start_day and abs(p - old) / old * 100 >= MIN_CHANGE_PCT), points[-1][0])
    return old, new, since


def week_use(supply: Supply) -> float:
    """A typical week's use, from the item's daily use and its kitchen's week."""
    return supply.daily_use * sum(kitchen.WEEK.get(supply.org_key, kitchen.FLAT))


# ---------------------------------------------------------------- recording

def record_offer_prices(session: Session, supply_id: int, day: int, items: list[dict], chosen: set[str]) -> None:
    """Keep the prices Valio Aimo offered for a stock item; the chosen products become the ones in use.

    Only prices per the item's own unit are kept (a per-kg price says nothing about an item counted in litres).
    """
    supply = session.get(Supply, supply_id)
    if supply is None:
        return
    for item in items:
        price, unit = item.get("comparison_price"), item.get("comparison_unit")
        if unit != supply.unit or not price or price <= 0:
            continue
        pid = str(item.get("product_id", ""))
        record = session.scalar(select(PriceRecord).where(PriceRecord.supply_id == supply_id, PriceRecord.day == day,
                                                          PriceRecord.product_id == pid, PriceRecord.source == "valio"))
        if record is None:
            record = PriceRecord(supply_id=supply_id, day=day, product_id=pid, source="valio")
            session.add(record)
        record.product_name, record.unit_price = str(item.get("name", ""))[:200], round(float(price), 4)
        record.in_use = pid in chosen


def seed_history(session: Session) -> None:
    """Simulated past prices for every stock item that has none yet (four weeks, deterministic)."""
    from gateway import catalog
    today, _ = kitchen.calendar(session)
    priced = set(session.scalars(select(PriceRecord.supply_id).distinct()))
    for s in session.scalars(select(Supply)):
        if s.id in priced:
            continue
        rows = PRODUCTS.get(s.name, []) if s.supplier == "Valio Aimo" else []
        known = catalog.products_by_id([pid for pid, _ in rows]) if rows else {}
        products = [(pid, catalog.display_name(known[pid]), f) for pid, f in rows if pid in known] or [("", s.name, 1.0)]
        ago, move = MOVES.get(s.name, (None, 1.0))
        for i, (pid, name, factor) in enumerate(products):
            for day in range(today, today - kitchen.HISTORY_DAYS, -1):
                wobble = 1 + random.Random(f"price/{s.org_key}/{s.name}/{pid}/{(day + 100) // 7}").uniform(-0.012, 0.012)
                moved = move if i == 0 and ago is not None and day > today - ago else 1.0
                session.add(PriceRecord(supply_id=s.id, day=day, product_id=pid, product_name=name, in_use=i == 0,
                                        unit_price=round(s.unit_cost * factor * moved * wobble, 4), source="history"))


# ---------------------------------------------------------------- reading

def _current(records: list[PriceRecord]) -> PriceRecord | None:
    """The latest record of the product in use (a signed Valio Aimo price wins over simulated history on the same day)."""
    in_use = [r for r in records if r.in_use]
    return max(in_use, key=lambda r: (r.day, r.source == "valio", r.id)) if in_use else None


def _series(records: list[PriceRecord], like: PriceRecord) -> list[tuple[int, float]]:
    """One product's prices from one source, one per day."""
    by_day = {r.day: r.unit_price for r in sorted(records, key=lambda r: r.id)
              if r.product_id == like.product_id and r.source == like.source and r.product_name == like.product_name}
    return sorted(by_day.items())


def switch_request_text(item: dict, on: date) -> str:
    """The agent request for a cheaper product: about a week's use, in the item's ordering steps."""
    unit = item["unit"]
    step = kitchen.STEP.get(unit, 1)
    quantity = max(step, math.ceil(item["use_week"] / step) * step)
    by = on + timedelta(days=3)
    return f"I need {quantity:,g} {kitchen.UNIT_WORDS.get(unit, unit)} of {item['alt_product']} by {by:%A}"


def overview(session: Session, org_key: str) -> dict:
    """Everything the Ingredient prices page shows for one kitchen."""
    day, start = kitchen.calendar(session)
    supplies = session.scalars(select(Supply).where(Supply.org_key == org_key).order_by(Supply.position)).all()
    ids = [s.id for s in supplies]
    records = session.scalars(select(PriceRecord).where(PriceRecord.supply_id.in_(ids), PriceRecord.day <= day,
                                                        PriceRecord.day > day - kitchen.HISTORY_DAYS)).all() if ids else []
    by_supply: dict[int, list[PriceRecord]] = {}
    for r in records:
        by_supply.setdefault(r.supply_id, []).append(r)

    items, alerts, cheaper = [], [], []
    for s in supplies:
        mine = by_supply.get(s.id, [])
        cur = _current(mine)
        price = cur.unit_price if cur else s.unit_cost
        use = round(week_use(s), 2)
        series = _series(mine, cur) if cur else []
        items.append({"supply_id": s.id, "supply": s.name, "unit": s.unit, "supplier": s.supplier,
                      "product": cur.product_name if cur else s.name, "unit_price": round(price, 2), "use_week": use,
                      "spend_week": round(price * use, 2), "share_pct": 0.0,
                      "source": cur.source if cur else "unit cost",
                      "trend": [[d, round(p, 2)] for d, p in series]})
        moved = change(series, day) if cur else None
        if moved:
            old, new, since = moved
            alerts.append({"supply_id": s.id, "supply": s.name, "unit": s.unit, "product": cur.product_name,
                           "old_price": round(old, 2), "new_price": round(new, 2),
                           "change_pct": round((new - old) / old * 100, 1), "since_day": since, "rising": new > old,
                           "use_week": use, "weekly_effect": round((new - old) * use, 2)})
        if cur:
            latest_alt: dict[str, PriceRecord] = {}
            for r in mine:
                if not r.in_use and r.source == cur.source and r.day > day - ALT_MAX_AGE and r.product_id != cur.product_id:
                    if r.product_id not in latest_alt or r.day > latest_alt[r.product_id].day:
                        latest_alt[r.product_id] = r
            best = min(latest_alt.values(), key=lambda r: r.unit_price, default=None)
            if best and best.unit_price < price:
                cheaper.append({"supply_id": s.id, "supply": s.name, "unit": s.unit, "current_product": cur.product_name,
                                "current_price": round(price, 2), "alt_product_id": best.product_id,
                                "alt_product": best.product_name, "alt_price": round(best.unit_price, 2),
                                "use_week": use, "saving_week": round((price - best.unit_price) * use, 2),
                                "orderable": kitchen.connected(s)})

    total = sum(i["spend_week"] for i in items)
    for i in items:
        i["share_pct"] = round(i["spend_week"] / total * 100, 1) if total else 0.0
    items.sort(key=lambda i: -i["spend_week"])
    alerts.sort(key=lambda a: -abs(a["weekly_effect"]))
    cheaper.sort(key=lambda c: -c["saving_week"])
    return {"day": day, "date": kitchen.date_of(start, day).isoformat(),
            "summary": {"spend_week": round(total, 2), "rising": sum(a["rising"] for a in alerts),
                        "saving_week": round(sum(c["saving_week"] for c in cheaper), 2),
                        "live_prices": sum(i["source"] == "valio" for i in items), "items": len(items)},
            "alerts": alerts, "cheaper": cheaper, "items": items}
