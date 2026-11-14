# Data insights

Computed offline from `data/catalog_index.json` (17,546 products) and the two portal CSVs.
"Food" means top-level category codes 10, 11, 17, 18, 21, 22, 41, 52, 85 (13,520 products). Codes such as 20, 63, 64 and 90 are mostly tableware, packaging and office supplies, so they are left out.

## 1. One food product in eight has no allergen declaration
- **1,652 of 13,520 food products (12.2%)** have `has_allergen_data = false`. Across the whole catalog it is 3,079 of 17,546 (17.5%), but that includes non-food.
- Why it matters: an agent cannot treat "no allergens listed" as "allergen-free". Missing data counts as unsafe (ASSUMPTIONS.md item 9).

## 2. About 120 of those products name an allergen right in the product name
- **124 undeclared food products** match an allergen word in their English or Finnish name (milk, cream, cheese, yoghurt, egg, wheat, nut and nut types, fish, salmon, cod, herring, tuna, shrimp, prawn, soy, sesame, plus maito, kerma, juusto, kananmuna, vehnä, pähkinä, lohi, silli, soija, seesami and others). The match uses whole words and Finnish stems. A few are false positives ("almond potato", "pak-soy", "butter beans"), so about 120 is the real number.
- Examples: *PCD 500g Peanut Butter*, *Katjang Peanuts 1 kg*, *Peltolan Blue Cheese 1.6kg*, *Salmon fillet C-cut*, *Wheat baguette ready baked*, *Oh Aik Guan sesame oil*.
- Separately, 69 undeclared food products have allergens in CAPITALS in their ingredient text (the EU labelling style), even though the structured flag is missing.
- Why it matters: a matcher that trusts only the structured allergen fields would let "peanut butter" through for a nut-allergy kitchen. Blocking on missing data is the only safe default.

## 3. Delivery reliability is usually perfect, but has a long bad tail
- Coverage: 4,425 products (25% of the catalog) have a reliability score.
- **Median 1.00 and mean 0.972. 56% are at exactly 100%.** The worst decile starts at 0.936.
- **291 products (6.6%) are below 90%**, 150 are below 80% and 33 are below 50%. The minimum is 0.0.
- Only 1 of those 291 low-reliability products has a substitute listed in the catalog index.
- Why it matters: an agent ordering for a fixed service date has to spot risky lines and flag them or suggest alternatives, because the catalog almost never offers a substitute.

## 4. Carbon footprint data covers about 63% of the catalog and varies more than tenfold
- **11,033 products (62.9%)** have an average kg CO2-eq/kg value.
- Median 2.11, range 0.13 to 30.46. The 90th percentile (7.12) is about **12 times** the 10th percentile.
- Why it matters: a sustainability limit in a mandate can only be enforced where data exists. Products with no value have to be shown as "unknown", not counted as zero.

## Method
These numbers come from a throwaway script (`json` + `csv` + `re`, Python stdlib) that is not checked in. It used `has_allergen_data`, `names.en`/`names.fi`, `category`, `substitutes` and `ingredients_en` from the index, and the raw CSV values. Not checked: whether the category codes map exactly to food, and whether the CSV scores are consistent with the index's own `reliability` and `co2_per_kg` fields (the counts match: 4,425 and 11,033).
