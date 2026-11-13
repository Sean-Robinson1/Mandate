# Assumptions

Each entry: what we assumed, why, and what would replace it in production.

1. **Orders are mocked locally.** The mock API has no order endpoint, so `app/orders.py` stores orders in our
   database behind the same gateway interface. Production: Valio Aimo's order API.
2. **An offer = one price lookup + one availability lookup, frozen together.** The gateway bundles them into
   an offer with an expiry no later than the order cut-off (`closingTime`).
3. **Net prices.** Spend limits and comparisons use prices before VAT (B2B). VAT-inclusive totals are shown.
4. **Litres or kg per sales unit come from the price API** (`catalogPrice / comparisonPrice`), rounded to
   2 decimals, because static packaging data is incomplete. If the comparison unit does not match the unit
   the buyer asked for, the product is excluded rather than converted.
5. **Orders are whole sales units.** `allowedLotSize` is 0.01 for most products, which we do not trust.
6. **A product past its order cut-off is not orderable**, even though the API still lists a delivery date.
7. **"By Monday" means the next Monday on or after today, Helsinki time**, delivered on or before that date.
8. **"Unlimited" availability** (`999999` for Terminal, `9999999999` for some Stock products) is treated as
   enough for any order, and flagged as "quantity not confirmed" for Terminal products.
9. **Product type matching uses a small lexicon** (e.g. "milk" = category 10400, excluding coffee, sour and
   flavoured variants). Production: a maintained product taxonomy (GS1 GPC).
10. **Missing allergen data is treated as unsafe** for any buyer with allergen constraints.
11. **Local LLM = Claude Code CLI** (`claude -p`) behind `LLMClient`; a rule-based parser is the fallback.
    The LLM only parses requests and words explanations.
