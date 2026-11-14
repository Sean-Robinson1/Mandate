"""Menu & margins arithmetic on a tiny restaurant: two ingredients, two dishes, a week of sales, one price rise."""
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from app import menu
from app.db import Dish, PriceRecord, RecipeLine, Sale, SimState, Supply, init_db, make_engine

TODAY = 10


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'menu.db'}")
    init_db(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as s:
        s.add(SimState(id=1, day=TODAY, start="2026-11-01"))
        cream = Supply(org_key="bistro", name="Cream", unit="L", on_hand=20, daily_use=1, reorder_below=5,
                       order_up_to=30, unit_cost=4.0, supplier="Valio Aimo", ask_as="cream")
        butter = Supply(org_key="bistro", name="Butter", unit="KG", on_hand=5, daily_use=1, reorder_below=2,
                        order_up_to=10, unit_cost=8.0)
        soup = Dish(org_key="bistro", name="Mushroom soup", menu_price=11.40)      # 10.00 before VAT
        cake = Dish(org_key="bistro", name="Cream cake", course="Dessert", menu_price=5.70)   # 5.00 before VAT
        s.add_all([cream, butter, soup, cake])
        s.flush()
        s.add_all([RecipeLine(dish_id=soup.id, supply_id=cream.id, quantity=0.1),
                   RecipeLine(dish_id=soup.id, supply_id=butter.id, quantity=0.05),
                   RecipeLine(dish_id=cake.id, supply_id=cream.id, quantity=0.5)])
        s.add_all([Sale(dish_id=soup.id, day=d, portions=10, menu_price=11.40) for d in (1, 8, 9, 10)]   # day 1: last week
                  + [Sale(dish_id=cake.id, day=10, portions=4, menu_price=5.70)])
        s.add_all([PriceRecord(supply_id=cream.id, day=-5, product_id="C1", product_name="Cream 38%", unit_price=4.0, in_use=True),
                   PriceRecord(supply_id=cream.id, day=5, product_id="C1", product_name="Cream 38%", unit_price=4.4, in_use=True),
                   PriceRecord(supply_id=cream.id, day=9, product_id="C2", product_name="Cheap cream", unit_price=3.8)])
        s.commit()
    return sessions


def test_dish_margins_alerts_and_savings(db):
    with db() as s:
        data = menu.dashboard(s, "bistro")
    dishes = {d["name"]: d for d in data["dishes"]}
    soup, cake = dishes["Mushroom soup"], dishes["Cream cake"]
    # Plate cost at today's cream price (4.40) and butter's own cost (8.00); margins before VAT.
    assert soup["plate_cost"] == 0.84 and soup["net_price"] == 10.0 and soup["margin_pct"] == 91.6
    assert soup["portions_week"] == 30 and not soup["below_target"] and soup["suggested_price"] is None
    # 2.20 to plate, 5.00 net: 56% against a 70% target -> 7.33 net, 8.36 with VAT, rounded up to 8.50.
    assert cake["margin_pct"] == 56.0 and cake["below_target"] and cake["suggested_price"] == 8.5
    assert data["summary"]["pressured_dish"]["name"] == "Cream cake"

    alert = data["alerts"][0]                            # cream 4.00 -> 4.40 on day 5
    assert (alert["supply"], alert["old_price"], alert["new_price"], alert["since_day"]) == ("Cream", 4.0, 4.4, 5)
    assert alert["change_pct"] == 10.0 and alert["rising"]
    assert alert["weekly_cost_change"] == 2.0            # 5 L this week (3 soup + 2 cake) x 0.40
    assert {d["name"]: d["loss_week"] for d in alert["dishes"]} == {"Mushroom soup": 1.2, "Cream cake": 0.8}

    saving = data["cheaper"][0]
    assert (saving["alt_product"], saving["alt_price"], saving["saving_week"]) == ("Cheap cream", 3.8, 3.0)
    assert saving["orderable"] and data["summary"]["biggest_saving"]["saving_week"] == 3.0
    assert menu.switch_request_text(saving, date(2026, 11, 10)) == "I need 10 litres of Cheap cream by Friday"


def test_small_price_moves_are_not_alerts():
    assert menu.price_change([(1, 4.0), (8, 4.08)], today=10) is None        # 2%
    assert menu.price_change([(1, 4.0), (8, 3.6)], today=10) == (4.0, 3.6, 8)


def test_menu_price_changes_are_checked(db):
    with db() as s:
        dish_id = s.scalar(Dish.__table__.select().with_only_columns(Dish.id).limit(1))
        for bad in ("0", "abc", "250"):
            with pytest.raises(ValueError):
                menu.set_menu_price(s, dish_id, bad, "mikko.laine")
        assert menu.set_menu_price(s, dish_id, "24,50", "mikko.laine").menu_price == 24.5
