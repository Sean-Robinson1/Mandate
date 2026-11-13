"""Build a slim, committed catalog index from the 87 MB portal product file.

Usage: python scripts/build_index.py [path-to-product-json]
Output: data/catalog_index.json (deterministic: sorted by product id).
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "docs" / "portal"
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else PORTAL / "valio_aimo_junction26_product_data.json"


def lang_map(entries):
    return {e["language"]: e["value"] for e in entries or [] if e.get("value")}


def read_csv(name):
    path = PORTAL / name
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))[1:]
    return {r[0]: float(r[1]) for r in rows if len(r) > 1 and r[1]}


def base_unit_measure(synkka):
    """Net content of ONE base unit, from supplier packaging data, if stated."""
    for conv in synkka.get("unitConversions") or []:
        if conv.get("synkkaPackagingType") != "BASE_UNIT_OR_EACH":
            continue
        vol, wt = conv.get("volume"), conv.get("netWeight")
        litres = vol["value"] if vol and vol.get("unit") == "LTR" else None
        if vol and vol.get("unit") == "MLT":
            litres = vol["value"] / 1000
        kg = None
        if wt and wt.get("unit") == "KGM":
            kg = wt["value"]
        elif wt and wt.get("unit") == "GRM":
            kg = wt["value"] / 1000
        return litres, kg
    return None, None


def slim(p, reliability, carbon):
    s = p["synkkaData"]
    enums = {c["name"]: c["values"] for c in s.get("classifications", []) if c["type"] == "ENUM"}
    has_allergen_data = "allergen" in enums and bool(s.get("gtin"))
    sales_unit_size = next((u["sizeInBaseUnits"] for u in p["units"] if u["unitId"] == p["salesUnit"]), None)
    litres, kg = base_unit_measure(s)
    return {
        "id": p["id"],
        "gtin": p.get("salesUnitGtin") or s.get("gtin"),
        "names": lang_map(s.get("names")),
        "brand": (s.get("brand") or "").strip() or None,
        "vendor": p["vendorName"],
        "category": p["category"],
        "country": p.get("countryOfOrigin"),
        "sales_unit": p["salesUnit"],
        "base_unit": p["baseUnit"],
        "sales_unit_size": sales_unit_size,
        "base_litres": litres,
        "base_kg": kg,
        "contains": sorted(v["id"] for v in enums.get("allergen", []) if v.get("unit") == "CONTAINS"),
        "may_contain": sorted(v["id"] for v in enums.get("allergen", []) if v.get("unit") == "MAY_CONTAIN"),
        "free_from": sorted(v["id"] for v in enums.get("nonAllergen", [])),
        "claims": sorted({v["id"] for v in enums.get("nutritionalClaim", [])}),
        "labels": sorted(v["id"] for v in enums.get("packagingMarkedLabel", []) if "synkkaId" in v),
        "has_allergen_data": has_allergen_data,
        "ingredients_en": lang_map(s.get("keyIngredients")).get("en"),
        "description_en": (lang_map(s.get("marketingTexts")).get("en") or "")[:400] or None,
        "substitutes": [x["substituteProduct"] for x in p.get("substitutions", [])],
        "reliability": reliability.get(p["id"]),
        "co2_per_kg": carbon.get(p["id"]),
    }


def main():
    products = json.loads(SRC.read_text(encoding="utf-8"))
    reliability = read_csv("valio_aimo_junction26_delivery_reliability.csv")
    carbon = read_csv("valio_aimo_junction26_carbon_footprint.csv")
    out = sorted((slim(p, reliability, carbon) for p in products if not p["deleted"]), key=lambda x: x["id"])
    dest = ROOT / "data" / "catalog_index.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {len(out)} products, {dest.stat().st_size/1e6:.1f} MB -> {dest}")


if __name__ == "__main__":
    main()
