"""Shared data contracts. Every component talks in these types.

Money is EUR before VAT unless a field says otherwise.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from protocol.mandate import Bands, HardConstraints, Mandate, SpendLimits, SubstitutionPolicy  # noqa: F401  re-used here

__all__ = ["Unit", "Decision", "IntentSpec", "Candidate", "Rejection", "PlanLine", "Plan", "PolicyContext",
           "PolicyReason", "PolicyResult", "Mandate", "SpendLimits", "Bands", "HardConstraints", "SubstitutionPolicy"]


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
