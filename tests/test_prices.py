"""Ingredient prices: alerts compare like with like, savings use the same source, offers are recorded per unit."""
import pytest
from sqlalchemy.orm import sessionmaker

from app import prices
from app.db import PriceRecord, SimState, Supply, init_db, make_engine


@pytest.fixture
def s(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'p.db'}")
    init_db(engine)
    with sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(SimState(id=1, day=20, start="2026-11-01"))
        session.add(Supply(id=1, org_key="hotel", name="Cream", supplier="Valio Aimo", ask_as="cream", unit="L",
                           on_hand=50, daily_use=10, reorder_below=20, order_up_to=80, unit_cost=4.0))
        session.commit()
        yield session
    engine.dispose()


def add(s, day, price, pid="A", in_use=True, source="history"):
    s.add(PriceRecord(supply_id=1, day=day, product_id=pid, product_name=f"Cream {pid}", unit_price=price,
                      in_use=in_use, source=source))


def test_a_price_rise_is_an_alert_with_its_weekly_effect(s):
    for d in range(1, 21):
        add(s, d, 4.0 if d < 18 else 4.4)
    s.commit()
    (alert,) = prices.overview(s, "hotel")["alerts"]
    week = prices.week_use(s.get(Supply, 1))
    assert (alert["old_price"], alert["new_price"], alert["since_day"]) == (4.0, 4.4, 18)
    assert alert["change_pct"] == 10.0 and alert["weekly_effect"] == round(0.4 * week, 2)


def test_first_real_price_after_simulated_history_is_not_a_price_change(s):
    for d in range(1, 20):
        add(s, d, 4.0)
    add(s, 20, 6.5, source="valio")                       # the real price is far from the simulated one
    s.commit()
    data = prices.overview(s, "hotel")
    assert data["alerts"] == [] and data["items"][0]["source"] == "valio" and data["items"][0]["unit_price"] == 6.5


def test_cheaper_equivalent_is_only_compared_with_the_same_source(s):
    add(s, 20, 4.0)
    add(s, 20, 3.5, pid="B", in_use=False)                 # simulated alternative: a saving
    s.commit()
    (row,) = prices.overview(s, "hotel")["cheaper"]
    assert row["alt_product_id"] == "B" and row["saving_week"] == round(0.5 * row["use_week"], 2) and row["orderable"]
    assert prices.switch_request_text(row, __import__("datetime").date(2026, 11, 20)).startswith("I need 70 litres")
    add(s, 21, 6.5, source="valio")                       # once the real price is in use, simulated savings don't count
    s.get(SimState, 1).day = 21
    s.commit()
    assert prices.overview(s, "hotel")["cheaper"] == []


def test_offer_prices_are_kept_per_stock_unit(s):
    items = [{"product_id": "A", "name": "Cream A", "comparison_price": 4.2, "comparison_unit": "L"},
             {"product_id": "B", "name": "Cream B", "comparison_price": 3.9, "comparison_unit": "L"},
             {"product_id": "C", "name": "Cream powder", "comparison_price": 9.0, "comparison_unit": "KG"}]
    prices.record_offer_prices(s, 1, 20, items, {"A"})
    prices.record_offer_prices(s, 1, 20, items, {"A"})    # the same offer twice keeps one row per product
    s.commit()
    rows = {r.product_id: r for r in s.query(PriceRecord)}
    assert set(rows) == {"A", "B"} and rows["A"].in_use and not rows["B"].in_use and rows["A"].source == "valio"
