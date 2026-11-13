"""Shared data contracts. Every component talks in these types.

Money is EUR before VAT unless a field says otherwise.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Unit(str, Enum):
    L = "L"
    KG = "KG"
    PCS = "PCS"


class Decision(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    REJECT = "REJECT"


# ---------------------------------------------------------------- intent

class IntentSpec(BaseModel):
    """What the buyer asked for, in structured form."""

    raw_text: str
    product_terms: list[str]                      # e.g. ["milk"]
    category: str | None = None                   # catalog category code prefix, e.g. "10400"
    exclude_terms: list[str] = Field(default_factory=list)
    quantity: float
    unit: Unit
    deadline: date                                # deliver on or before
    required_claims: list[str] = Field(default_factory=list)      # e.g. ["FREE_FROM_LACTOSE"]
    excluded_allergens: list[str] = Field(default_factory=list)   # GS1 codes, e.g. ["AN"]
    preferences: dict[str, Any] = Field(default_factory=dict)     # soft: brand, organic, pack_size...
    max_budget_eur: float | None = None
    confidence: float = 1.0                       # 0-1, parser's own certainty
    ambiguities: list[str] = Field(default_factory=list)          # things the parser had to guess
    parser: str = "rules"                         # "rules" | "llm:<provider>"


# ---------------------------------------------------------------- candidates and plan

class Candidate(BaseModel):
    """One product with live price and availability attached."""

    product_id: str
    name: str
    vendor: str
    brand: str | None = None
    gtin: str | None = None
    category: str
    sales_unit: str
    unit_price: float                             # per sales unit, net
    unit_price_with_tax: float
    measure_per_unit: float                       # litres or kg in one sales unit
    comparison_unit: str                          # "L" | "KG"
    comparison_price: float                       # net EUR per litre or kg
    available_units: float                        # sales units available by available_date
    quantity_confirmed: bool                      # False for Terminal products (supplier stock unknown)
    available_date: date | None
    closing_time: datetime | None                 # order cut-off
    sales_block: bool = False
    logistic_model: str | None = None
    claims: list[str] = Field(default_factory=list)
    contains: list[str] = Field(default_factory=list)
    may_contain: list[str] = Field(default_factory=list)
    free_from: list[str] = Field(default_factory=list)
    has_allergen_data: bool = True
    labels: list[str] = Field(default_factory=list)
    reliability: float | None = None              # 0-1 historical delivery reliability
    co2_per_kg: float | None = None
    description: str | None = None                # UNTRUSTED supplier text. Never used for decisions.


class Rejection(BaseModel):
    """A product the agent looked at and did not use, with the reason."""

    product_id: str
    name: str
    reason: str


class PlanLine(BaseModel):
    product_id: str
    name: str
    vendor: str
    category: str
    sales_unit: str
    sales_units: int                              # whole sales units ordered
    measure: float                                # litres or kg this line covers
    unit_price: float
    line_total: float
    comparison_price: float
    delivery_date: date
    closing_time: datetime | None = None
    quantity_confirmed: bool = True
    role: str = "primary"                         # "primary" | "split" | "substitute"
    relaxed_preferences: list[str] = Field(default_factory=list)   # soft prefs this line breaks
    claims: list[str] = Field(default_factory=list)
    contains: list[str] = Field(default_factory=list)
    may_contain: list[str] = Field(default_factory=list)
    has_allergen_data: bool = True
    reliability: float | None = None


class Plan(BaseModel):
    lines: list[PlanLine]
    unit: Unit
    requested_quantity: float
    covered_quantity: float
    shortfall: float = 0.0                        # requested - covered, if > 0
    total_net: float
    total_with_tax: float
    latest_delivery: date | None = None
    reference_price: float | None = None          # cheapest orderable EUR per L/kg that honours the stated preferences
    cheapest_overall_price: float | None = None   # cheapest orderable EUR per L/kg ignoring soft preferences
    rejected: list[Rejection] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- mandate

class SpendLimits(BaseModel):
    per_order_auto_eur: float                     # at or below: agent may order alone
    per_order_max_eur: float                      # above: refused outright
    per_week_auto_eur: float                      # weekly total at or below: agent may order alone


class Bands(BaseModel):
    """Green up to *_auto, amber up to *_max, red beyond."""

    price_premium_auto_pct: float = 5.0           # plan avg price vs reference_price
    price_premium_max_pct: float = 20.0
    overage_auto_pct: float = 3.0                 # covered above requested (pack rounding)
    overage_max_pct: float = 15.0
    shortfall_max_pct: float = 50.0               # any shortfall needs a human; beyond this it is refused
    late_days_max: int = 0                        # days after deadline a human may still approve
    min_intent_confidence: float = 0.8            # below: ask the human
    min_reliability_auto: float = 0.9             # known reliability below this needs a human


class HardConstraints(BaseModel):
    """Never relaxable by the agent or by an approver."""

    required_claims: list[str] = Field(default_factory=list)
    excluded_allergens: list[str] = Field(default_factory=list)    # CONTAINS is always a violation
    exclude_may_contain: bool = False             # MAY_CONTAIN is also a violation
    require_allergen_data: bool = False           # products without allergen data are refused


class SubstitutionPolicy(BaseModel):
    relaxable_preferences: list[str] = Field(default_factory=list)  # soft prefs the agent may relax alone
    allow_split_orders: bool = True


class Mandate(BaseModel):
    """What the buyer organisation authorises its agent to do. Signed by the organisation."""

    mandate_id: str
    buyer_org: str
    agent_id: str
    valid_from: date
    valid_until: date
    currency: str = "EUR"
    limits: SpendLimits
    bands: Bands = Field(default_factory=Bands)
    allowed_categories: list[str] | None = None   # category code prefixes; None = any
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    substitution: SubstitutionPolicy = Field(default_factory=SubstitutionPolicy)
    approver: str = "purchasing-manager"
    signature: str | None = None                  # base64 Ed25519 over canonical JSON without this field
    signer_key_id: str | None = None


# ---------------------------------------------------------------- policy

class PolicyContext(BaseModel):
    now: datetime
    week_spend_eur: float = 0.0                   # already committed this ISO week
    mandate_signature_valid: bool = True


class PolicyReason(BaseModel):
    rule: str                                     # stable id, e.g. "spend.per_order_auto"
    outcome: Decision                             # what this rule alone demands
    message: str                                  # plain language, shown to the approver
    details: dict[str, Any] = Field(default_factory=dict)


class PolicyResult(BaseModel):
    decision: Decision
    reasons: list[PolicyReason] = Field(default_factory=list)     # every rule that did not pass
    passed: list[str] = Field(default_factory=list)               # ids of rules that passed
