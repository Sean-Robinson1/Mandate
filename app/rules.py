"""Changing a mandate: the editable fields, the difference a change makes, and suggestions from experience.

A change never takes effect by itself. The purchasing manager reviews the difference and signs the next version,
which is published to Valio Aimo; from then on Valio Aimo refuses orders under any older version.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

import settings as config
from app.db import Run
from protocol.mandate import Mandate

ALLERGENS = {"AN": "tree nuts", "AP": "peanuts", "AM": "milk", "AE": "egg", "AF": "fish", "AW": "gluten cereals",
             "AY": "soy", "AS": "sesame", "AC": "crustaceans"}


@dataclass(frozen=True)
class Field:
    path: str            # dotted path in the mandate, e.g. "limits.per_order_auto_eur"
    label: str
    kind: str            # eur | pct | share | weeks | allergens | bool | date
    hint: str = ""


FIELDS = [
    Field("limits.per_order_auto_eur", "The agent may order alone, per order", "eur"),
    Field("limits.per_week_auto_eur", "The agent may order alone, per week", "eur"),
    Field("limits.per_order_max_eur", "Never above, per order, even with approval", "eur"),
    Field("bands.price_premium_auto_pct", "Accept up to this much above the cheapest option", "pct"),
    Field("bands.min_reliability_auto", "Minimum delivery reliability to order alone", "share"),
    Field("bands.unusual_qty_weeks", "Ask a person when an order is more than this many weeks of usual use", "weeks"),
    Field("hard_constraints.excluded_allergens", "Never buy products containing", "allergens"),
    Field("hard_constraints.exclude_may_contain", "Also refuse products that may contain traces", "bool"),
    Field("substitution.allow_split_orders", "The agent may split an order across products", "bool"),
    Field("valid_until", "Valid until", "date"),
]


def _get(doc: dict, path: str):
    for part in path.split("."):
        doc = doc[part]
    return doc


def _set(doc: dict, path: str, value) -> None:
    *parents, last = path.split(".")
    for part in parents:
        doc = doc.setdefault(part, {})
    doc[last] = value


def show(field: Field, value) -> str:
    if field.kind == "eur":
        return f"€{value:,.0f}" if float(value).is_integer() else f"€{value:,.2f}"
    if field.kind == "pct":
        return f"{value:g}%"
    if field.kind == "share":
        return f"{value * 100:g}%"
    if field.kind == "weeks":
        return f"{value:g} weeks"
    if field.kind == "allergens":
        return ", ".join(ALLERGENS.get(a, a) for a in value) or "nothing"
    if field.kind == "bool":
        return "yes" if value else "no"
    return str(value)


def form_values(mandate: Mandate) -> dict[str, object]:
    """Current values as the form shows them (shares as percentages)."""
    doc = mandate.model_dump(mode="json")
    out = {}
    for f in FIELDS:
        value = _get(doc, f.path)
        out[f.path] = round(value * 100, 2) if f.kind == "share" else value
    return out


def parse(form: dict, current: Mandate) -> tuple[dict, list[str]]:
    """Form input -> (the complete proposed mandate terms, errors). Unknown keys are ignored."""
    proposed = current.model_dump(mode="json")
    errors = []
    for f in FIELDS:
        if f.kind == "allergens":
            _set(proposed, f.path, sorted(a for a in ALLERGENS if form.get(f"{f.path}.{a}")))
            continue
        if f.kind == "bool":
            _set(proposed, f.path, bool(form.get(f.path)))
            continue
        raw = str(form.get(f.path, "")).strip()
        if f.kind == "date":
            try:
                _set(proposed, f.path, date.fromisoformat(raw).isoformat())
            except ValueError:
                errors.append(f"{f.label}: enter a date")
            continue
        try:
            number = float(raw.replace(",", ".").replace(" ", "").replace("€", "").replace("%", ""))
            if not math.isfinite(number) or number < 0:
                raise ValueError
        except ValueError:
            errors.append(f"{f.label}: enter a number of zero or more")
            continue
        if f.kind == "share":
            if number > 100:
                errors.append(f"{f.label}: at most 100%")
                continue
            number = round(number / 100, 4)
        _set(proposed, f.path, number)
    limits = proposed["limits"]
    if not errors and limits["per_order_auto_eur"] > limits["per_order_max_eur"]:
        errors.append("The agent's own limit per order cannot be above the absolute maximum")
    if not errors and date.fromisoformat(proposed["valid_until"]) < date.fromisoformat(proposed["valid_from"]):
        errors.append("Valid until cannot be before the mandate starts")
    return proposed, errors


def diff(current: Mandate, proposed: dict) -> list[dict]:
    before = current.model_dump(mode="json")
    return [{"path": f.path, "label": f.label, "old": show(f, _get(before, f.path)), "new": show(f, _get(proposed, f.path)),
             "looser": _looser(f, _get(before, f.path), _get(proposed, f.path))}
            for f in FIELDS if _get(before, f.path) != _get(proposed, f.path)]


def _looser(f: Field, old, new) -> bool:
    """Does this change give the agent more freedom? Shown so the signer notices."""
    if f.kind in ("eur", "pct", "weeks"):
        return new > old
    if f.kind == "share":
        return new < old
    if f.kind == "allergens":
        return not set(old) <= set(new)
    if f.path == "hard_constraints.exclude_may_contain":
        return old and not new
    if f.path == "substitution.allow_split_orders":
        return new and not old
    return f.kind == "date" and new > old


def changes_only(current: Mandate, proposed: dict) -> dict:
    """The proposed terms as an update for keys.amend_mandate (top-level keys that changed)."""
    before = current.model_dump(mode="json")
    return {k: v for k, v in proposed.items() if before.get(k) != v}


# ---------------------------------------------------------------- earned trust

@dataclass
class Suggestion:
    path: str
    current: float
    proposed: float
    evidence: str


def suggestions(session: Session, mandate: Mandate, now: datetime | None = None, window_days: int = 30,
                min_cases: int = 3) -> list[Suggestion]:
    """If the approver keeps approving orders held back only by the per-order limit, suggest a limit that covers
    them. Any decline of such an order means no suggestion. A person still has to sign the change."""
    now = now or datetime.now(config.HELSINKI)
    since = (now - timedelta(days=window_days)).date().isoformat()
    runs = session.scalars(select(Run).where(Run.mandate_id == mandate.mandate_id, Run.created_at >= since,
                                             Run.status.in_(("APPROVED", "DECLINED")))).all()
    only_size = [r for r in runs if r.policy_json
                 and {x["rule"] for x in json.loads(r.policy_json).get("reasons", [])} == {"spend.per_order"}]
    approved = [r for r in only_size if r.status == "APPROVED" and r.total_net > mandate.limits.per_order_auto_eur]
    if len(approved) < min_cases or any(r.status == "DECLINED" for r in only_size):
        return []
    largest = max(r.total_net for r in approved)
    proposed = float(math.ceil(largest / 500) * 500)
    if proposed <= mandate.limits.per_order_auto_eur or proposed > mandate.limits.per_order_max_eur:
        return []
    who = mandate.approver_name or mandate.approver
    return [Suggestion(
        path="limits.per_order_auto_eur", current=mandate.limits.per_order_auto_eur, proposed=proposed,
        evidence=f"{who} approved all {len(approved)} orders that were held back only by the "
                 f"€{mandate.limits.per_order_auto_eur:,.0f} limit in the last {window_days} days "
                 f"(largest €{largest:,.2f}), and declined none.")]
