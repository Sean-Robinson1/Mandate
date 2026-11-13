"""Demo mandates, canonical serialisation for signing, and the mandate JSON Schema."""
from __future__ import annotations

from datetime import date

from app.models import Bands, HardConstraints, Mandate, SpendLimits, SubstitutionPolicy
from app.signing import canonical_bytes

_VALID_FROM = date(2026, 11, 1)
_VALID_UNTIL = date(2026, 12, 31)

DEMO_MANDATES: dict[str, Mandate] = {
    "hotel": Mandate(
        mandate_id="mandate-hotel-001",
        buyer_org="Nordic Harbour Hotels Oy",
        agent_id="agent:nordic-harbour:purchasing-1",
        valid_from=_VALID_FROM,
        valid_until=_VALID_UNTIL,
        limits=SpendLimits(per_order_auto_eur=2500, per_order_max_eur=10000, per_week_auto_eur=8000),
        allowed_categories=None,
        substitution=SubstitutionPolicy(
            relaxable_preferences=["brand", "pack_size"], allow_split_orders=True
        ),
    ),
    "school": Mandate(
        mandate_id="mandate-school-001",
        buyer_org="Kaarinan koulukeittiö (school kitchen)",
        agent_id="agent:kaarina-school:purchasing-1",
        valid_from=_VALID_FROM,
        valid_until=_VALID_UNTIL,
        limits=SpendLimits(per_order_auto_eur=800, per_order_max_eur=3000, per_week_auto_eur=2500),
        bands=Bands(min_reliability_auto=0.95),
        hard_constraints=HardConstraints(
            excluded_allergens=["AN", "AP"],  # GS1: tree nuts, peanuts
            exclude_may_contain=True,
            require_allergen_data=True,
        ),
        substitution=SubstitutionPolicy(relaxable_preferences=["brand"]),
    ),
}


def canonical_json(mandate: Mandate) -> bytes:
    """Deterministic bytes to sign: everything except the signature fields (same encoding as app.signing)."""
    return canonical_bytes(mandate.model_dump(mode="json", exclude={"signature", "signer_key_id"}))


def mandate_json_schema() -> dict:
    schema = Mandate.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/trusted-trade/mandate.schema.json",
        "title": "Trusted Trade purchasing mandate",
        "description": (
            "What a buyer organisation authorises its purchasing agent to do: spend limits, "
            "tolerance bands, hard constraints and substitution rules. Signed by the organisation "
            "(Ed25519 over the canonical JSON without `signature` and `signer_key_id`)."
        ),
        **schema,
    }
