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
        kitchen.seed(s, start=NOW.date())
    return world


def use_on(world, name: str, day: int) -> float:
    with world.buyer_db() as s:
        _, start = kitchen.calendar(s)
    return kitchen.expected_use(supply(world, name), kitchen.date_of(start, day))


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
    assert list(r)[:4] == ["Lactose-free milk", "Cream", "Butter", "Oat drink"]
    assert all(not row.low for row in r.values())
    assert len(r["Cream"].history) == kitchen.HISTORY_DAYS and r["Cream"].history[-1].on_hand == 110
    with sheet_db.buyer_db() as s:
        assert kitchen.due_restocks(s, "hotel") == []


def test_a_day_uses_stock_and_records_it(sheet_db):
    with sheet_db.buyer_db() as s:
        assert kitchen.next_day(s) == 2
    assert supply(sheet_db, "Lactose-free milk").on_hand == 760 - use_on(sheet_db, "Lactose-free milk", 2)
    assert rows(sheet_db)["Lactose-free milk"].history[-1].day == 2


def test_school_uses_nothing_at_weekends(sheet_db):
    milk = supply(sheet_db, "Lactose-free milk", "school")
    assert kitchen.expected_use(milk, date(2026, 11, 14)) == 0          # Saturday
    assert kitchen.expected_use(milk, date(2026, 11, 16)) == 90         # Monday


def test_forecast_counts_deliveries_and_finds_the_day_it_runs_out(sheet_db):
    r = rows(sheet_db)["Cream"]                                          # 110 L, ~25 L a day
    assert r.runs_out_day is not None and 4 <= r.runs_out_day - 1 <= 6
    with sheet_db.buyer_db() as s:
        kitchen.record_order(s, s.get(Supply, r.supply.id), "200", "2026-11-15", "test")
    later = rows(sheet_db)["Cream"]
    assert later.on_order == 200 and later.expected[0].via == "manual"
    assert later.runs_out_day is None or later.runs_out_day > r.runs_out_day


@pytest.mark.parametrize("quantity,due", [("0", "2026-11-15"), ("5", "2026-11-13"), ("5", "soon"), ("x", "2026-11-15")])
def test_bad_recorded_orders_are_refused(sheet_db, quantity, due):
    with sheet_db.buyer_db() as s, pytest.raises(ValueError):
        kitchen.record_order(s, s.get(Supply, supply(sheet_db, "Cream").id), quantity, due, "test")


def test_agent_reordering_only_for_suppliers_with_a_gateway(sheet_db):
    with pytest.raises(ValueError):
        set_cell(sheet_db, "Potatoes", "rule", "auto")
    set_cell(sheet_db, "Potatoes", "on_hand", 10)                        # low, but the kitchen orders it itself
    with sheet_db.buyer_db() as s:
        assert kitchen.due_restocks(s, "hotel") == []


def test_stock_file_import_matches_names_and_reports_the_rest(sheet_db):
    data = "Item;On hand\ncream;95,5\nEggs;720\nCaviar;3\nButter;-1\n".encode()
    with sheet_db.buyer_db() as s:
        result = kitchen.import_counts(s, "hotel", kitchen.parse_stock_file(data), "import", "test")
    assert result.updated == ["Cream", "Eggs"] and result.unknown == ["Caviar"] and len(result.invalid) == 1
    assert supply(sheet_db, "Cream").on_hand == 95.5
    assert rows(sheet_db)["Cream"].history[-1].source == "import"
    with pytest.raises(ValueError):
        kitchen.parse_stock_file(b"product code,price\n1,2\n")


def test_low_auto_row_is_restocked_up_to_its_level_without_asking(sheet_db):
    set_cell(sheet_db, "Lactose-free milk", "on_hand", 300)                 # below 600
    [result] = run_restocks(sheet_db)
    assert result.status == "ORDERED"
    assert result.intent.quantity == 1300                                    # up to 1,600
    r = rows(sheet_db)["Lactose-free milk"]
    assert r.on_order == 1300 and r.status == "On order"
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


def test_agent_delivery_waits_for_check_in_and_then_tops_up(sheet_db):
    set_cell(sheet_db, "Lactose-free milk", "on_hand", 300)
    run_restocks(sheet_db)
    with sheet_db.buyer_db() as s:
        order = s.scalar(select(Order))
    lead = (date.fromisoformat(order.delivery_date) - date.fromisoformat(order.created_at[:10])).days
    with sheet_db.buyer_db() as s:
        for _ in range(lead):                     # the real lead time (Fri -> Mon) in kitchen days
            kitchen.next_day(s)
        [due] = [i for i in kitchen.deliveries(s, "hotel")[0] if i.due]
        before = supply(sheet_db, "Lactose-free milk").on_hand
        kitchen.check_in(s, "hotel", due.key, "test")
        assert s.get(Order, order.order_id).status == "DELIVERED"
    assert supply(sheet_db, "Lactose-free milk").on_hand == before + 1300
    assert rows(sheet_db)["Lactose-free milk"].delivered_today == 1300
    with sheet_db.buyer_db() as s, pytest.raises(ValueError):
        kitchen.check_in(s, "hotel", due.key, "test")                        # not twice


def test_unchecked_delivery_is_taken_in_the_next_day(sheet_db):
    cream = supply(sheet_db, "Cream")
    with sheet_db.buyer_db() as s:
        kitchen.record_order(s, s.get(Supply, cream.id), "100", "2026-11-14", "test")    # day 2
        kitchen.next_day(s)                                                 # day 2: waits for check-in
        assert supply(sheet_db, "Cream").on_hand == 110 - use_on(sheet_db, "Cream", 2)
        kitchen.next_day(s)                                                 # day 3: taken in first
    assert supply(sheet_db, "Cream").on_hand == 110 + 100 - use_on(sheet_db, "Cream", 2) - use_on(sheet_db, "Cream", 3)


def test_check_in_refuses_a_delivery_not_due_yet(sheet_db):
    cream = supply(sheet_db, "Cream")
    with sheet_db.buyer_db() as s:
        d = kitchen.record_order(s, s.get(Supply, cream.id), "100", "2026-11-16", "test")
        with pytest.raises(ValueError):
            kitchen.check_in(s, "hotel", f"d-{d.id}", "test")


def test_to_order_groups_by_supplier_and_mark_ordered_uses_lead_time(sheet_db):
    groups = kitchen.to_order(list(rows(sheet_db).values()))
    assert groups[0].supplier == "Valio Aimo" and groups[0].agent             # the agent's group first
    pelto = next(g for g in groups if g.supplier == "Pelto Produce Oy")
    assert not pelto.agent and "Potatoes" in [r.supply.name for r in pelto.rows]
    potatoes = supply(sheet_db, "Potatoes")
    with sheet_db.buyer_db() as s:
        d = kitchen.mark_ordered(s, "hotel", potatoes.id, "test")
        assert (d.quantity, d.due_day) == (120, 1 + kitchen.SUPPLIERS["Pelto Produce Oy"]["lead_days"])
        with pytest.raises(ValueError):
            kitchen.mark_ordered(s, "hotel", potatoes.id, "test")             # already on order
    assert not rows(sheet_db)["Potatoes"].todo


def test_use_first_follows_the_newest_deliveries(sheet_db):
    with sheet_db.buyer_db() as s:
        for _ in range(4):
            kitchen.next_day(s)
        batches = kitchen.use_first(s, "hotel")
    assert all(b.days_left <= kitchen.USE_FIRST_DAYS for b in batches)
    assert all(b.quantity <= b.supply.on_hand for b in batches)


def test_stock_count_keeps_blank_fields(sheet_db):
    cream, eggs = supply(sheet_db, "Cream"), supply(sheet_db, "Eggs")
    with sheet_db.buyer_db() as s:
        result = kitchen.save_count(s, "hotel", {cream.id: "95", eggs.id: " "}, "test")
    assert result.updated == ["Cream"]
    assert supply(sheet_db, "Cream").on_hand == 95 and supply(sheet_db, "Eggs").on_hand == 900


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


def test_agent_order_from_dashboard_only_for_gateway_suppliers(sheet_db):
    with sheet_db.buyer_db() as s:
        r = kitchen.agent_order(s, "hotel", supply(sheet_db, "Butter").id, "40")
        assert r.text.startswith("Restock 40 kg of butter") and r.review_reason      # butter is 'ask first'
        with pytest.raises(ValueError):
            kitchen.agent_order(s, "hotel", supply(sheet_db, "Potatoes").id)        # no gateway
        with pytest.raises(ValueError):
            kitchen.agent_order(s, "hotel", supply(sheet_db, "Cream").id, "0")


def test_agent_suggests_one_delivery_for_items_due_soon(sheet_db):
    [sg] = kitchen.suggestions(list(rows(sheet_db).values()))
    assert sg.supplier == "Valio Aimo" and len(sg.rows) >= 2 and "in one delivery" in sg.body
    assert sg.title.endswith("run low this week")


def test_mark_ordered_takes_a_typed_quantity(sheet_db):
    with sheet_db.buyer_db() as s:
        d = kitchen.mark_ordered(s, "hotel", supply(sheet_db, "Coffee beans").id, "test", "15")
    assert d.quantity == 15


def test_reset_starts_the_simulation_again(sheet_db):
    from app.db import wipe
    with sheet_db.buyer_db() as s:
        kitchen.next_day(s)
        bind = s.get_bind()
    wipe(bind)
    with sheet_db.buyer_db() as s:
        assert s.scalar(select(Supply.id)) is None
        kitchen.seed(s, start=NOW.date())
        assert kitchen.today(s) == 1
    assert supply(sheet_db, "Lactose-free milk").on_hand == 760
