"""Deterministic policy engine: the code that enforces the mandate.

Pure function of (intent, plan, mandate, ctx). No I/O, no clock reads, no LLM,
no network. Time comes in through ``ctx.now``.

CRITICAL INVARIANT: every limit, band and hard constraint is read from the
Mandate and nowhere else. Nothing in the plan, the intent text or any product
text can raise a limit or relax a constraint. The intent may only make the
check *stricter* (extra required claims, extra excluded allergens, a lower
budget). Free text (intent.raw_text, line names, ambiguities) is only ever
echoed into messages, never interpreted. PlanLine has no description field
for that reason.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Callable

from app.models import (
    Decision,
    IntentSpec,
    Mandate,
    Plan,
    PlanLine,
    PolicyContext,
    PolicyReason,
    PolicyResult,
)

REJECT = Decision.REJECT
NEEDS_HUMAN = Decision.NEEDS_HUMAN

_SEVERITY = {Decision.AUTO_APPROVE: 0, Decision.NEEDS_HUMAN: 1, Decision.REJECT: 2}

# A rule returns a PolicyReason when it does not pass, otherwise None.
Rule = Callable[[IntentSpec, Plan, Mandate, PolicyContext], "PolicyReason | None"]
# A warning returns True when it should be flagged. It never changes the decision.
Warning = Callable[[IntentSpec, Plan, Mandate, PolicyContext], bool]


# ---------------------------------------------------------------- helpers

def _reason(rule: str, outcome: Decision, message: str, **details) -> PolicyReason:
    return PolicyReason(rule=rule, outcome=outcome, message=message, details=details)


def _eur(amount: float) -> str:
    return f"{amount:,.2f} EUR"


def _pct(value: float) -> float:
    # Rounding removes float noise so "exactly at the limit" is stable.
    return round(value, 6)


def _label(line: PlanLine) -> str:
    return f"{line.name} ({line.product_id})"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _is_past(closing: datetime, now: datetime) -> bool:
    # If either side is naive, compare both as naive wall-clock times.
    if closing.tzinfo is None or now.tzinfo is None:
        closing, now = closing.replace(tzinfo=None), now.replace(tzinfo=None)
    return closing <= now


# ---------------------------------------------------------------- mandate

def mandate_signature(intent, plan, mandate, ctx):
    if ctx.mandate_signature_valid:
        return None
    return _reason(
        "mandate.signature", REJECT,
        f"The signature on mandate {mandate.mandate_id} could not be verified, "
        "so the agent has no authority to order.",
        mandate_id=mandate.mandate_id,
    )


def mandate_validity(intent, plan, mandate, ctx):
    today = ctx.now.date()
    if mandate.valid_from <= today <= mandate.valid_until:
        return None
    return _reason(
        "mandate.validity", REJECT,
        f"Mandate {mandate.mandate_id} is valid from {mandate.valid_from} to "
        f"{mandate.valid_until}; today is {today}.",
        today=today.isoformat(),
        valid_from=mandate.valid_from.isoformat(),
        valid_until=mandate.valid_until.isoformat(),
    )


# ---------------------------------------------------------------- plan shape

def plan_empty(intent, plan, mandate, ctx):
    if plan.lines:
        return None
    return _reason(
        "plan.empty", REJECT,
        "The plan has no order lines: nothing orderable meets the constraints.",
    )


# ---------------------------------------------------------------- hard constraints

def hard_required_claims(intent, plan, mandate, ctx):
    required = _dedupe(mandate.hard_constraints.required_claims + intent.required_claims)
    violations = [
        {"product_id": line.product_id, "name": line.name, "missing_claim": claim}
        for line in plan.lines
        for claim in required
        if claim not in line.claims
    ]
    if not violations:
        return None
    text = "; ".join(f"{v['name']} ({v['product_id']}) lacks {v['missing_claim']}" for v in violations)
    return _reason(
        "hard.required_claims", REJECT,
        f"Required product claims are missing: {text}.",
        required_claims=required, violations=violations,
    )


def hard_allergens(intent, plan, mandate, ctx):
    excluded = _dedupe(mandate.hard_constraints.excluded_allergens + intent.excluded_allergens)
    violations = []
    for line in plan.lines:
        for allergen in excluded:
            if allergen in line.contains:
                violations.append({"product_id": line.product_id, "name": line.name,
                                   "allergen": allergen, "level": "contains"})
            elif mandate.hard_constraints.exclude_may_contain and allergen in line.may_contain:
                violations.append({"product_id": line.product_id, "name": line.name,
                                   "allergen": allergen, "level": "may_contain"})
    if not violations:
        return None
    words = {"contains": "contains", "may_contain": "may contain"}
    text = "; ".join(
        f"{v['name']} ({v['product_id']}) {words[v['level']]} {v['allergen']}" for v in violations
    )
    return _reason(
        "hard.allergens", REJECT,
        f"Excluded allergens found: {text}.",
        excluded_allergens=excluded, violations=violations,
    )


def hard_allergen_data(intent, plan, mandate, ctx):
    if not mandate.hard_constraints.require_allergen_data:
        return None
    missing = [line for line in plan.lines if not line.has_allergen_data]
    if not missing:
        return None
    return _reason(
        "hard.allergen_data", REJECT,
        "No allergen data for: " + ", ".join(_label(l) for l in missing)
        + ". Missing safety data is treated as unsafe.",
        product_ids=[l.product_id for l in missing],
    )


def category_allowed(intent, plan, mandate, ctx):
    allowed = mandate.allowed_categories
    if allowed is None:
        return None
    outside = [l for l in plan.lines if not any(l.category.startswith(p) for p in allowed)]
    if not outside:
        return None
    text = ", ".join(f"{_label(l)} in category {l.category}" for l in outside)
    return _reason(
        "category.allowed", REJECT,
        f"Products outside the categories this mandate allows ({', '.join(allowed) or 'none'}): {text}.",
        allowed_categories=list(allowed),
        violations=[{"product_id": l.product_id, "category": l.category} for l in outside],
    )


# ---------------------------------------------------------------- spend

def spend_per_order(intent, plan, mandate, ctx):
    total, limits = plan.total_net, mandate.limits
    details = dict(total_net=total, per_order_auto_eur=limits.per_order_auto_eur,
                   per_order_max_eur=limits.per_order_max_eur)
    # Fail closed: NaN compares False against every limit and would slip through.
    if not math.isfinite(total) or total < 0:
        return _reason("spend.per_order", REJECT,
                       f"The order total ({total}) is not a valid amount.", **details)
    if total > limits.per_order_max_eur:
        return _reason(
            "spend.per_order", REJECT,
            f"Order total {_eur(total)} is above the absolute per-order maximum of "
            f"{_eur(limits.per_order_max_eur)}.", **details)
    if total > limits.per_order_auto_eur:
        return _reason(
            "spend.per_order", NEEDS_HUMAN,
            f"Order total {_eur(total)} is above the {_eur(limits.per_order_auto_eur)} "
            "the agent may order alone.", **details)
    return None


def spend_per_week(intent, plan, mandate, ctx):
    limit = mandate.limits.per_week_auto_eur
    week_total = ctx.week_spend_eur + plan.total_net
    if week_total <= limit:
        return None
    return _reason(
        "spend.per_week", NEEDS_HUMAN,
        f"This order ({_eur(plan.total_net)}) on top of {_eur(ctx.week_spend_eur)} already "
        f"committed this week makes {_eur(week_total)}, above the weekly limit of {_eur(limit)} "
        "the agent may spend alone.",
        week_spend_eur=ctx.week_spend_eur, total_net=plan.total_net,
        week_total_eur=week_total, per_week_auto_eur=limit,
    )


def spend_intent_budget(intent, plan, mandate, ctx):
    budget = intent.max_budget_eur
    if budget is None or plan.total_net <= budget:
        return None
    return _reason(
        "spend.intent_budget", NEEDS_HUMAN,
        f"Order total {_eur(plan.total_net)} is above the budget of {_eur(budget)} "
        "given in the request.",
        total_net=plan.total_net, max_budget_eur=budget,
    )


# ---------------------------------------------------------------- bands

def band_price_premium(intent, plan, mandate, ctx):
    if not plan.reference_price or plan.reference_price <= 0 or plan.covered_quantity <= 0:
        return None
    bands = mandate.bands
    avg = plan.total_net / plan.covered_quantity
    premium = _pct((avg / plan.reference_price - 1) * 100)
    if premium <= bands.price_premium_auto_pct:
        return None
    unit = plan.unit.value
    base = (f"Average price {avg:.2f} EUR/{unit} is {premium:.1f}% above the cheapest "
            f"eligible price of {plan.reference_price:.2f} EUR/{unit}")
    details = dict(avg_price=round(avg, 6), reference_price=plan.reference_price,
                   premium_pct=premium, auto_pct=bands.price_premium_auto_pct,
                   max_pct=bands.price_premium_max_pct)
    if premium > bands.price_premium_max_pct:
        return _reason("band.price_premium", REJECT,
                       f"{base}; the maximum allowed premium is {bands.price_premium_max_pct:g}%.",
                       **details)
    return _reason("band.price_premium", NEEDS_HUMAN,
                   f"{base}; the agent may accept up to {bands.price_premium_auto_pct:g}% alone.",
                   **details)


def preference_cost(intent, plan, mandate, ctx):
    """Honouring a soft preference is the buyer's wish, but if it is expensive a human should confirm it."""
    cheapest = plan.cheapest_overall_price
    if not cheapest or cheapest <= 0 or plan.covered_quantity <= 0 or not intent.preferences:
        return None
    avg = plan.total_net / plan.covered_quantity
    premium = _pct((avg / cheapest - 1) * 100)
    if premium <= mandate.bands.price_premium_auto_pct:
        return None
    unit = plan.unit.value
    extra = plan.total_net - cheapest * plan.covered_quantity
    wanted = ", ".join(sorted(intent.preferences))
    return _reason("preference.cost", NEEDS_HUMAN,
                   f"Keeping the stated preference ({wanted}) costs {avg:.2f} EUR/{unit}, {premium:.0f}% more than "
                   f"the cheapest option that meets the requirements ({cheapest:.2f} EUR/{unit}). That is about "
                   f"{_eur(extra)} extra; the agent may accept up to {mandate.bands.price_premium_auto_pct:g}% alone.",
                   avg_price=round(avg, 6), cheapest_overall_price=cheapest, premium_pct=premium,
                   extra_eur=round(extra, 2))


def band_overage(intent, plan, mandate, ctx):
    requested, covered = plan.requested_quantity, plan.covered_quantity
    if covered <= requested:
        return None
    unit = plan.unit.value
    if requested <= 0:
        # No meaningful percentage; fail safe and ask a person.
        return _reason("band.overage", NEEDS_HUMAN,
                       f"The plan orders {covered:g} {unit} but the requested quantity is {requested:g}.",
                       requested_quantity=requested, covered_quantity=covered)
    bands = mandate.bands
    overage = _pct((covered - requested) / requested * 100)
    if overage <= bands.overage_auto_pct:
        return None
    base = (f"The plan orders {covered:g} {unit} for a request of {requested:g} {unit} "
            f"({overage:.1f}% over)")
    details = dict(requested_quantity=requested, covered_quantity=covered, overage_pct=overage,
                   auto_pct=bands.overage_auto_pct, max_pct=bands.overage_max_pct)
    if overage > bands.overage_max_pct:
        return _reason("band.overage", REJECT,
                       f"{base}; the maximum allowed overage is {bands.overage_max_pct:g}%.", **details)
    return _reason("band.overage", NEEDS_HUMAN,
                   f"{base}; the agent may accept up to {bands.overage_auto_pct:g}% alone.", **details)


def band_shortfall(intent, plan, mandate, ctx):
    requested = plan.requested_quantity
    # Trust the larger of the stated and the derived shortfall.
    shortfall = max(plan.shortfall, requested - plan.covered_quantity)
    if shortfall <= 0:
        return None
    unit = plan.unit.value
    max_pct = mandate.bands.shortfall_max_pct
    shortfall_pct = _pct(shortfall / requested * 100) if requested > 0 else 100.0
    base = (f"The plan is {shortfall:g} {unit} short of the {requested:g} {unit} requested "
            f"({shortfall_pct:.1f}% short)")
    details = dict(requested_quantity=requested, covered_quantity=plan.covered_quantity,
                   shortfall=shortfall, shortfall_pct=shortfall_pct, max_pct=max_pct)
    if shortfall_pct > max_pct:
        return _reason("band.shortfall", REJECT,
                       f"{base}; more than {max_pct:g}% short is refused.", **details)
    return _reason("band.shortfall", NEEDS_HUMAN,
                   f"{base}; any shortfall needs a person to decide.", **details)


def band_delivery(intent, plan, mandate, ctx):
    dates = [l.delivery_date for l in plan.lines]
    if plan.latest_delivery is not None:
        dates.append(plan.latest_delivery)
    if not dates:
        return None
    latest = max(dates)
    late_days = (latest - intent.deadline).days
    if late_days <= 0:
        return None
    max_days = mandate.bands.late_days_max
    base = f"Latest delivery is {latest}, {late_days} day(s) after the deadline of {intent.deadline}"
    details = dict(latest_delivery=latest.isoformat(), deadline=intent.deadline.isoformat(),
                   late_days=late_days, late_days_max=max_days)
    if late_days > max_days:
        return _reason("band.delivery", REJECT,
                       f"{base}; at most {max_days} day(s) late may be approved.", **details)
    return _reason("band.delivery", NEEDS_HUMAN,
                   f"{base}; late delivery needs a person to decide.", **details)


def order_cutoff(intent, plan, mandate, ctx):
    passed = [l for l in plan.lines
              if l.closing_time is not None and _is_past(l.closing_time, ctx.now)]
    if not passed:
        return None
    text = ", ".join(f"{_label(l)} closed at {l.closing_time.isoformat()}" for l in passed)
    return _reason(
        "order.cutoff", REJECT,
        f"The order cut-off has passed: {text} (now {ctx.now.isoformat()}).",
        now=ctx.now.isoformat(),
        violations=[{"product_id": l.product_id, "closing_time": l.closing_time.isoformat()}
                    for l in passed],
    )


# ---------------------------------------------------------------- substitution

def substitution_relaxed_preferences(intent, plan, mandate, ctx):
    permitted = mandate.substitution.relaxable_preferences
    violations = [
        {"product_id": line.product_id, "name": line.name, "preference": pref}
        for line in plan.lines
        for pref in line.relaxed_preferences
        if pref not in permitted
    ]
    if not violations:
        return None
    text = "; ".join(f"{v['name']} ({v['product_id']}) does not meet the '{v['preference']}' preference"
                     for v in violations)
    return _reason(
        "substitution.relaxed_preferences", NEEDS_HUMAN,
        f"{text}. The agent may only relax: {', '.join(permitted) or 'nothing'}.",
        relaxable_preferences=list(permitted), violations=violations,
    )


def substitution_split(intent, plan, mandate, ctx):
    if len(plan.lines) <= 1 or mandate.substitution.allow_split_orders:
        return None
    return _reason(
        "substitution.split", NEEDS_HUMAN,
        f"The order is split across {len(plan.lines)} products and this mandate does not "
        "let the agent split orders alone.",
        line_count=len(plan.lines),
    )


# ---------------------------------------------------------------- uncertainty

def uncertainty_intent_confidence(intent, plan, mandate, ctx):
    minimum = mandate.bands.min_intent_confidence
    if intent.confidence >= minimum:  # NaN fails this test and escalates
        return None
    guesses = "; ".join(intent.ambiguities) if intent.ambiguities else "none recorded"
    return _reason(
        "uncertainty.intent_confidence", NEEDS_HUMAN,
        f"The request was understood with confidence {intent.confidence:.2f}, below the required "
        f"{minimum:.2f}. Points the parser had to guess: {guesses}.",
        confidence=intent.confidence, min_intent_confidence=minimum,
        ambiguities=list(intent.ambiguities),
    )


def uncertainty_reliability(intent, plan, mandate, ctx):
    minimum = mandate.bands.min_reliability_auto
    low = [l for l in plan.lines if l.reliability is not None and not l.reliability >= minimum]
    if not low:
        return None
    text = ", ".join(f"{_label(l)} at {l.reliability:.0%}" for l in low)
    return _reason(
        "uncertainty.reliability", NEEDS_HUMAN,
        f"Delivery reliability is below the required {minimum:.0%}: {text}.",
        min_reliability_auto=minimum,
        violations=[{"product_id": l.product_id, "reliability": l.reliability} for l in low],
    )


def review_required(intent, plan, mandate, ctx):
    """The buyer chose 'ask first' for what this request restocks. Can only make the decision stricter."""
    if not ctx.review_reason:
        return None
    return _reason("request.review_required", NEEDS_HUMAN, ctx.review_reason)


def unconfirmed_quantity(intent, plan, mandate, ctx) -> bool:
    return any(not l.quantity_confirmed for l in plan.lines)


# ---------------------------------------------------------------- registry

RULES: list[tuple[str, Rule]] = [
    ("mandate.signature", mandate_signature),
    ("mandate.validity", mandate_validity),
    ("plan.empty", plan_empty),
    ("hard.required_claims", hard_required_claims),
    ("hard.allergens", hard_allergens),
    ("hard.allergen_data", hard_allergen_data),
    ("category.allowed", category_allowed),
    ("spend.per_order", spend_per_order),
    ("spend.per_week", spend_per_week),
    ("spend.intent_budget", spend_intent_budget),
    ("band.price_premium", band_price_premium),
    ("preference.cost", preference_cost),
    ("band.overage", band_overage),
    ("band.shortfall", band_shortfall),
    ("band.delivery", band_delivery),
    ("order.cutoff", order_cutoff),
    ("substitution.relaxed_preferences", substitution_relaxed_preferences),
    ("substitution.split", substitution_split),
    ("uncertainty.intent_confidence", uncertainty_intent_confidence),
    ("uncertainty.reliability", uncertainty_reliability),
    ("request.review_required", review_required),
]

# Flagged in `passed` as "<id>:warning"; never changes the decision.
WARNINGS: list[tuple[str, Warning]] = [
    ("uncertainty.unconfirmed_quantity", unconfirmed_quantity),
]

RULE_IDS: list[str] = [rule_id for rule_id, _ in RULES] + [rule_id for rule_id, _ in WARNINGS]


def evaluate(intent: IntentSpec, plan: Plan, mandate: Mandate, ctx: PolicyContext) -> PolicyResult:
    """Run every rule, every time. The decision is the most severe outcome."""
    reasons: list[PolicyReason] = []
    passed: list[str] = []

    for rule_id, rule in RULES:
        reason = rule(intent, plan, mandate, ctx)
        if reason is None:
            passed.append(rule_id)
        else:
            reasons.append(reason)

    for rule_id, warning in WARNINGS:
        flagged = warning(intent, plan, mandate, ctx)
        passed.append(f"{rule_id}:warning" if flagged else rule_id)

    decision = max((r.outcome for r in reasons), key=_SEVERITY.__getitem__,
                   default=Decision.AUTO_APPROVE)
    return PolicyResult(decision=decision, reasons=reasons, passed=passed)
