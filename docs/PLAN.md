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
| 1 | Restaurant kitchen: person, mandate, keys, supplies, weekly shape | `app/mandates.py`, `app/kitchen.py` (`DEFAULT_SUPPLIES`, `WEEK`), `scripts/reset.py` | Sean + Claude | In progress |
| 2 | Menu & recipes: `Dish`, `RecipeLine` tables, seed ~10 dishes per kitchen, cost per portion, allergens rolled up from product data; Menu page | new `app/menu.py`, `app/db.py`, `app/templates/menu.html` | Sean + Claude | In progress |
| 3 | Live service on Next day: ticket feed (SSE, like the agent stream), stock drains via recipes, end-of-day summary | `app/menu.py`, `app/kitchen.py` (`next_day`), `app/web.py`, new `app/templates/service.html` | Sean + Claude | Next |
| 4 | Food cost page: theoretical vs actual after a count, waste log with reasons (spoiled, over-prep, dropped, returned), food cost % vs 28–35% target | new `app/costs.py`, `app/templates/costs.html` | open | — |
| 5 | Menu matrix (popularity × margin chart) and price alerts ("cream +8% → carbonara margin now 61%") | `app/charts.py`, `app/templates/menu.html` | open | — |
| 6 | AI: event booking → order. "40 extra covers tomorrow" → recipes × covers → shortfall → offers → order or approval | `app/intent.py`, `app/agent.py`, `app/menu.py` | open | — |
| 7 | AI: recipe-aware substitute on stock-out, allergens checked, trade-off explained (only suggests; ordering via policy) | `app/planner.py`, `app/menu.py` | open | — |
| 8 | AI: use-it-up specials from stock near its date (`use_first` in `app/kitchen.py`) | `app/menu.py`, `app/llm.py` | open | — |
| 9 | Drift alert: a standing order creeping above what sales explain is flagged before it renews | `app/kitchen.py`, `app/policy.py` (stricter only) | open | — |
| 10 | Pitch/demo update: `docs/DEMO.md`, `docs/CHALLENGE_CHECKLIST.md`, `README.md` | docs | open | after 1–6 |

Items 4–9 depend on 2 (recipes). Until it lands, build against the `Dish`/`RecipeLine` shape below.

## Data shape for item 2 (so others can start)
```
Dish:       id, org_key, name, category (starter/main/dessert/drink), price_eur, active, position
RecipeLine: id, dish_id, supply_id, quantity (in the supply's unit, per portion), yield_pct (default 100)
Sale:       id, org_key, day, minute, table, dish_id, quantity        # written by the live service
Waste:      id, supply_id, day, quantity, reason, by                  # item 4
```
Helpers in `app/menu.py`: `portion_cost(dish)`, `allergens(dish)`, `use_for_sales(session, org_key, day)`
(supply_id → quantity used).
