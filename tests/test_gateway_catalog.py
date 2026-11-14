"""Valio Aimo's catalog search: word matching, exclusions, near misses and the result limit."""
from gateway import catalog


def product(pid, name, claims=(), contains=()) -> dict:
    return {"id": pid, "names": {"en": name}, "category": "10400", "claims": list(claims), "contains": list(contains),
            "may_contain": [], "free_from": [], "has_allergen_data": True, "labels": [], "vendor": "V",
            "sales_unit": "ST"}


def ids(result) -> list[str]:
    return [p.product_id for p in result.products]


def test_terms_match_at_the_start_of_a_word():
    index = [product("1", "Butter 500g"), product("2", "JEPO Cooked SageButterflavored Potato3kg"),
             product("3", "Cocoa butter"), product("4", "Peanutbutter"), product("5", "Butters, salted")]
    assert ids(catalog.search(["butter"], index=index)) == ["1", "3", "5"]


def test_exclusions_match_anywhere():
    index = [product("1", "Milk 1l"), product("2", "Coffeemilk 1l"), product("3", "Chocolate milk")]
    assert ids(catalog.search(["milk"], exclude=["coffee", "chocolate"], index=index)) == ["1"]


def test_near_misses_are_reported_with_the_reason():
    index = [product("1", "Milk", claims=["FREE_FROM_LACTOSE"]), product("2", "Milk"),
             product("3", "Milk", claims=["FREE_FROM_LACTOSE"], contains=["AN"])]
    result = catalog.search(["milk"], claims=["FREE_FROM_LACTOSE"], allergens=["AN"], index=index)
    assert ids(result) == ["1"]
    assert [(r.product_id, r.reason) for r in result.not_matching] == [
        ("2", "is not lactose-free"), ("3", "contains excluded allergen AN")]


def test_matches_beyond_the_limit_are_counted_not_hidden():
    index = [product(str(i), f"Milk {i}") for i in range(catalog.MAX_RESULTS + 5)]
    result = catalog.search(["milk"], index=index)
    assert len(result.products) == catalog.MAX_RESULTS and result.not_returned == 5
