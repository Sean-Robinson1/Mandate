"""Static catalog search: word matching, exclusions and the shortlist limit."""
from datetime import date

from app import catalog
from app.models import IntentSpec, Unit


def intent(terms, exclude=(), claims=()) -> IntentSpec:
    return IntentSpec(raw_text="t", product_terms=list(terms), exclude_terms=list(exclude), quantity=10,
                      unit=Unit.KG, deadline=date(2026, 11, 16), required_claims=list(claims))


def product(pid, name, claims=()) -> dict:
    return {"id": pid, "names": {"en": name}, "category": "10400", "claims": list(claims), "contains": [],
            "may_contain": [], "free_from": [], "has_allergen_data": True, "labels": [], "vendor": "V",
            "sales_unit": "ST"}


def test_terms_match_at_the_start_of_a_word():
    index = [product("1", "Butter 500g"), product("2", "JEPO Cooked SageButterflavored Potato3kg"),
             product("3", "Cocoa butter"), product("4", "Peanutbutter"), product("5", "Butters, salted")]
    matches, _ = catalog.search(intent(["butter"]), index)
    assert [p["id"] for p in matches] == ["1", "3", "5"]


def test_exclusions_match_anywhere():
    index = [product("1", "Milk 1l"), product("2", "Coffeemilk 1l"), product("3", "Chocolate milk")]
    matches, _ = catalog.search(intent(["milk"], exclude=["coffee", "chocolate"]), index)
    assert [p["id"] for p in matches] == ["1"]


def test_missing_required_claim_is_reported_not_silently_dropped():
    index = [product("1", "Milk", claims=["FREE_FROM_LACTOSE"]), product("2", "Milk")]
    matches, rejected = catalog.search(intent(["milk"], claims=["FREE_FROM_LACTOSE"]), index)
    assert [p["id"] for p in matches] == ["1"]
    assert [(r.product_id, r.reason) for r in rejected] == [("2", "is not lactose-free")]


def test_matches_beyond_the_shortlist_are_reported_as_not_priced():
    index = [product(str(i), f"Milk {i}") for i in range(catalog.MAX_SHORTLIST + 5)]
    matches, rejected = catalog.search(intent(["milk"]), index)
    assert len(matches) == catalog.MAX_SHORTLIST
    assert len(rejected) == 5 and all(r.reason.startswith(catalog.NOT_PRICED) for r in rejected)
