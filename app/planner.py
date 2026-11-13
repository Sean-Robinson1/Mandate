"""Fulfilment planner: cover the requested quantity by the deadline at minimum cost.

Pure and deterministic: same candidates and clock in, same plan out.
Hard requirements were already filtered in catalog.search; this module handles time, stock and cost.
"""
from __future__ import annotations

import math
from datetime import datetime

from app.models import Candidate, IntentSpec, Plan, PlanLine, Rejection

MAX_REPORTED_ALTERNATIVES = 8


def _is_past(closing: datetime | None, now: datetime) -> bool:
    if closing is None:
        return False
    if (closing.tzinfo is None) != (now.tzinfo is None):
        closing, now = closing.replace(tzinfo=None), now.replace(tzinfo=None)
    return closing <= now


def ineligibility(c: Candidate, intent: IntentSpec, now: datetime) -> str | None:
    """Why this candidate cannot be ordered for this request, or None if it can."""
    if c.sales_block:
        return "blocked for sale by Valio Aimo"
    if c.available_date is None or c.available_units <= 0:
        return "out of stock, no delivery date available"
    if c.available_date > intent.deadline:
        return f"earliest delivery {c.available_date.isoformat()} is after the deadline {intent.deadline.isoformat()}"
    if _is_past(c.closing_time, now):
        return f"order cut-off passed at {c.closing_time:%a %d %b %H:%M}"
    if c.measure_per_unit <= 0:
        return "pack size unknown"
    return None


def relaxed_preferences(c: Candidate, intent: IntentSpec) -> list[str]:
    """Soft preferences this candidate does not satisfy."""
    relaxed = []
    brand = intent.preferences.get("brand")
    if brand and (c.brand or "").strip().lower() != str(brand).lower():
        relaxed.append("brand")
    if intent.preferences.get("organic") and "EU_ORGANIC_FARMING" not in c.labels:
        relaxed.append("organic")
    return relaxed


def _rank_key(c: Candidate, intent: IntentSpec):
    # Fewest relaxed preferences first, then cheapest per litre/kg, then most reliable, then id for determinism.
    return (len(relaxed_preferences(c, intent)), c.comparison_price, -(c.reliability or 0.0), c.product_id)


def build_plan(intent: IntentSpec, candidates: list[Candidate], now: datetime,
               already_rejected: list[Rejection] | None = None) -> Plan:
    rejected = list(already_rejected or [])
    eligible: list[Candidate] = []
    late_or_closed: list[tuple[Candidate, str]] = []
    for c in candidates:
        reason = ineligibility(c, intent, now)
        if reason:
            rejected.append(Rejection(product_id=c.product_id, name=c.name, reason=reason))
            late_or_closed.append((c, reason))
        else:
            eligible.append(c)
    eligible.sort(key=lambda c: _rank_key(c, intent))

    lines: list[PlanLine] = []
    remaining = intent.quantity
    for c in eligible:
        if remaining <= 1e-9:
            break
        wanted = math.ceil(remaining / c.measure_per_unit - 1e-9)
        take = min(wanted, math.floor(c.available_units))
        if take <= 0:
            continue
        relaxed = relaxed_preferences(c, intent)
        lines.append(PlanLine(
            product_id=c.product_id, name=c.name, vendor=c.vendor, category=c.category,
            sales_unit=c.sales_unit, sales_units=take, measure=round(take * c.measure_per_unit, 2),
            unit_price=c.unit_price, line_total=round(take * c.unit_price, 2),
            comparison_price=c.comparison_price, delivery_date=c.available_date,
            closing_time=c.closing_time, quantity_confirmed=c.quantity_confirmed,
            role="primary" if not lines else ("substitute" if relaxed else "split"),
            relaxed_preferences=relaxed, claims=c.claims, contains=c.contains, may_contain=c.may_contain,
            has_allergen_data=c.has_allergen_data, reliability=c.reliability,
        ))
        remaining -= take * c.measure_per_unit

    chosen = {l.product_id for l in lines}
    price_of = {c.product_id: c for c in candidates}
    covered = round(sum(l.measure for l in lines), 2)
    total_net = round(sum(l.line_total for l in lines), 2)
    total_gross = round(sum(l.sales_units * price_of[l.product_id].unit_price_with_tax for l in lines), 2)
    reference = min((c.comparison_price for c in eligible), default=None)
    unit = intent.unit.value

    notes: list[str] = []
    for i, line in enumerate(lines[:-1]):
        c = price_of[line.product_id]
        notes.append(f"{line.name} only had {math.floor(c.available_units)} {c.sales_unit} "
                     f"({line.measure:g} {unit}) available by {intent.deadline:%A}, "
                     f"so the rest comes from {lines[i + 1].name}.")
    if lines and reference is not None:
        cheaper = sorted(((c, why) for c, why in late_or_closed if c.comparison_price < reference),
                         key=lambda x: x[0].comparison_price)
        if cheaper:
            c, why = cheaper[0]
            notes.append(f"A cheaper option exists but cannot be used: {c.name} at "
                         f"{c.comparison_price:.2f} EUR/{unit} ({why}).")
    if any(not l.quantity_confirmed for l in lines):
        notes.append("Some lines are supplier-delivered: Valio Aimo does not confirm exact stock for them.")

    alternatives = [c for c in eligible if c.product_id not in chosen][:MAX_REPORTED_ALTERNATIVES]
    for c in alternatives:
        rejected.append(Rejection(
            product_id=c.product_id, name=c.name,
            reason=f"available, but not needed: {c.comparison_price:.2f} EUR/{unit}"
                   + (f" vs {reference:.2f} EUR/{unit} for the cheapest" if reference else "")))

    return Plan(
        lines=lines, unit=intent.unit, requested_quantity=intent.quantity, covered_quantity=covered,
        shortfall=round(max(0.0, intent.quantity - covered), 2), total_net=total_net,
        total_with_tax=total_gross, latest_delivery=max((l.delivery_date for l in lines), default=None),
        reference_price=reference, rejected=rejected, notes=notes,
    )
