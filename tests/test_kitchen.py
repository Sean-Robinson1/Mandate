"""The supplies sheet, simulated days, and the agent restocking by itself (see conftest.py for the world)."""
from datetime import date

import pytest
from sqlalchemy import select

from app import agent, kitchen
from app.db import Order, Run, Supply
from tests.conftest import NOW


@pytest.fixture
def sheet_db(world):
    with world.buyer_db() as s:
        kitchen.seed(s)
    return world


def supply(world, name: str, org: str = "hotel") -> Supply:
    with world.buyer_db() as s:
        return s.scalar(select(Supply).where(Supply.org_key == org, Supply.name == name))


def set_cell(world, name: str, field: str, value, manager: bool = True, org: str = "hotel"):
    with world.buyer_db() as s:
        return kitchen.update_cell(s, supply(world, name, org).id, field, str(value), "test", manager)


def run_restocks(world, org: str = "hotel") -> list[agent.RunResult]:
    """What the console does after each change: start the agent on every low row (synchronously here)."""
    results = []
    with world.buyer_db() as s:
        due = kitchen.due_restocks(s, org)
    for r in due:
        with world.buyer_db() as s:
            trace = agent.open_run(s, r.text, world.mandates[org], now=NOW, supply_id=r.supply_id, trigger=r.trigger)
            results.append(agent.run(r.text, world.mandates[org], s, world.client, now=NOW, trace_id=trace,
                                     review_reason=r.review_reason))
    return results


def rows(world, org: str = "hotel") -> dict[str, kitchen.Row]:
    with world.buyer_db() as s:
        return {r.supply.name: r for r in kitchen.sheet(s, org, "Mikko Laine")}


def test_seeded_sheet_is_healthy(sheet_db):
    r = rows(sheet_db)
    assert list(r) == ["Lactose-free milk", "Cream", "Butter", "Oat drink"]
    assert all(row.status == "OK" for row in r.values())
    with sheet_db.buyer_db() as s:
        assert kitchen.due_restocks(s, "hotel") == []


def test_a_day_uses_stock(sheet_db):
    with sheet_db.buyer_db() as s:
        assert kitchen.next_day(s) == 2
    assert supply(sheet_db, "Lactose-free milk").on_hand == 760 - 220


def test_low_auto_row_is_restocked_up_to_its_level_without_asking(sheet_db):
    set_cell(sheet_db, "Lactose-free milk", "on_hand", 300)                 # below 600
    [result] = run_restocks(sheet_db)
    assert result.status == "ORDERED"
    assert result.intent.quantity == 1300                                    # up to 1,600
    r = rows(sheet_db)["Lactose-free milk"]
    assert r.on_order == 1300 and "on the way" in r.status
    with sheet_db.buyer_db() as s:
        assert kitchen.due_restocks(s, "hotel") == []                        # nothing ordered twice


def test_low_ask_first_row_waits_for_the_manager(sheet_db):
    set_cell(sheet_db, "Lactose-free milk", "rule", "ask")
    set_cell(sheet_db, "Lactose-free milk", "on_hand", 300)
    [result] = run_restocks(sheet_db)
    assert result.status == "NEEDS_HUMAN"
    assert [x.rule for x in result.policy.reasons] == ["request.review_required"]
    assert rows(sheet_db)["Lactose-free milk"].status == "Waiting for Mikko Laine"
    with sheet_db.buyer_db() as s:
        assert kitchen.due_restocks(s, "hotel") == []                        # already being decided


def test_delivery_arrives_and_tops_up_the_sheet(sheet_db):
    set_cell(sheet_db, "Lactose-free milk", "on_hand", 300)
    run_restocks(sheet_db)
    days = 0
    while True:                                   # the real lead time (Fri -> Mon) in kitchen days
        before = supply(sheet_db, "Lactose-free milk").on_hand
        with sheet_db.buyer_db() as s:
            kitchen.next_day(s)
            order = s.scalar(select(Order))
        days += 1
        if order.status == "DELIVERED":
            break
        assert days < 5
    lead = (date.fromisoformat(order.delivery_date) - date.fromisoformat(order.created_at[:10])).days
    assert days == lead
    assert supply(sheet_db, "Lactose-free milk").on_hand == before + 1300 - 220   # delivered, then the day's use
    assert rows(sheet_db)["Lactose-free milk"].delivered_today == 1300


def test_delivered_orders_still_count_towards_the_week(sheet_db):
    set_cell(sheet_db, "Lactose-free milk", "on_hand", 300)
    run_restocks(sheet_db)
    with sheet_db.buyer_db() as s:
        for _ in range(4):
            kitchen.next_day(s)
        assert agent.week_spend(s, sheet_db.mandates["hotel"].mandate_id, NOW) == 1300


def test_crew_can_count_but_only_the_manager_sets_levels_and_rules(sheet_db):
    set_cell(sheet_db, "Cream", "on_hand", 95, manager=False)
    with pytest.raises(PermissionError):
        set_cell(sheet_db, "Cream", "reorder_below", 10, manager=False)
    with pytest.raises(PermissionError):
        set_cell(sheet_db, "Cream", "rule", "ask", manager=False)
    set_cell(sheet_db, "Cream", "rule", "ask")
    assert supply(sheet_db, "Cream").rule == "ask"


@pytest.mark.parametrize("field,value", [("on_hand", "-5"), ("on_hand", "lots"), ("reorder_below", "500"),
                                         ("rule", "always")])
def test_bad_cell_values_are_refused(sheet_db, field, value):
    with pytest.raises(ValueError):
        set_cell(sheet_db, "Cream", field, value)          # reorder 500 would be above 'order up to' 250


def test_restock_request_reads_like_a_person_wrote_it(sheet_db):
    milk = supply(sheet_db, "Lactose-free milk")
    assert kitchen.restock_text(milk, 1300, NOW.date()) == "Restock 1,300 litres of lactose-free milk by Monday"
