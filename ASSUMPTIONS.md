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
9a. **The gateway sends its own User-Agent** (`trusted-trade-gateway/0.4`) because the mock API sits behind
   Cloudflare, which reportedly blocks default Python client user agents with 403.

## Kitchen

10a. **Kitchen days are simulated.** "Next day" uses each item's daily use and delivers orders whose lead time has
    passed; the lead time is the real one from Valio Aimo's offer (order date to delivery date), counted in kitchen
    days. Production: stock from the kitchen's inventory system or stocktakes, deliveries from Valio Aimo's
    delivery notes.
10b. **The agent restocks up to the "order up to" level**, rounded up to 10 L or 5 kg, and only when nothing for
    that row is already on the way or being decided. "Ask first" rows always go to the purchasing manager; that
    rule lives on the buyer's side (Valio Aimo enforces the mandate, not the kitchen's preferences).
10c. **Four weeks of history are generated on first run**, worked backwards from today's stock with ±15% daily
    noise and deliveries whenever the stock would have passed "order up to". Day 1 is the real date the demo was
    seeded; "Next day" moves the kitchen calendar on, while orders to Valio Aimo still use real dates.
10d. **Use follows a fixed week per kitchen** (the school uses nothing at weekends, the hotel is busier at
    weekends). The forecast runs that week at the typical daily use and counts deliveries on the way; it does not
    assume the agent will reorder. Production: learn the pattern from the kitchen's own history and menus.
10e. **Other suppliers have no ordering gateway**, so their items are tracked but ordered by the kitchen, which
    ticks it off ("Mark as ordered": the suggested quantity, due after the supplier's lead time). Supplier names,
    lead times, delivery days, unit costs and shelf lives are made up. A delivery adds to stock when someone checks
    it in; one not checked in by the next day is taken in automatically so the demo never stalls. Production:
    delivery notes (GS1 DESADV) or the supplier's confirmation.
10g. **Use-by dates assume stock is used oldest first**: what is left is the newest deliveries, each dated from
    its arrival plus the item's shelf life. Production: dates read from delivery notes or labels per batch.
10f. **Stock systems connect by CSV export or a push API.** Items are matched by exact name (ignoring case);
    unknown names are reported, never created. The push API uses one shared token. Production: per-system
    credentials, and matching by GTIN or the kitchen system's own item code.

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

## Reasonable orders, resends and products

20. **"Unusual amount" compares a request with the kitchen's own usual week**: the item's typical daily use
    times its week pattern, from the kitchen's stock records. The limit (default 3 weeks) is in the signed
    mandate, so the purchasing manager sets it. A request for an item the kitchen does not stock, or in another
    unit, is not checked by this rule. The rule is code, not the LLM; it can only send an order to a person.
21. **Orders are never dropped when Valio Aimo does not answer.** The signed order is saved before sending, tried
    three times, then queued and resent every 20 seconds. Valio Aimo answers a resend of an order it already
    confirmed (same mandate, buyer reference, offer and lines) with the same signed confirmation, so a lost answer
    never causes a second order. Production: a durable outbox and Valio Aimo's order API with idempotency keys.
22. **Product pages show Valio Aimo's catalog facts through its gateway.** We could not confirm the webshop's
    product link format, so the "Open in Valio Aimo's webshop" link appears only when `VALIO_SHOP_URL` is set.
