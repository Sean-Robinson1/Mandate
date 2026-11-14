# Competitors: other teams on the Valio Aimo challenge

Public GitHub repositories from other Junction 2026 teams working on the same challenge, found by repository
search on 14 November 2026. All had 0 stars and were last updated 13–14 November. We read READMEs and skimmed
the code; we did not run any of them. Repos with unusual names may be missing (GitHub code search was not
available), so treat this as a snapshot, not a complete list.

Note: one team (ToukoUrsin) also uses "Nordic Harbour Hotels" as its buyer, and several use the name "Trusted
Trade". Both probably come from the challenge brief, so neither sets us apart.

## The list, strongest first

| # | Repo | Team / project | In one line |
|---|------|----------------|-------------|
| 1 | [ToukoUrsin/aimo-agent-gateway](https://github.com/ToukoUrsin/aimo-agent-gateway) | Northbound Dispatch | Deployed, measured, very polished gateway with passkey approvals and a proof page |
| 2 | [SaedAbukar/valioaimo](https://github.com/SaedAbukar/valioaimo) | Aimo Agent Gateway | Published rule tiers, a requirement-by-requirement checklist, kitchen memory |
| 3 | [ngwbap/steel-ball-agentgate](https://github.com/ngwbap/steel-ball-agentgate) | STEEL BALL | Sharp product story; good/better/best options; approver can edit amounts |
| 4 | [vgorovoy/trusted-trade-agent-valio](https://github.com/vgorovoy/trusted-trade-agent-valio) | – | Autonomy modes, ruled-out candidates with reasons, an agent benchmark |
| 5 | [aapolipponen/aimo-agent-gateway](https://github.com/aapolipponen/aimo-agent-gateway) | – | Solid Next.js gateway with JWT mandates and a delivery-reliability floor |
| 6 | [zimaBloo/trusted-trade-gateway](https://github.com/zimaBloo/trusted-trade-gateway) | – | Work in progress: FastAPI, 12 MCP tools, Peppol-style order statuses |
| 7 | [Viettranni/AimoValio-Junction2026](https://github.com/Viettranni/AimoValio-Junction2026) | Aimo Planner | Embeddable chat widget for event menus; a buyer assistant, not a trust gateway |

Weak or not usable: [antonTan96/junction26_aimo](https://github.com/antonTan96/junction26_aimo) (made-up ERP, but a
nice diff of raw vs cleaned public records), [Murtsi/Guru](https://github.com/Murtsi/Guru) (synthetic proof of
concept, mostly docs), [onurminen/junctionValio](https://github.com/onurminen/junctionValio) (minimal, commits the raw
challenge data), [charlesgobina/valio-aimo](https://github.com/charlesgobina/valio-aimo) (empty).

## Each team in more detail

### 1. ToukoUrsin / Northbound Dispatch
- Python gateway with MCP and REST, deployed on Fly, with a slide deck, a demo film (Remotion) and recorded test
  runs behind every number it shows.
- ES256 signatures over canonical JSON; the approver says yes on a second device with a passkey (WebAuthn).
- MCP Apps: each tool call answers with an order card drawn inside Claude.
- Gemini reads a menu photo; code turns it into an order.
- Valio Aimo's own side: a `/seller` agent spots what a kitchen missed (a drifted standing order, short-dated
  stock); a `/tower` control room splits short milk fairly across 60 kitchens.
- Proof page: 190 runs across 5 models with 0 rule breaks, 100 attack tests; Peppol UBL order checked against
  the official schema; ledger head anchored on the Solana test network; standalone `verify/` scripts.
- Data insight they use: 31% of food products have no allergen declaration, and 290 of those name an allergen
  in the title.
- Versus us: the same core design (signed offer and mandate, rules in code, hash chain), with many more surfaces
  and more measured evidence. The breadth may dilute their story.

### 2. SaedAbukar / Aimo Agent Gateway
- Offers signed as W3C Verifiable Credentials (`vc+jwt`).
- `GET /v1/rules` publishes four tiers: decide alone, decide and report, needs a named person, never allowed.
- YAML order playbook and discovery documents (`/.well-known/ucp`, an agent card).
- Named approver on every confirmation; 135 tests; `docs/CHALLENGE_CHECKLIST.md` maps each requirement to the
  test that proves it.
- Kitchen memory: learns use per guest and leftovers from past events.
- Their mock-API notes are the most useful finds: the API is behind Cloudflare and refuses Python's default user
  agent; "Terminal" products always report 999999 stock, so delivery reliability matters more for them; the
  `customer` field changes nothing; more than 1,000 products in one call returns 422.

### 3. ngwbap / STEEL BALL
- Pitch: the webshop's search for "lactose free milk" lists milk chocolate first. They check that "milk" stays
  in the milk category.
- Claude turns the request into a structured need; one price call and one availability call cover ~8 products.
- Good / better / best plans, scored 0.55 × delivery certainty + 0.45 × match quality, each showing where its
  numbers came from.
- "Always ask me" is the default. The approver can change amounts and the order is re-priced. Unusual
  quantities versus the buyer's normal week go to a person.
- The agent can declare its own risks (it can only add caution); each agent can be paused.
- Strict mode: missing allergen data counts as unverified, not safe.

### 4. vgorovoy
- Ed25519-signed mandate, offers, cart and receipt; SQLite search in Finnish, English and Swedish with GS1
  allergen codes.
- Autonomy modes (autonomous / confirm cart / advise) and an "Agent Passport" before acting alone.
- The planner lists every ruled-out candidate with its reason; agent benchmark (`tta bench`); buyer and Valio
  each see their own view of the audit log.

### 5. aapolipponen
- Next.js with ES256 JWT mandates and a public key list (JWKS); REST, OpenAPI, stdio MCP and HTTP MCP share one
  schema set.
- The mandate has a minimum delivery-reliability floor and a price tolerance; unknown reliability goes to a person.
- Every call needs a business reason from the agent. Honest about the limits of its hash chain.

### 6. zimaBloo (work in progress)
- FastAPI, 12 MCP tools, JWS quotes, a discovery document, order statuses modelled on Peppol order-response codes.
- Five scenarios on real data (cut-off passed, partial stock, allergen refused). Gotcha they note: a stale cache
  sends every order to a person.

### 7. Viettranni / Aimo Planner
- One-script-tag chat widget on a mock webshop; event menu to a priced draft order that learns from leftovers;
  Mastra agents on Claude; "fast-forward to the day after the event" check-in.
- A buyer assistant rather than a trust gateway; weak on mandates.

## What we already have that they highlight

Ruled-out products with reasons (`gateway/catalog.py` near misses, `app/planner.py`), a delivery-reliability
floor in the mandate (`min_reliability_auto`), an approver's note on every decision, a tamper-check script
(`scripts/verify_offer.py --tamper`), approvals signed with the approver's own key, versioned mandates that
Valio Aimo enforces, and a stock-first kitchen dashboard none of them have.

## Best ideas worth taking, ranked by judge impact for the effort

1. **Valio Aimo can pause an agent instantly** (ToukoUrsin `gateway/tower.py`, ngwbap `set_agent_paused`).
   Every call after that is refused with a plain reason.
2. **Unusual amount goes to a person** (ngwbap): an order far above the kitchen's normal week needs approval.
   We already have the usage history in `app/kitchen.py`.
3. **"Try to cheat" panel** (ToukoUrsin `agent/rogue.py`, ngwbap demo mode): one click runs a misbehaving agent
   against the live gateway (edited price, reused offer, old mandate, self-signed approval, over the cap); each
   attempt shows as refused, with the reason. We have these only as tests.
4. **Valio Aimo's own activity view** (ToukoUrsin `/seller`, `/tower`; vgorovoy): offers issued, orders accepted
   and refused, and why. Our gateway does not record refusals yet.
5. **Published rule tiers** (SaedAbukar `GET /v1/rules`): alone / needs a named person / never, served by the
   gateway and as an MCP tool.
6. Approver signs on their own phone (ToukoUrsin passkeys; about 400 lines, browser-tested).
7. Agent-declared concerns that can only add caution (ngwbap).
8. Approve with changes and live re-pricing (ngwbap). *We now have "send it back to the agent with changes".*
9. A proof page of counted results, each number linked to its record (ToukoUrsin).
10. Good / better / best options with a score (ngwbap).

## Short implementation plan for the top five

**1. Pause switch (small)**
- `gateway/store.py`: a new `PausedAgent` table (agent id, paused at, by, note); a new table, because
  `create_all` will not add columns to an existing one.
- `gateway/service.py`: check it first in `offer` and `place_order` and refuse with a plain reason.
- `gateway/api.py`: pause and unpause endpoints behind an operator token from `settings.py` / `.env`.
- One test in `tests/test_gateway.py`. Trust boundary: the token check must not be skipped.

**2. Unusual amount (small to medium)**
- `protocol/mandate.py`: `Bands.unusual_week_factor` (e.g. 2.0); regenerate `schemas/mandate.schema.json`.
- `app/models.py` + `app/agent.py`: pass the kitchen's usual weekly use from `app/kitchen.py`.
- `app/policy.py`: rule `quantity.unusual` → needs a person. Only makes decisions stricter, so it fits our rules.
- Boundary test in `tests/test_policy.py`; note in `ASSUMPTIONS.md`.

**3. "Try to cheat" panel (medium)**
- `app/web.py`: a route that runs about six attacks over HTTP through `app/gateway_client.py`, signing with the
  agent's key.
- `app/templates/attacks.html`: one plain line per attempt, raw gateway response one click deeper; link from
  `base.html`. Never fake a result: an attack that succeeds shows in red.

**4. Valio Aimo's activity view (medium)**
- `gateway/store.py`: a `DecisionRecord` table; `gateway/service.py` writes one on every refusal and acceptance
  (inside the existing session, never across the backend call).
- `gateway/api.py`: `GET /v1/activity` (operator token) and a simple page on :8100.

**5. Published rule tiers (small)**
- `protocol/`: a helper that turns a mandate into three plain-language lists, so neither side imports the other.
- `gateway/api.py` `GET /v1/mandates/{id}/rules`, a matching MCP tool, and the same wording on
  `app/templates/mandates.html`. One test.

## What not to copy

- **Menu photo → recipes → order, and event planning** (ToukoUrsin, Viettranni): huge and LLM-centric; it pulls
  the story away from "code enforces, the LLM only reads".
- **Stacking standards and infrastructure** (fair-share across 60 kitchens, Solana anchoring, Peppol UBL, OAuth,
  ES256/VC formats): days of work each, little gain in a demo. Our Ed25519 + signed mandate story is clear.
- **Benchmarking five LLMs**: needs keys and hours, and argues the wrong point. Our safety does not depend on the
  model; the "try to cheat" panel shows that more cheaply.
