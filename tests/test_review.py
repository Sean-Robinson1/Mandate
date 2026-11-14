"""The approver's summary: price against what the kitchen usually pays, stock, and what declining means."""
import json

import pytest
from sqlalchemy import delete, select

from app import kitchen, review
from app.db import PriceRecord, Run, Supply
from tests.conftest import NOW


def cheese_run(s, total: float, quantity: float = 10) -> tuple[Run, dict, dict]:
    cheese = s.scalar(select(Supply).where(Supply.org_key == "restaurant", Supply.name == "Grated cheese"))
    plan = {"unit": "KG", "covered_quantity": quantity, "requested_quantity": quantity, "total_net": total,
            "total_with_tax": round(total * 1.135, 2),
            "lines": [{"name": "Valio cheese mix 2.5 kg", "measure": quantity, "delivery_date": "2026-11-16"}]}
    intent = {"unit": "KG", "deadline": "2026-11-17", "product_terms": ["grated", "cheese"]}
    run = Run(trace_id="t-cheese", created_at=NOW.isoformat(), request_text="Restock 10 kg of grated cheese",
              mandate_id="mandate-restaurant-001", buyer_org="Bistro Lumo Oy", status="NEEDS_HUMAN",
              supply_id=cheese.id, total_net=total)
    return run, plan, intent


@pytest.fixture
def db(world):
    with world.buyer_db() as s:
        kitchen.seed(s, start=NOW.date())
    return world


def test_price_is_compared_with_the_recent_average_paid(db):
    with db.buyer_db() as s:
        cheese = s.scalar(select(Supply).where(Supply.org_key == "restaurant", Supply.name == "Grated cheese"))
        s.execute(delete(PriceRecord).where(PriceRecord.supply_id == cheese.id))
        for day, price in [(1, 8.0), (0, 8.0), (-1, 7.0), (-40, 1.0)]:         # the last one is too old to count
            s.add(PriceRecord(supply_id=cheese.id, day=day, product_id="p", unit_price=price, in_use=True))
        s.commit()
        run, plan, intent = cheese_run(s, total=55.0)
        b = review.brief(s, run, plan, intent, db.mandates["restaurant"])
    assert b.price == 5.5 and b.usual == pytest.approx(7.67, abs=0.01)
    assert b.diff_pct < 0 and b.price_tone == "ok" and "below" in b.price_words
    assert b.on_time and b.supply.name == "Grated cheese" and b.on_hand == 5
    assert "Bistro burger and fries" in b.dishes
    assert b.week_spent == 0 and b.week_after == 55.0 and b.order_limit == 600


def test_a_price_close_to_usual_reads_as_about_the_same(db):
    with db.buyer_db() as s:
        run, plan, intent = cheese_run(s, total=0)
        usual = review._usual_price(s, s.get(Supply, run.supply_id), kitchen.today(s))[0]
        run, plan, intent = cheese_run(s, total=round(usual * 10 * 1.01, 2))
        b = review.brief(s, run, plan, intent, db.mandates["restaurant"])
    assert b.price_tone == "" and b.price_words == "About what you usually pay"
