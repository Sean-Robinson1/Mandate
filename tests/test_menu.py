"""The restaurant's menu: portion costs from recipes and prices, and a day's service using stock."""
import json

import pytest
from sqlalchemy import select

from app import kitchen, menu
from app.db import Order, Run, Sale, Supply
from tests.conftest import NOW


@pytest.fixture
def db(world):
    with world.buyer_db() as s:
        kitchen.seed(s, start=NOW.date())
    return world


def dish(s, name):
    return next(v for v in menu.dishes(s, "restaurant") if v.dish.name == name)


def test_portion_cost_counts_trimming_and_extras_at_list_prices(db):
    with db.buyer_db() as s:
        v = dish(s, "Creamy salmon soup")
        salmon = next(i for i in v.ingredients if i.supply.name == "Salmon fillet")
        assert salmon.used == pytest.approx(0.14 / 0.9, abs=1e-4)          # 90% usable after trimming
        assert salmon.price.source == "list price"
        assert v.cost == round(sum(i.cost for i in v.ingredients) + v.dish.extras, 2)
        assert v.food_cost_pct == round(100 * v.cost / round(17.5 / (1 + menu.VAT), 2), 1)


def test_portion_cost_uses_the_price_valio_aimo_confirmed(db):
    with db.buyer_db() as s:
        salmon = s.scalar(select(Supply).where(Supply.org_key == "restaurant", Supply.name == "Salmon fillet"))
        s.add(Run(trace_id="t-salmon", created_at=NOW.isoformat(), request_text="Restock salmon", mandate_id="m",
                  buyer_org="Bistro Lumo Oy", status="ORDERED", supply_id=salmon.id))
        s.add(Order(order_id="PO-1", trace_id="t-salmon", mandate_id="m", buyer_org="Bistro Lumo Oy", status="PLACED",
                    total_net=600.0, created_at=NOW.isoformat(), supplier_order_id="AIMO-1",
                    lines_json=json.dumps([{"measure": 30, "contains": ["AF"]}])))
        s.commit()
        price = dish(s, "Creamy salmon soup").ingredients[0].price
    assert price.per_unit == 20.0 and price.source == "Valio Aimo order AIMO-1" and price.link == "t-salmon"


def test_service_uses_recipes_and_a_dish_sells_out_when_an_ingredient_runs_short(db):
    with db.buyer_db() as s:
        salmon = s.scalar(select(Supply).where(Supply.org_key == "restaurant", Supply.name == "Salmon fillet"))
        salmon.on_hand = 0.5                                              # enough for about three portions
        s.commit()
        day = kitchen.next_day(s)
        summary = menu.day_summary(s, "restaurant", day)
        sales = s.scalars(select(Sale).where(Sale.org_key == "restaurant", Sale.day == day)).all()
        salmon = s.get(Supply, salmon.id)
        names = {v.dish.id: v.dish.name for v in menu.dishes(s, "restaurant")}
    assert summary.covers > 0 and summary.revenue > 0
    assert {n for n, _ in summary.sold_out} & {"Creamy salmon soup", "Pan-fried salmon, dill potatoes"}
    assert 0 <= salmon.on_hand < 0.16                                    # used down to less than one portion
    served_salmon = sum(x.quantity for x in sales if x.served and "salmon" in names[x.dish_id].lower())
    assert served_salmon >= 1
