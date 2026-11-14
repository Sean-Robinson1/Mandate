"""The mandate: what a buyer organisation authorises its purchasing agent to do.

Signed by the organisation (Ed25519 over the canonical JSON without `signature` and `signer_key_id`).
Both sides read it: the buyer's policy engine applies every rule; Valio Aimo's gateway independently enforces the
hard limits (signature, validity, agent identity, spend caps, allergens and claims) before accepting an order.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


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
    unusual_qty_weeks: float = 3.0                # more than this many weeks of the kitchen's usual use needs a human


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
    """What the buyer organisation authorises its agent to do. Signed by the organisation.

    Mandates change over time: each change is a new, higher version, signed again. Valio Aimo always uses the
    highest version it has seen for a mandate id and refuses older ones, so a looser old version cannot be replayed.
    """

    mandate_id: str
    version: int = 1
    issued_at: datetime | None = None
    changed_by: str = ""                          # who authorised this version (person id)
    change_note: str = ""
    buyer_org: str
    agent_id: str
    agent_public_key: str | None = None           # base64url Ed25519; the agent signs every order with it
    valid_from: date
    valid_until: date
    currency: str = "EUR"
    limits: SpendLimits
    bands: Bands = Field(default_factory=Bands)
    allowed_categories: list[str] | None = None   # category code prefixes; None = any
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    substitution: SubstitutionPolicy = Field(default_factory=SubstitutionPolicy)
    approver: str = "purchasing-manager"          # approver id
    approver_name: str = ""                       # how people see the approver, e.g. "Mikko Laine"
    approver_public_key: str | None = None        # base64url Ed25519; approvals above the agent's limits use it
    signature: str | None = None                  # base64 Ed25519 over canonical JSON without this field
    signer_key_id: str | None = None
