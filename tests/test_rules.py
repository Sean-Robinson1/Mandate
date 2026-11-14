"""Changing mandates: editing, signing new versions, Valio Aimo refusing old ones, and earned-trust suggestions."""
import json
from datetime import timedelta

from app import agent, keys, rules
from app.db import Run
from tests.conftest import NOW
from tests.test_agent import decide, order, supplier_orders


def form_for(mandate, **changes) -> dict:
    """The edit form as submitted: current values, with some changed."""
    values = rules.form_values(mandate)
    values.update(changes)
    form = {}
    for f in rules.FIELDS:
        v = values[f.path]
        if f.kind == "allergens":
            form.update({f"{f.path}.{a}": "1" for a in v})
        elif f.kind == "bool":
            if v:
                form[f.path] = "1"
        else:
            form[f.path] = str(v)
    return form


def amend(world, key="bistro", **changes):
    m = world.mandates[key]
    proposed, errors = rules.parse(form_for(m, **changes), m)
    assert errors == []
    new = keys.amend_mandate(key, m, rules.changes_only(m, proposed), "mikko.laine", "test")
    world.mandates[key] = new
    return m, new


# ---------------------------------------------------------------- the form

def test_unchanged_form_means_no_difference(world):
    m = world.mandates["nutfree"]
    proposed, errors = rules.parse(form_for(m), m)
    assert errors == [] and rules.diff(m, proposed) == []


def test_diff_names_changes_and_flags_more_freedom(world):
    m = world.mandates["nutfree"]
    proposed, _ = rules.parse(form_for(m, **{"limits.per_order_auto_eur": 1200,
                                             "hard_constraints.excluded_allergens": ["AN", "AP", "AS"]}), m)
    changes = {c["path"]: c for c in rules.diff(m, proposed)}
    assert changes["limits.per_order_auto_eur"]["old"] == "€800" and changes["limits.per_order_auto_eur"]["new"] == "€1,200"
    assert changes["limits.per_order_auto_eur"]["looser"] is True
    assert changes["hard_constraints.excluded_allergens"]["looser"] is False      # adding sesame is stricter


def test_dropping_an_allergen_is_flagged_as_more_freedom(world):
    m = world.mandates["nutfree"]
    proposed, _ = rules.parse(form_for(m, **{"hard_constraints.excluded_allergens": ["AP"]}), m)
    assert [c["looser"] for c in rules.diff(m, proposed)] == [True]


def test_inconsistent_limits_are_refused(world):
    m = world.mandates["bistro"]
    _, errors = rules.parse(form_for(m, **{"limits.per_order_auto_eur": 20000}), m)
    assert errors and "absolute maximum" in errors[0]
    _, errors = rules.parse(form_for(m, **{"limits.per_week_auto_eur": "a lot"}), m)
    assert errors


# ---------------------------------------------------------------- versions

def test_new_version_is_signed_and_old_one_kept(world):
    old, new = amend(world, **{"limits.per_order_auto_eur": 3500})
    assert new.version == old.version + 1 and new.changed_by == "mikko.laine"
    assert keys.verify_mandate(new) and new.limits.per_order_auto_eur == 3500
    assert [m.version for m in keys.mandate_history("bistro")] == [old.version]
    assert keys.load_mandates()["bistro"].version == new.version              # loaded unchanged afterwards


def test_raised_limit_lets_the_agent_order_alone(world):
    assert order(world, 3000).status == "NEEDS_HUMAN"
    amend(world, **{"limits.per_order_auto_eur": 3500})
    assert order(world, 3000).status == "ORDERED"


def test_valio_aimo_refuses_an_older_version_once_it_has_seen_a_newer_one(world):
    old, new = amend(world, **{"limits.per_order_auto_eur": 3500})
    assert world.client.publish_mandate(new.model_dump(mode="json"))["version"] == new.version
    world.mandates["bistro"] = old                    # an agent still holding (or replaying) the old version
    r = order(world, 1000)
    assert r.status == "REFUSED" and "replaced by version" in r.receipt
    assert supplier_orders(world) == 0


def test_publishing_an_older_version_is_refused(world):
    old, new = amend(world, **{"limits.per_order_auto_eur": 3500})
    world.client.publish_mandate(new.model_dump(mode="json"))
    status, body = world.client._call("PUT", "/v1/mandates", json=old.model_dump(mode="json"))
    assert status == 409 and "replaced" in body["error"]


def test_a_forged_new_version_is_refused(world):
    forged = world.mandates["bistro"].model_dump(mode="json")
    forged["version"] = 99
    forged["limits"]["per_order_auto_eur"] = 1_000_000
    status, _ = world.client._call("PUT", "/v1/mandates", json=forged)
    assert status == 403


def test_pending_approval_is_rechecked_under_the_new_rules(world):
    r = order(world, 3000)
    amend(world, **{"limits.per_order_auto_eur": 3500})
    with world.buyer_db() as s:
        row = agent.decide(s, r.trace_id, world.mandates["bistro"], approve=True, gateway=world.client,
                           now=NOW + timedelta(minutes=5))
    assert row.status == "APPROVED"


# ---------------------------------------------------------------- earned trust

def test_repeated_approvals_suggest_a_higher_limit(world):
    m = world.mandates["bistro"]
    with world.buyer_db() as s:
        assert rules.suggestions(s, m, now=NOW) == []
    for litres in (2600, 2700, 2600):            # 7,900 together: held back by the order limit only
        decide(world, order(world, litres).trace_id)
    with world.buyer_db() as s:
        [sg] = rules.suggestions(s, m, now=NOW)
    assert sg.path == "limits.per_order_auto_eur" and sg.current == 2500 and sg.proposed == 3000
    assert "approved all 3" in sg.evidence


def test_a_single_decline_means_no_suggestion(world):
    decide(world, order(world, 2600).trace_id, approve=False)
    for litres in (2600, 2600, 2600):
        decide(world, order(world, litres).trace_id)
    with world.buyer_db() as s:
        assert rules.suggestions(s, world.mandates["bistro"], now=NOW) == []


def test_orders_held_for_other_reasons_do_not_count(world):
    for litres in (2600, 2700, 2600):
        r = order(world, litres)
        with world.buyer_db() as s:                   # pretend each also needed approval for another reason
            row = s.get(Run, r.trace_id)
            policy = json.loads(row.policy_json)
            policy["reasons"].append({"rule": "band.price_premium", "outcome": "NEEDS_HUMAN", "message": "", "details": {}})
            row.policy_json = json.dumps(policy)
            s.commit()
        decide(world, r.trace_id)
    with world.buyer_db() as s:
        assert rules.suggestions(s, world.mandates["bistro"], now=NOW) == []
