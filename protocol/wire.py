"""Wire formats of the Valio Aimo agent gateway (v1).

Flow: search the catalog -> request an offer (signed by Valio Aimo, fixed prices, short expiry)
-> place an order against that offer (signed by the buyer's agent, carrying its signed mandate and,
when the mandate requires one, a signed human approval) -> signed order confirmation.

Signed documents are plain JSON objects with `signer_key_id` and `signature` added; the signature covers
every other field (see protocol.signing). Verify the JSON as received, then parse it.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

API_VERSION = "v1"
OFFER_TTL_MINUTES = 15


# ---------------------------------------------------------------- catalog

class Product(BaseModel):
    """Static product facts from Valio Aimo's catalog."""

    product_id: str
    name: str
    brand: str | None = None
    vendor: str
    category: str
    sales_unit: str
    gtin: str | None = None
    claims: list[str] = Field(default_factory=list)        # e.g. FREE_FROM_LACTOSE
    contains: list[str] = Field(default_factory=list)      # GS1 allergen codes
    may_contain: list[str] = Field(default_factory=list)
    has_allergen_data: bool = True
    labels: list[str] = Field(default_factory=list)        # e.g. EU_ORGANIC_FARMING
    reliability: float | None = None                       # historical delivery reliability, 0-1
    co2_per_kg: float | None = None                        # product-group average, kg CO2e per kg


class ProductDetail(Product):
    """One product's full catalog page: the facts an agent searches on, plus what a person reads."""

    description: str | None = None
    ingredients: str | None = None
    substitutes: list[str] = Field(default_factory=list)  # product ids Valio Aimo lists as substitutes


class NotMatching(BaseModel):
    product_id: str
    name: str
    reason: str


class SearchResult(BaseModel):
    products: list[Product]
    not_matching: list[NotMatching] = Field(default_factory=list)   # near misses, with the reason
    not_returned: int = 0                                            # matches beyond the page limit


# ---------------------------------------------------------------- offer

class OfferRequest(BaseModel):
    products: list[str] = Field(min_length=1, max_length=200)
    delivery_by: date
    buyer_org: str | None = None


class OfferItem(BaseModel):
    product_id: str
    name: str
    sales_unit: str
    unit_price: float                      # EUR per sales unit, before VAT
    unit_price_with_tax: float
    comparison_unit: str | None = None     # "L" | "KG"
    comparison_price: float | None = None  # EUR per comparison unit, before VAT
    available_units: float                 # sales units deliverable on available_date
    available_date: date | None = None
    closing_time: datetime | None = None   # order cut-off for available_date
    logistic_model: str | None = None      # "Stock" (quantity known) | "Terminal" (supplier-delivered)
    quantity_confirmed: bool = False
    sales_block: bool = False


class Offer(BaseModel):
    """Fixed prices and availability, valid until expires_at. Signed by Valio Aimo."""

    offer_id: str
    issuer: str = "Valio Aimo"
    issued_at: datetime
    expires_at: datetime
    delivery_by: date
    buyer_org: str | None = None
    items: list[OfferItem]
    unavailable: list[NotMatching] = Field(default_factory=list)
    signer_key_id: str | None = None
    signature: str | None = None


# ---------------------------------------------------------------- order

class OrderLine(BaseModel):
    product_id: str
    sales_units: int = Field(gt=0)


class Approval(BaseModel):
    """A person's sign-off on exact order lines. Signed with the approver key the mandate names."""

    trace_id: str                          # the buyer's request it belongs to
    mandate_id: str
    approver: str
    approver_name: str = ""
    decision: str                          # APPROVED | DECLINED
    decided_at: datetime
    lines: list[OrderLine]                 # exactly what may be ordered
    total_net: float                       # at most this, before VAT
    rules_signed_off: list[str] = Field(default_factory=list)   # the buyer policy reasons the approver saw
    note: str = ""
    signer_key_id: str | None = None
    signature: str | None = None


class OrderRequest(BaseModel):
    """Signed by the buyer's agent with the key its mandate names."""

    offer_id: str
    mandate_id: str
    lines: list[OrderLine] = Field(min_length=1)
    buyer_reference: str | None = None     # the buyer's own trace id
    approval: dict | None = None           # signed approval record, when the mandate requires one
    signer_key_id: str | None = None
    signature: str | None = None


class PlaceOrder(BaseModel):
    """Both documents are verified exactly as received, then parsed (OrderRequest, Mandate)."""

    order: dict                            # a signed OrderRequest
    mandate: dict                          # the buyer organisation's signed Mandate


class Check(BaseModel):
    """One thing the gateway verified before accepting an order."""

    check: str                             # stable id, e.g. "mandate.signature"
    ok: bool
    message: str


class ConfirmedLine(BaseModel):
    product_id: str
    name: str
    sales_units: int
    sales_unit: str
    unit_price: float
    line_total: float
    delivery_date: date


class OrderConfirmation(BaseModel):
    """Valio Aimo's commitment to deliver. Signed by Valio Aimo."""

    order_id: str
    offer_id: str
    status: str                            # CONFIRMED | DELIVERED | CANCELLED
    buyer_org: str
    mandate_id: str
    buyer_reference: str | None = None
    lines: list[ConfirmedLine]
    total_net: float
    total_with_tax: float
    delivery_date: date
    confirmed_at: datetime
    approved_by: str | None = None
    checks: list[Check]
    signer_key_id: str | None = None
    signature: str | None = None


class OrderRefused(BaseModel):
    error: str
    checks: list[Check] = Field(default_factory=list)
