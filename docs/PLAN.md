# Plan: kitchen operations dashboard + trusted ordering (decided 14 Nov 2026)

## The pitch (equal billing)
Two halves, one story:
1. **Run your kitchen**: menu, recipes, live sales, stock, waste, food cost, which dishes earn money.
2. **Order safely through Valio Aimo**: the agent turns kitchen needs into signed offers and orders, within the
   signed mandate, with a person approving what is outside it. (Everything we already have.)

Why it fits the brief: recipes unlock the brief's scenarios we marked "not done" (need → recipe → order,
last-minute event, recipe-compatible substitute, standing-order drift). The kitchen gives the agent real reasons
to order; the gateway and rules stay the spine. Do not pitch a generic restaurant app.

## What restaurants actually struggle with (research, mostly vendor sources, treat numbers as rough)
- Food cost under inflation: inflation is operators' #1 2026 challenge; target food cost 28–35% of sales.
- Theoretical vs actual usage (dishes sold × recipe vs what counts show) is the key control number; the gap is
  waste, over-portioning, theft.
- 4–10% of purchased food is wasted; main causes are over-prep, poor rotation, ordering without usage data.
- Stock counts take hours and get estimated; counts only pay off when they feed ordering and costing.
- Par levels = use per delivery cycle + safety stock, with weekend peaks.
- Supplier price creep; weekly price tracking recommended.
- Menu engineering: popularity × margin → stars, plowhorses, puzzles, dogs.
- Finland: own-check plan must cover where allergen info is kept (and temperature records — out of scope).

## Decisions
- Kitchens: **add a restaurant** with the full menu experience; keep the hotel (current flow) and school
  (strict allergens). Restaurant needs a person, a mandate and keys like the others (`app/mandates.py`,
  `app/keys.py`, `scripts/reset.py`).
- Recipe ingredients point at the kitchen's **supply rows** (`Supply` in `app/db.py`), not catalog products.
  Cost per portion uses the supply's latest price (signed Valio Aimo price where we have one).
- **Next day = live service**: a feed of tickets (time, table, dishes) streams in for a few seconds, stock drains
  through recipes, then an end-of-day summary (covers, revenue, food cost %, what ran low, what to use first).
  Simulated; record in `ASSUMPTIONS.md`. Weekend peaks via the existing `WEEK` shares.
- The LLM still only reads text and suggests; all ordering goes through `app/policy.py` and the gateway.
- Out of scope: staff rotas, reservations, real POS integration, accounting, temperature logging.

## Work items and owners
Claim an item by putting your name in the Owner column and pushing before you start. Keep to the listed files
where possible to avoid merge conflicts.

| # | Item | Main files | Owner | Status |
|---|------|-----------|-------|--------|
| 1 | Restaurant kitchen: person, mandate, keys, supplies, weekly shape | `app/mandates.py`, `app/kitchen.py` (`DEFAULT_SUPPLIES`, `WEEK`), `app/intent.py` (salmon fillet, grated cheese) | Sean + Claude | Done |
| 2 | Menu & recipes: `Dish`, `RecipeLine` tables, 11 restaurant dishes, cost per portion (confirmed Valio Aimo price when bought), allergens from declarations + product data; Menu page | `app/menu.py`, `app/db.py`, `app/templates/menu.html` | Sean + Claude | Done |
| 3 | Live service on Next day: ticket playback (computed, then played over ~9 s), stock drains via recipes, dishes sell out, end-of-day summary; standing orders for non-agent suppliers | `app/menu.py`, `app/kitchen.py` (`next_day`, `standing_orders`), `app/web.py`, `app/templates/service.html` | Sean + Claude | Done |
| 4 | Food cost page: waste log with reasons, food cost % vs 28–35% target (from recipes and with waste), day by day, dishes and ingredients driving it; closing check; two weeks of sample history. Still open: theoretical vs actual after a stock count | `app/costs.py`, `app/templates/costs.html` | Sean + Claude | Done, except theoretical vs actual |
| 4b | Approval screen reworked: price vs what we usually pay, why the agent asked, arrival vs deadline, stock, weekly spend, dishes affected, what declining means; technical details below | `app/review.py`, `app/templates/_decision.html`, `approvals.html` | Sean + Claude | Done |
| 5 | Menu and margins: target margin per course, suggested price, editable menu price (owner only, audited), price changes shown per dish. Ported from Sam's `menu-margins` branch onto main's data; dish costs now use the price history in `app/prices.py`. Still open: the popularity × margin chart | `app/menu.py`, `app/templates/menu.html` | Sam's design, ported by Sean + Claude | Done, except the chart |
| 6 | AI: event booking → order. "40 extra covers tomorrow" → recipes × covers → shortfall → offers → order or approval | `app/intent.py`, `app/agent.py`, `app/menu.py` | open | — |
| 7 | AI: recipe-aware substitute on stock-out, allergens checked, trade-off explained (only suggests; ordering via policy). Real data hook: on 14 Nov only 1 of ~30 salmon fillets in the mock API had stock; rainbow trout and whitefish fillets were plentiful | `app/planner.py`, `app/menu.py` | open | — |
| 8 | AI: use-it-up specials from stock near its date (`use_first` in `app/kitchen.py`) | `app/menu.py`, `app/llm.py` | open | — |
| 9 | Drift alert: a standing order creeping above what sales explain is flagged before it renews | `app/kitchen.py`, `app/policy.py` (stricter only) | open | — |
| 11 | Ingredient prices: keep every signed Valio Aimo price per stock item and product; price-change alerts per ingredient (€/week effect); cheaper equivalent products with one-click order through the agent; dish impact plugs into item 2/5 once `Dish`/`RecipeLine` land | new `app/prices.py`, `app/db.py` (`PriceRecord`), `app/agent.py` (one hook), new `app/templates/prices.html` | Sam + Claude | Done (dish impact waits for item 2) |
| 12 | Try again: a request the agent did not order (refused, failed, offer changed, declined) can be retried or edited and resent; held back while the same item has a request in progress | `app/agent.py`, `app/web.py`, `app/templates/run.html` | Sam + Claude | Done |
| 10 | Pitch/demo update: `docs/DEMO.md`, `docs/CHALLENGE_CHECKLIST.md`, `README.md` | docs | open | after 1–6 |

Items 4–9 depend on 2 (recipes), which is now in `app/menu.py`: `dishes(session, org, today)` returns each dish
with its ingredients, prices, cost, food cost % and last week's sales; `day_summary(session, org, day)` returns a
day's tickets and takings. Sales are in the `sales` table (served=0 marks a dish that had sold out).

Found while testing: on a busy Friday the restaurant runs out of cream, because the agent's reorder level is the
same every day of the week. Weekend-aware reorder levels would be a good addition to item 9.

## Data shape (as built, `app/db.py`)
```
Dish:       id, org_key, name, category (starter/main/dessert/drink), price (with VAT), weight, extras, active, position
RecipeLine: id, dish_id, supply_id, quantity (supply's unit, per portion), yield_pct
Sale:       id, org_key, day, ticket, minute, table_no, covers, dish_id, quantity, price, cost, served
Waste:      id, supply_id, day, quantity, reason, by                  # item 4, not built
```
