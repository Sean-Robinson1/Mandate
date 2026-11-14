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
        assert salmon.price.source == "recent price"                      # from the recorded price history
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


# ---------------------------------------------------------------- margins (ported from the menu-margins branch)

def test_suggested_price_reaches_the_target_margin_rounded_up_to_50_cents():
    # 2.20 to plate at a 70% target: 7.33 before VAT, 8.32 with 13.5% VAT, rounded up to 8.50.
    assert menu.suggested_price(2.20, 70) == 8.5
    assert menu.suggested_price(0, 70) == 0


def test_a_dish_below_its_target_gets_a_suggested_price(db):
    with db.buyer_db() as s:
        reindeer = dish(s, "Sautéed reindeer, mash, lingonberries")
        coffee = dish(s, "Coffee")
    assert reindeer.below_target and reindeer.target == 70
    net = reindeer.suggested_price / (1 + menu.VAT)
    assert 100 * (1 - reindeer.cost / net) >= 70                          # the suggestion really reaches the target
    assert coffee.target == 85                                             # drinks aim higher than mains


def test_menu_price_changes_are_checked_and_audited(db):
    from app.audit import AuditEvent
    with db.buyer_db() as s:
        soup = dish(s, "Creamy salmon soup").dish
        for bad in ("0", "abc", "250"):
            with pytest.raises(ValueError):
                menu.set_menu_price(s, "restaurant", soup.id, bad, "jussi.heikkinen")
        with pytest.raises(ValueError, match="no such dish"):
            menu.set_menu_price(s, "hotel", soup.id, "20", "mikko.laine")   # another kitchen's dish
        changed, old = menu.set_menu_price(s, "restaurant", soup.id, "18,90", "jussi.heikkinen")
        event = s.scalar(select(AuditEvent).where(AuditEvent.event_type == "menu.price_changed"))
    assert (changed.price, old) == (18.9, 17.5) and event is not None


def test_a_price_rise_shows_which_dishes_lose_margin(db):
    with db.buyer_db() as s:
        views = menu.dishes(s, "restaurant", today=kitchen.today(s))
        impact = {a["supply"]: a for a in menu.price_impact(s, "restaurant", views)}
    cream = impact["Cream"]                                               # seeded +12% three days ago
    assert cream["rising"] and {d["dish"] for d in cream["dishes"]} >= {"Crème brûlée", "Chanterelle pasta"}
    assert all(d["after"] < d["before"] for d in cream["dishes"]) and cream["week"] > 0


def test_stock_that_would_be_wasted_gets_a_special_that_sells_it(db):
    with db.buyer_db() as s:
        kitchen.demo_start(s)                                       # yesterday's delivery brought too much salmon
        ideas = menu.special_ideas(s, "restaurant")
        assert ideas and ideas[0].supply.name == "Salmon fillet" and ideas[0].at_risk >= menu.SPECIAL_MIN_AT_RISK
        idea = ideas[0]
        assert idea.price < idea.dish.dish.price and idea.margin > 0          # discounted, never below cost
        menu.start_special(s, "restaurant", idea.dish.dish.id, idea.pct, idea.days, "test", "jussi.heikkinen")
        assert all(i.dish.dish.id != idea.dish.dish.id for i in menu.special_ideas(s, "restaurant"))
        kitchen.next_day(s)
        sold = s.scalars(select(Sale).where(Sale.dish_id == idea.dish.dish.id, Sale.day == kitchen.today(s))).all()
        assert sold and all(x.price == idea.price for x in sold)
