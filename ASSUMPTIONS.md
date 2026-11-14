# Assumptions

Each entry: what we assumed, why, and what would replace it in production.

## Data and API

1. **Valio Aimo's gateway keeps its own orders.** The challenge's mock API has no order endpoint, so the gateway
   records offers and orders in its own database (`gateway.db`, separate from the buyer's). Production: the
   gateway passes accepted orders to Valio Aimo's order system.
2. **An offer is one price lookup plus one availability lookup, frozen and signed**, valid for 15 minutes and
   usable once. If it expires while waiting for approval, the agent asks for a new one; if prices or stock
   changed, nothing is ordered and the request must be asked again.
3. **Net prices.** Spend limits and comparisons use prices before VAT (B2B). VAT-inclusive totals are shown.
4. **Litres or kg per sales unit come from the offer's prices** (`unit_price / comparison_price`), rounded to
   2 decimals, because static packaging data is incomplete. If the comparison unit does not match the unit
   the buyer asked for, the product is excluded rather than converted.
5. **Orders are whole sales units.** `allowedLotSize` is 0.01 for most products, which we do not trust.
6. **A product past its order cut-off is not orderable**, even though the API still lists a delivery date.
7. **"Unlimited" availability** (`999999` for Terminal, `9999999999` for some Stock products) is treated as
   enough for any order, and flagged as "quantity not confirmed" for Terminal products.
8. **A search returns at most 200 products**, because the backend asks clients not to poll the whole catalog.
   Every product type in the lexicon has fewer matches; any beyond the limit are counted and noted on the plan.
9. **Missing allergen data is treated as unsafe** for any buyer with allergen constraints.

## Kitchen

10a. **Kitchen days are simulated.** "Next day" uses each item's daily use and delivers orders whose lead time has
    passed; the lead time is the real one from Valio Aimo's offer (order date to delivery date), counted in kitchen
    days. Production: stock from the kitchen's inventory system or stocktakes, deliveries from Valio Aimo's
    delivery notes.
10b. **The agent restocks up to the "order up to" level**, rounded up to 10 L or 5 kg, and only when nothing for
    that row is already on the way or being decided. "Ask first" rows always go to the purchasing manager; that
    rule lives on the buyer's side (Valio Aimo enforces the mandate, not the kitchen's preferences).

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
    rule-based parser is the fallback. The LLM only reads requests. Its reading must agree with the rule
    parser on product, quantity, unit and deadline, or a person decides; if only the LLM can read a request,
    a person decides too.

## Trust

15. **Every party has its own key, created on first run** (`keys/`): Valio Aimo's gateway key; each buyer
    organisation's key; each buyer agent's key; each approver's key. The organisation signs its mandate once,
    naming its agent's and approver's public keys; the signed file is loaded as is. All keys live on one machine
    for the demo. Production: each in its owner's key store (HSM, device keychain), never side by side.
16. **Onboarding is a shared folder.** A buyer organisation "registers" its public key with Valio Aimo by writing
    it to `keys/registry/`. Production: Valio Aimo's customer onboarding, with identity checks.
17. **Signing in is picking a name.** The console's person switcher stands in for single sign-on. Approval is
    still bound to the right key: only the approver named in the mandate can decide, and Valio Aimo verifies
    the approver's signature before accepting the order.
17a. **Mandate changes are signed by the organisation on the purchasing manager's say-so.** The manager reviews
    the difference and the organisation's key signs the next version; it is published to Valio Aimo, which keeps
    the highest version per mandate and refuses older ones. Valio Aimo also learns a newer version the first time
    an order carries it. Limit suggestions only look at orders held back by the per-order limit alone, need three
    approvals in 30 days and no declines, and never change anything themselves.
18. **Valio Aimo enforces the mandate's hard limits; the buyer enforces everything.** The gateway checks the
    signatures, validity, allergens, claims, categories, the absolute order cap and the agent's spend authority.
    Softer judgement (price bands, preferences, confidence in the request) stays with the buyer's policy engine.
19. **The audit log is tamper-evident inside the database, not tamper-proof.** Edits, gaps and deletions from
    the end are detected, but someone who can rewrite every row and the head record can forge a consistent
    log. Production: publish the head hash periodically somewhere the database administrator cannot change.
