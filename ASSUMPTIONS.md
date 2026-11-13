# Assumptions

Each entry: what we assumed, why, and what would replace it in production.

## Data and API

1. **Orders are mocked locally.** The mock API has no order endpoint, so `agent.place_order` stores orders in
   our database. Production: Valio Aimo's order API.
2. **Prices and availability are looked up once per request and not frozen.** At sign-off the policy is
   re-checked against the clock and the week's spend, but prices and stock are not fetched again.
   Planned (M4): an offer that bundles both lookups with an expiry no later than the order cut-off
   (`closingTime`), signed by the gateway.
3. **Net prices.** Spend limits and comparisons use prices before VAT (B2B). VAT-inclusive totals are shown.
4. **Litres or kg per sales unit come from the price API** (`catalogPrice / comparisonPrice`), rounded to
   2 decimals, because static packaging data is incomplete. If the comparison unit does not match the unit
   the buyer asked for, the product is excluded rather than converted.
5. **Orders are whole sales units.** `allowedLotSize` is 0.01 for most products, which we do not trust.
6. **A product past its order cut-off is not orderable**, even though the API still lists a delivery date.
7. **"Unlimited" availability** (`999999` for Terminal, `9999999999` for some Stock products) is treated as
   enough for any order, and flagged as "quantity not confirmed" for Terminal products.
8. **At most 200 matching products are priced per request**, because the API asks clients not to poll the
   whole catalog. Every product type in the lexicon has fewer matches; any beyond the limit are listed as
   "not priced" and noted on the plan, never dropped silently.
9. **Missing allergen data is treated as unsafe** for any buyer with allergen constraints.

## Reading requests

10. **"By Monday" means the next Monday after today, Helsinki time**, delivered on or before that date.
    Said on a Monday, it means next week's Monday. If several weekdays are named, the first one mentioned counts.
11. **Numbers:** a comma followed by groups of three digits is a thousands separator ("1,000" = 1000), as in
    English. A dot used the same way ("1.000") is read as 1000 too, but flagged so a person confirms it.
    A space is always a thousands separator ("1 000").
12. **A budget needs the word "budget" or a currency marker** (€, EUR). "Up to 500 litres" is a quantity.
13. **Product type matching uses a small lexicon** (e.g. "milk" = category 10400, excluding coffee, sour and
    flavoured variants). The most specific phrase wins ("oat milk" is an oat drink). Catalog names must
    contain each product term at the start of a word. Production: a maintained product taxonomy (GS1 GPC).
14. **Local LLM = Claude Code CLI** (`claude -p`) behind `LLMClient`; Gemini is the alternative and a
    rule-based parser is the fallback. The LLM only parses requests. Its reading must agree with the rule
    parser on product, quantity, unit and deadline, or a person decides; if only the LLM can read a request,
    a person decides too.

## Trust

15. **Demo keys are derived from fixed seeds** (`app/trust.py`), so signatures are identical on every run, and
    the demo mandates are signed at startup. Signature checks therefore show the mechanism (a changed limit
    fails verification) rather than real separation of authority. Production: the buyer organisation signs
    mandates offline and the gateway holds only their public keys; approvers sign with their own keys.
16. **The web console has no login.** Whoever can open it decides as the mandate's approver. Production:
    single sign-on, with the approver's identity bound to their signing key.
17. **The audit log is tamper-evident inside the database, not tamper-proof.** Edits, gaps and deletions from
    the end are detected, but someone who can rewrite every row and the head record can forge a consistent
    log. Production: publish the head hash periodically somewhere the database administrator cannot change.
