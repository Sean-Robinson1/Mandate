# CLAUDE.md

Junction 2026 Valio Aimo challenge: how a wholesaler lets outside purchasing agents order safely.
Audience is hackathon judges: the UX and the trust story matter more than code polish.

## Current plan (14 Nov): read `docs/PLAN.md` first
Equal billing: a kitchen operations dashboard (menu, recipes, live sales, stock, waste, food cost) **and** the
trusted ordering agent through Valio Aimo. Adds a restaurant kitchen next to the hotel and school. Work items
and owners are in the table in `docs/PLAN.md`: claim one there and push before starting.

## Layout
- `gateway/`: Valio Aimo's side (REST on :8100, plus MCP). Talks to the challenge's mock API.
- `app/`: the buyer's agent and console (:8000). Talks to the gateway **over HTTP only**.
- `protocol/`: what both sides share (wire formats, mandate, signing). Neither side imports the other.
- `settings.py`: shared config. Secrets only in `.env` (git-ignored).

## Commands
- Run both: `.venv/Scripts/python -m scripts.dev`
- Tests: `.venv/Scripts/python -m pytest -q` (no network; `tests/conftest.py` builds a fake world)
- Fresh demo data (services stopped): `.venv/Scripts/python -m scripts.reset`

## Rules
- The LLM only reads requests. Every permission decision is deterministic code (`app/policy.py`, `gateway/service.py`).
- Policy changes may only make decisions stricter from request text; limits come from the signed mandate.
- Never hold a DB lock across a network or LLM call (see `app/agent.py` docstring).
- Changing `protocol/mandate.py` means regenerating `schemas/mandate.schema.json` (a test checks it).
- User-facing text: plain business language; rule IDs, hashes and JSON one click deeper.
- Record anything simulated or assumed in `ASSUMPTIONS.md`.
- Never commit `.env`, `keys/`, `*.db` or `PROBLEM_DESCRIPTION.md` (it contains the API key).
- Commit messages: one short line, written like a person would ("Fix crash on out-of-stock responses").
  No body, no Co-Authored-By or other trailers.

## Keep it small (from the ponytail skill)
- Read the code you touch and trace the real flow first. Lazy about the solution, never about reading.
- Before writing code, stop at the first that works: not needed at all → already in this repo → stdlib →
  native platform/browser feature → an installed dependency (see `requirements.txt`) → one line → minimal new code.
- No abstractions the task doesn't need. No new dependency without a reason.
- Never cut validation at trust boundaries (anything arriving at the gateway), signature checks, data-loss
  handling, security or accessibility.
- One small test for logic with a branch, money or security; none for trivial code.
- Mark deliberate shortcuts with `# shortcut: <when to revisit>`.
- End each piece of work by saying what was skipped or not checked.
