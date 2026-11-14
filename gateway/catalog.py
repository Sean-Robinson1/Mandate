"""Valio Aimo's catalog: static product search.

Search narrows on static data. Only the shortlist an agent then asks an offer for is sent to the price and
availability backend, as the mock API's documentation requires.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import settings as config
from protocol.wire import NotMatching, Product, SearchResult

# The backend takes up to 1000 ids per call but asks clients not to poll the whole catalog. 200 covers every
# product type buyers search for in the demo (milk: 73 matches); anything beyond is counted, never hidden.
MAX_RESULTS = 200


@lru_cache(maxsize=1)
def load_index() -> list[dict]:
    return json.loads(config.CATALOG_INDEX.read_text(encoding="utf-8"))


def products_by_id(ids) -> dict[str, dict]:
    wanted = set(ids)
    return {p["id"]: p for p in load_index() if p["id"] in wanted}


def display_name(p: dict) -> str:
    name = p["names"].get("en") or p["names"].get("fi") or p["id"]
    # The dataset has mis-encoded characters (C1 controls, U+FFFD); drop them so names render cleanly.
    keep = (ch for ch in name if ord(ch) not in (0xAE, 0xFFFD) and not 0x80 <= ord(ch) <= 0x9F)
    return " ".join("".join(keep).split())


def to_product(p: dict) -> Product:
    return Product(
        product_id=p["id"], name=display_name(p), brand=p.get("brand"), vendor=p["vendor"], category=p["category"],
        sales_unit=p["sales_unit"], gtin=p.get("gtin"), claims=p["claims"], contains=p["contains"],
        may_contain=p["may_contain"], has_allergen_data=p["has_allergen_data"], labels=p["labels"],
        reliability=p.get("reliability"), co2_per_kg=p.get("co2_per_kg"))


def _claim_words(claim: str) -> str:
    if claim.startswith("FREE_FROM_"):
        return claim[10:].replace("_", " ").lower() + "-free"
    return claim.replace("_", " ").lower()


def _searchable(p: dict) -> str:
    return " ".join(p["names"].values()).lower()


def _term_pattern(term: str) -> re.Pattern:
    # Match at the start of a word: "milk" finds "milk" and "milks", not "buttermilk".
    return re.compile(r"(?<!\w)" + re.escape(term.lower()))


def search(terms: list[str], category: str | None = None, exclude: list[str] = (), claims: list[str] = (),
           allergens: list[str] = (), index: list[dict] | None = None) -> SearchResult:
    """Products whose name contains every term (at the start of a word) and none of the exclude words.

    Products that match by name but lack a required claim or contain an excluded allergen are returned as
    near misses with the reason, so the buyer can see what was ruled out and why.
    """
    index = index if index is not None else load_index()
    wanted = [_term_pattern(t) for t in terms]
    excluded = [t.lower() for t in exclude]
    matches: list[dict] = []
    not_matching: list[NotMatching] = []
    for p in index:
        if category and not p["category"].startswith(category):
            continue
        text = _searchable(p)
        if not all(w.search(text) for w in wanted):
            continue
        if any(t in text for t in excluded):           # exclusion matches anywhere: errs on the side of caution
            continue
        missing = [c for c in claims if c not in p["claims"]]
        if missing:
            not_matching.append(NotMatching(product_id=p["id"], name=display_name(p),
                                            reason="is not " + " or ".join(_claim_words(c) for c in missing)))
            continue
        hit = [a for a in allergens if a in p["contains"]]
        if hit:
            not_matching.append(NotMatching(product_id=p["id"], name=display_name(p),
                                            reason=f"contains excluded allergen {', '.join(hit)}"))
            continue
        matches.append(p)
    return SearchResult(products=[to_product(p) for p in matches[:MAX_RESULTS]], not_matching=not_matching,
                        not_returned=max(0, len(matches) - MAX_RESULTS))
