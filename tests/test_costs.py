"""Waste leaves stock at today's price, and food cost adds it to what the recipes say."""
import pytest
from sqlalchemy import select

from app import costs, kitchen, menu
from app.db import Supply, Waste
from tests.conftest import NOW


@pytest.fixture
def db(world):
    with world.buyer_db() as s:
        kitchen.seed(s, start=NOW.date())
    return world


def supply(s, name):
    return s.scalar(select(Supply).where(Supply.org_key == "restaurant", Supply.name == name))


def test_logged_waste_leaves_stock_and_is_costed(db):
    with db.buyer_db() as s:
        cream = supply(s, "Cream")
        before, price = cream.on_hand, menu.prices(s, [cream])[cream.id].per_unit
        w = costs.log_waste(s, "restaurant", cream.id, "2,5", "spoiled", "elina.makinen", "fridge too warm")
        assert supply(s, "Cream").on_hand == pytest.approx(before - 2.5)
        assert w.cost == pytest.approx(2.5 * price, abs=0.01)                # at today's price, not the list price


@pytest.mark.parametrize("quantity, reason, org, message", [
    ("0", "spoiled", "restaurant", "how much"),
    ("999", "spoiled", "restaurant", "only"),
    ("1", "lost", "restaurant", "reason"),
    ("1", "spoiled", "hotel", "no such item"),                        # another kitchen's item
])
def test_bad_waste_entries_are_refused_and_change_nothing(db, quantity, reason, org, message):
    with db.buyer_db() as s:
        cream = supply(s, "Cream")
        before, logged = cream.on_hand, len(s.scalars(select(Waste.id)).all())
        with pytest.raises(ValueError, match=message):
            costs.log_waste(s, org, cream.id, quantity, reason, "x")
        assert supply(s, "Cream").on_hand == before
        assert len(s.scalars(select(Waste.id)).all()) == logged


def test_the_week_adds_waste_on_top_of_recipe_cost(db):
    with db.buyer_db() as s:
        for _ in range(3):
            kitchen.next_day(s)
        o = costs.overview(s, "restaurant")
    assert o.net_sales > 0 and 15 < o.pct < 40
    assert o.waste_cost > 0 and o.pct_with_waste == round(100 * (o.recipe_cost + o.waste_cost) / o.net_sales, 1)


def test_waste_share_and_weekly_figures():
    days = [costs.DayCost(d, None, 1000.0, 300.0, w) for d, w in ((1, 40.0), (2, 10.0))]
    o = costs.Overview(days, [], [], [], [])
    assert o.waste_pct == round(100 * 50 / 650, 1)                       # of the food bought: dishes sold plus waste
    assert o.waste_per_week == 175.0                                      # €25 a day
    assert o.avoidable_per_week == round(7 * (50 - 0.04 * 650) / 2, 2)   # above a well-run kitchen's 4%
