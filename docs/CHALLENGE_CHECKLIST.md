# Challenge checklist (Valio Aimo, Junction 2026)

Each line of the brief, what we built for it, and how to see it. Run the demo with
`.venv/Scripts/python -m scripts.dev` (console http://127.0.0.1:8000, Valio Aimo API docs http://127.0.0.1:8100/docs).
Tests: `.venv/Scripts/python -m pytest -q`. What is simulated is listed in `ASSUMPTIONS.md`.

## Core (required)

| Requirement from the brief | Status | Where in the code | Test that proves it | Demo step |
|---|---|---|---|---|
| **Product, price and availability lookup** in a structured form an outside agent can use | Done | `gateway/api.py` (`GET /v1/products`, `POST /v1/offers`), `gateway/catalog.py`, `gateway/backend.py`, `gateway/mcp_server.py` (same as MCP tools) | `test_gateway_catalog.py::test_terms_match_at_the_start_of_a_word`, `test_near_misses_are_reported_with_the_reason`, `test_gateway.py::test_search_reports_near_misses` | Open http://127.0.0.1:8100/docs and try the search; or ask the agent for milk and click the request to see the search and offer messages |
| **From inquiry to order at fixed prices**, not a negotiated quote | Done | `gateway/service.py` (signed offer valid 15 min, usable once; order checked against it), `protocol/wire.py`, `app/agent.py` | `test_gateway.py::test_offer_is_signed_and_published_key_verifies_it`, `test_an_offer_can_be_used_once`, `test_expired_offer_is_refused`, `test_agent.py::test_within_authority_orders_through_valio_aimo`, `test_changed_price_after_approval_orders_nothing` | Type "I need 1,000 litres of lactose-free milk by Monday" in the agent panel; watch offer, order and signed confirmation appear |
| **Permission and human approval**: what the agent decides alone and what goes to a person | Done | `app/policy.py` (buyer's rules engine), `protocol/mandate.py` (signed limits), `gateway/service.py` (Valio Aimo checks the mandate and the approval signature again), `app/rules.py` (changing the rules) | `test_policy.py` (per-order, per-week, price, delivery, allergen rules), `test_agent.py::test_over_the_order_limit_waits_for_a_person`, `test_approval_is_signed_and_checked_by_valio_aimo`, `test_gateway.py::test_over_the_agents_limit_without_approval_is_refused`, `test_absolute_cap_holds_even_with_approval` | Ask for an order above the limit as Aino; switch to Mikko in the sidebar and approve it; then change the purchasing rules and sign a new version |
| Example request: "1,000 litres of lactose-free milk by Monday" returns products, prices and availability, then orders or asks for approval | Done | `app/intent.py` (reading the request), `app/planner.py` (cheapest plan that meets requirements), `app/agent.py` | `test_planner.py::test_rules_parse_the_challenge_request`, `test_intent.py::test_headline_request_stays_confident` | Same request as above, from the console or `python -m app.cli` |

## Optional directions

| Requirement from the brief | Status | Where in the code | Test that proves it | Demo step |
|---|---|---|---|---|
| **Delivery time comparison** | Done | `app/planner.py` (late deliveries excluded and explained), `app/policy.py` (delivery and cut-off rules, supplier reliability) | `test_planner.py::test_late_delivery_is_excluded_and_explained`, `test_policy.py::test_delivery_boundaries`, `test_cutoff_boundaries`, `test_reliability_boundary` | Click a request and open the plan: products that arrive too late are listed with the reason |
| **Sustainability labels and certificates** | Partial | Labels and claims pass through search (`gateway/catalog.py`, `protocol/wire.py`); required claims enforced in `app/policy.py` and `gateway/service.py` | `test_policy.py::test_required_claim_from_mandate_missing_rejects_and_names_product` | Search results show labels; a mandate can require a claim. Labels are not used to rank or compare offers |
| Category-level carbon footprint (stretch) | Not done | - | - | - |
| **Substitution and exception handling** | Partial | `app/planner.py` (splits across products when one runs short, reports shortfall, brand preference and its cost), `protocol/mandate.py` (`SubstitutionPolicy`), `app/policy.py` (relaxed preference or split needs a person); expired offers renewed, changed prices stop the order (`app/agent.py`) | `test_planner.py::test_partial_stock_splits_across_products_in_price_order`, `test_shortfall_when_stock_runs_out`, `test_policy.py::test_unpermitted_relaxed_preference_needs_human_and_is_named`, `test_split_not_allowed_needs_human`, `test_agent.py::test_expired_offer_is_renewed_when_nothing_changed` | Ask for more than one product has in stock; the plan splits it and explains. No recipe-based substitutes |
| **Traceability and explainability**: a readable record of what the agent did and why | Done | `app/audit.py` (append-only, hash-chained log), `app/timeline.py` (step-by-step story), `app/templates/activity.html` | `test_audit.py` (edits, gaps and deletions detected), `test_policy.py::test_every_rule_is_reported_exactly_once` | Open Activity, click any request: plain-language steps; rule IDs, hashes and JSON one click deeper |

## Problems Valio Aimo would like tackled

| Problem | Status | Where in the code | Test that proves it | Demo step |
|---|---|---|---|---|
| **Trust protocol**: an agent can trust stock, price and origin claims without a manual check | Done (stock and price); origin only as product data | `protocol/signing.py` (Ed25519 over canonical JSON), `gateway/service.py` (signed offers and confirmations), `protocol/registry.py`, `gateway/keys.py` | `test_signing.py`, `test_agent.py::test_a_forged_offer_is_not_trusted`, `test_gateway.py::test_mandate_from_an_unregistered_organisation_is_refused`, `test_order_signed_by_someone_other_than_the_agent_is_refused` | Click a request, open a message from Valio Aimo: signature checked, details one click deeper |
| **Guardrails model**: spend thresholds and substitutions outside preferences need sign-off | Done | `app/policy.py`, `protocol/mandate.py`, `gateway/service.py`, `app/rules.py` | `test_policy.py::test_per_order_boundaries`, `test_per_week_boundary`, `test_intent_text_cannot_raise_limits`, `test_agent.py::test_concurrent_requests_cannot_overspend_the_week`, `test_rules.py::test_valio_aimo_refuses_an_older_version_once_it_has_seen_a_newer_one` | As Mikko, open the purchasing rules, review the difference and sign; the old version is refused from then on |
| **Response logic**: partial availability and good-better-best ranking | Partial | `app/planner.py` (cheapest first, split on partial stock, brand preference) | `test_planner.py::test_partial_stock_splits_across_products_in_price_order`, `test_brand_preference_is_honoured_first_and_its_cost_is_reported` | Plan view for a request. Ranking is by price and preference only, no tiered good-better-best |
| **Audit trail** for both buyer and Valio Aimo | Partial | Buyer: `app/audit.py`, `app/timeline.py`. Valio Aimo: offers and orders kept in its own store (`gateway/store.py`) | `test_audit.py::test_chain_of_events_verifies`, `test_deleting_the_last_rows_is_detected` | Buyer's Activity page. Valio Aimo has no audit screen of its own |
| Agent works without exposing real systems or confidential data | Done | Buyer talks to the gateway over HTTP only (`app/gateway_client.py`); the gateway alone calls the backend (`gateway/backend.py`) | `test_planner.py::test_supplier_text_cannot_influence_the_plan` | Split-screen console shows exactly what crosses between the two sides |

## Example scenarios

| Scenario | Status | Notes |
|---|---|---|
| Supplier stock-out / low stock triggers the agent | Partial | `app/kitchen.py`: press **Next day**; items below their level are restocked or sent to the manager (`test_kitchen.py::test_low_auto_row_is_restocked_up_to_its_level_without_asking`, `test_low_ask_first_row_waits_for_the_manager`). No recipe-compatible substitute |
| Allergen-safe substitution (school / hospital) | Partial | School mandate forbids nuts, enforced by both sides (`test_policy.py::test_school_mandate_refuses_may_contain_nuts_and_hotel_does_not`, `test_gateway.py::test_allergen_rules_are_enforced_by_valio_aimo_too`). Switch to Sari to see it. No nutrition rules |
| Standing order drift detection | Partial | Forecast and four weeks of history per item (`app/kitchen.py`, item page); limit suggestions from repeated approvals (`test_rules.py::test_repeated_approvals_suggest_a_higher_limit`). No anomaly flag on cost or quantity creep |
| Need → recipe → order; last-minute event | Not done | No recipe model |
| Proactive selling agent (Valio Aimo suggests products) | Not done | - |

## Summary

19 lines checked: 9 done, 7 partial, 3 not done. All four core lines are done.

## Not done / honest gaps

- **No recipes.** Substitutes are other products of the same type, not recipe-compatible alternatives; the recipe
  and last-minute-event scenarios are not covered.
- **No good-better-best ranking.** The plan picks the cheapest product that meets the rules, with brand preference.
- **Sustainability labels are shown and can be required, but not compared or scored.** No carbon footprint.
- **Valio Aimo has no audit screen.** It keeps offers and orders, but the readable log is the buyer's.
- **No proactive selling or drift alerts.**
- **Demo shortcuts** (see `ASSUMPTIONS.md`): all keys on one machine, signing in is picking a name, onboarding is a
  shared folder, kitchen days are simulated, the gateway keeps its own orders because the mock API has no order
  endpoint, and the audit log is tamper-evident, not tamper-proof.
- Not checked for this document: the demo steps were not re-run end to end.
