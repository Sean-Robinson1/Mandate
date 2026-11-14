"""The demo restaurant's simulated menu: complete, consistent with the stock items, and with its price moves in place."""
from sqlalchemy import func, select

from app import kitchen
from app.db import Dish, PriceRecord, RecipeLine, Sale, Supply
from tests.conftest import NOW


def test_seeded_menu_is_complete_and_shows_the_cream_rise(world):
    with world.buyer_db() as s:
        kitchen.seed(s, start=NOW.date())
        dishes = s.scalars(select(Dish).where(Dish.org_key == "bistro")).all()
        assert 20 <= len(dishes) <= 30
        supply_ids = set(s.scalars(select(Supply.id).where(Supply.org_key == "bistro")))
        lines = s.scalars(select(RecipeLine)).all()
        assert lines and all(line.supply_id in supply_ids for line in lines)
        assert {d.id for d in dishes} == {line.dish_id for line in lines}       # every dish has a recipe
        assert s.scalar(select(func.count()).select_from(Sale)) == len(dishes) * kitchen.HISTORY_DAYS

        cream = s.scalar(select(Supply).where(Supply.org_key == "bistro", Supply.name == "Cream"))
        prices = dict(s.execute(select(PriceRecord.day, PriceRecord.unit_price).where(
            PriceRecord.supply_id == cream.id, PriceRecord.in_use)).all())
        assert prices[1] / prices[-10] > 1.08                                    # +12% from day -2, minus drift
        cheaper = s.scalars(select(PriceRecord).where(PriceRecord.supply_id == cream.id, PriceRecord.day == 1,
                                                      PriceRecord.in_use.is_(False))).all()
        assert any(r.unit_price < prices[1] for r in cheaper)

        kitchen.next_day(s)                                                      # simulate_day adds day 2
        assert s.scalar(select(func.count()).select_from(Sale).where(Sale.day == 2)) == len(dishes)
        assert s.scalar(select(PriceRecord.unit_price).where(PriceRecord.supply_id == cream.id, PriceRecord.day == 2,
                                                             PriceRecord.in_use)) == prices[1]
