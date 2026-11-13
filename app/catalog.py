"""Static catalog search plus live enrichment into Candidates.

Search narrows on static data first; only the shortlist is sent to the price/availability API,
as the API documentation requires.
"""
from __future__ import annotations

import json
from functools import lru_cache

from app import config
from app.models import Candidate, IntentSpec, Rejection, Unit
from app.valio_client import ValioAPI

UNLIMITED = 999_999          # Terminal products report this; some Stock products report 9999999999
MAX_SHORTLIST = 60


@lru_cache(maxsize=1)
def load_index() -> list[dict]:
    return json.loads(config.CATALOG_INDEX.read_text(encoding="utf-8"))


def product_by_id(product_id: str) -> dict | None:
    return next((p for p in load_index() if p["id"] == product_id), None)


def display_name(p: dict) -> str:
    name = p["names"].get("en") or p["names"].get("fi") or p["id"]
    return " ".join(name.replace("®", "").replace("\u0099", "").split())


def _searchable(p: dict) -> str:
    return " ".join(p["names"].values()).lower()


def search(intent: IntentSpec, index: list[dict] | None = None) -> tuple[list[dict], list[Rejection]]:
    """Static shortlist. Returns (matches, near-misses rejected on a stated requirement)."""
    index = index if index is not None else load_index()
    matches, rejected = [], []
    for p in index:
        if intent.category and not p["category"].startswith(intent.category):
            continue
        text = _searchable(p)
        if not all(t.lower() in text for t in intent.product_terms):
            continue
        if any(t.lower() in text for t in intent.exclude_terms):
            continue
        missing = [c for c in intent.required_claims if c not in p["claims"]]
        if missing:
            rejected.append(Rejection(product_id=p["id"], name=display_name(p),
                                      reason=f"does not carry required claim {', '.join(missing)}"))
            continue
        hit = [a for a in intent.excluded_allergens if a in p["contains"]]
        if hit:
            rejected.append(Rejection(product_id=p["id"], name=display_name(p),
                                      reason=f"contains excluded allergen {', '.join(hit)}"))
            continue
        matches.append(p)
    return matches[:MAX_SHORTLIST], rejected


def enrich(products: list[dict], intent: IntentSpec, api: ValioAPI) -> tuple[list[Candidate], list[Rejection]]:
    """Attach live price and availability. Products that cannot be priced or measured are rejected with a reason."""
    if not products:
        return [], []
    ids = [p["id"] for p in products]
    prices = api.prices(ids, intent.deadline)
    avail = api.availability(ids, intent.deadline)
    out: list[Candidate] = []
    rejected: list[Rejection] = []
    for p in products:
        name = display_name(p)
        pr, av = prices.get(p["id"]), avail.get(p["id"])
        if pr is None or pr.error or pr.catalogPrice is None:
            rejected.append(Rejection(product_id=p["id"], name=name, reason="no price returned by Valio Aimo"))
            continue
        if intent.unit in (Unit.L, Unit.KG):
            if pr.comparisonUnit != intent.unit.value or not pr.comparisonPrice or pr.comparisonPrice.value <= 0:
                rejected.append(Rejection(
                    product_id=p["id"], name=name,
                    reason=f"not priced per {intent.unit.value} (comparison unit {pr.comparisonUnit})"))
                continue
            measure = round(pr.catalogPrice.value / pr.comparisonPrice.value, 2)
            comparison_price = pr.comparisonPrice.value
        else:
            measure, comparison_price = 1.0, pr.catalogPrice.value
        line = av.scheduleLines[0] if av and av.scheduleLines else None
        amount = line.availableAmount if line else 0.0
        out.append(Candidate(
            product_id=p["id"], name=name, vendor=p["vendor"], brand=p.get("brand"), gtin=p.get("gtin"),
            category=p["category"], sales_unit=p["sales_unit"],
            unit_price=pr.catalogPrice.value,
            unit_price_with_tax=pr.catalogPrice.valueWithTax or pr.catalogPrice.value,
            measure_per_unit=measure, comparison_unit=pr.comparisonUnit or intent.unit.value,
            comparison_price=comparison_price,
            available_units=min(amount, UNLIMITED),
            quantity_confirmed=bool(av and av.logisticModel == "Stock"),
            available_date=line.availableDate if line else None,
            closing_time=av.closingTime if av else None,
            sales_block=bool((av and av.salesBlock) or (line and line.salesBlock)),
            logistic_model=av.logisticModel if av else None,
            claims=p["claims"], contains=p["contains"], may_contain=p["may_contain"], free_from=p["free_from"],
            has_allergen_data=p["has_allergen_data"], labels=p["labels"],
            reliability=p.get("reliability"), co2_per_kg=p.get("co2_per_kg"),
            description=p.get("description_en"),
        ))
    return out, rejected


def find_candidates(intent: IntentSpec, api: ValioAPI) -> tuple[list[Candidate], list[Rejection]]:
    products, static_rejected = search(intent)
    candidates, live_rejected = enrich(products, intent, api)
    return candidates, static_rejected + live_rejected
