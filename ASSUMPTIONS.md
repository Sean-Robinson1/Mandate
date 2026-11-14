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
10h. **The restaurant, Bistro Lumo Oy, is made up**, as are its people, menu-sized daily use and the supplier
    names Lihatalo, Riistatalo and Juustola. Its week is quiet on Mondays and busiest on Fridays and Saturdays.
    Only five of its items (salmon fillet, cream, butter, milk, grated cheese) are ordered by the agent; Valio Aimo
    dry goods without a product term (pasta, flour, sugar) are ordered by the kitchen in the webshop. A kitchen
    added after the first run is seeded at the current kitchen day, with its own four weeks of history.
10i. **The restaurant's service is simulated.** "Next day" seats about 120 guests on a normal day (scaled by its
    week) at lunch and dinner. Every guest takes a main; 35% a starter, 40% a dessert and 50% a coffee, each dish
    chosen by a fixed popularity weight. Each dish takes its recipe out of stock (after trimming losses), and
    sells out for the rest of the day when an ingredient runs short. The page plays back a day already computed.
    Production: the till's (POS) sales export.
10j. **Recipes, menu prices and portion costs are made up but realistic.** Prices of the agent's Valio Aimo items
    were set near real offers from the mock API. Once the agent buys an item, its cost per portion uses the
    price Valio Aimo confirmed. Oil, seasoning and garnish are a fixed amount per dish, not tracked as stock.
    Food cost is measured against the price before VAT, taken as 13.5% for restaurant food. Allergens per dish
    come from the kitchen's declaration for each item, plus Valio Aimo's product data for what it delivered.
10l. **The restaurant starts with two weeks of past services and waste**, simulated the same way as a live day,
    so the menu, service and food cost pages have a history on day one. They add sales and waste records only;
    the seeded stock history is unchanged. After each live service a closing check bins stock past its use-by
    date and logs a little over-prepared food (2–5% of one or two items' use). Production: the till's sales export
    and the kitchen's own waste log.
10n. **Each course has a target gross margin** (starters 72%, mains 70%, desserts 80%, drinks 85%), so drinks and
    desserts carry more margin than mains, as on real menus. A dish below its target gets the lowest menu price
    that reaches it, rounded up to €0.50. Only the approver named in the mandate (the owner) can change a menu
    price; each change is in the audit log. A dish's cost uses the best price evidence available: Valio Aimo's
    signed price for the product in use, then the last confirmed order, then the recorded price history
    (simulated before the first real offer, see app/prices.py), then the list price.
10o. **A fresh demo starts with a morning stock count** that finds a few items low in each kitchen (hotel: cream
    and butter; school: lactose-free milk and oat drink; restaurant: cream and grated cheese). The agent then acts
    for real through Valio Aimo's gateway: items set to "agent restocks" are ordered alone, items set to "ask
    first" wait for the kitchen's approver. The restaurant's standing orders with its other suppliers are already
    on the way. This happens once, on day 1 before any request, and again after "Reset demo".
10m. **Waste is costed at the item's current price** (Valio Aimo's confirmed price once the agent has bought it,
    else the list price) and leaves stock at once. Food cost is shown from recipes, and with logged waste added;
    the gap nobody logged (recipes against stock counts) is not calculated yet.
10k. **The restaurant's other suppliers deliver on standing orders**: when an item the agent does not order runs
    low and nothing is on the way, an order for the suggested quantity is recorded, due after the supplier's lead
    time. The morning's deliveries are taken in before service. Production: the chef's own orders, or a supplier
    rep's standing order, and the kitchen's delivery check-in.
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
18a. **The agent asks rather than refuses whenever a person could allow it** (owner's call, 14 Nov). Price,
    over-ordering, shortfall and late delivery beyond the mandate's `*_max` bands now go to the approver, flagged
    as unusual. The agent only stops without asking when no approval could help: the hard limits in 18, an
    invalid or expired mandate, nothing suitable in time, or a passed cut-off; it then says why and what to do.
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

22a. **"Try to cheat" runs real tricks against the real gateway**, signed with the kitchen's real agent key, and
    shows Valio Aimo's actual answers. Two are not pure gateway refusals: an edited price is shown failing the
    signature check against Valio Aimo's published key, because an order carries only the offer number and the
    gateway prices it from its own stored copy; and "reused offer" uses the kitchen's last placed order, or places
    one genuine 1-unit order first if there is none. "Old rules" is skipped until the kitchen has signed a second
    mandate version. Tricks use one unit of a cheap product; the two over-the-limit tricks go just past the agent's
    own limit, so a wrong acceptance there would be a real order of that size. The last run per kitchen is kept in
    memory only (each run is in the audit log).

## Ingredient prices

27. **Ingredient prices are recorded, not looked up.** Valio Aimo's API gives today's price only, so every signed
    offer the agent receives is kept per stock item, product and day. Before that, each item gets four weeks of
    simulated prices at its unit cost with small drift, plus a few deliberate moves (cream, butter, salmon fillet,
    grated cheese) and one or two real catalog products as alternatives, so the page has something to show.
28. **A price change compares like with like:** the same product from the same source. Switching products, or the
    first real Valio Aimo price arriving after simulated history, is not shown as a price change. Moves under 3%
    within 14 days are treated as noise. The € per week effect uses the item's typical week of use.
29. **A cheaper equivalent** is another product Valio Aimo offered for the same stock item, priced per the same
    unit, within the last 14 days, and from the same source as the current price. Ordering it goes through the
    agent and the purchasing rules like any request. Recipe and dish impact follows once the menu lands.
30. **Specials against waste** are suggested when typical use (the weekly shape), taking stock oldest first, would
    leave at least €25 of a fresh item past its date within a week. The suggested dish is the main that uses most of
    it, at 20% off (10% if 20% would sell below ingredient cost). A dish on special is picked 2.5 times as often in
    the simulated service: a made-up figure, as we have no price-response data. A manager starts it; it is audited.
31. **Demo starting stock** for the restaurant is set from what its recipes actually use over two weeks of
    simulated service. On the first morning, yesterday's delivery brought 24 kg of salmon and 12 kg of lettuce
    more than ordered, so the waste alert has something to act on from the start.
