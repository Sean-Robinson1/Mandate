"""The demo buyers (hotel, school, restaurant), their people, and the mandate JSON Schema.

A mandate here is the organisation's *intent*. It becomes authority only once the organisation signs it with
its own key (app/keys.py issues and stores the signed copy; nothing re-signs it afterwards).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from protocol.mandate import Bands, HardConstraints, Mandate, SpendLimits, SubstitutionPolicy
from protocol.signing import canonical_bytes

_VALID_FROM = date(2026, 11, 1)
_VALID_UNTIL = date(2026, 12, 31)


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    org_key: str            # which buyer they belong to


PEOPLE: dict[str, Person] = {p.id: p for p in [
    Person("aino.koskinen", "Aino Koskinen", "Head chef", "hotel"),
    Person("mikko.laine", "Mikko Laine", "Purchasing manager", "hotel"),
    Person("sari.virtanen", "Sari Virtanen", "Kitchen manager", "school"),
    Person("elina.makinen", "Elina Mäkinen", "Head chef", "restaurant"),
    Person("jussi.heikkinen", "Jussi Heikkinen", "Owner", "restaurant"),
]}

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
        approver="mikko.laine",
        approver_name="Mikko Laine",
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
        approver="sari.virtanen",
        approver_name="Sari Virtanen",
    ),
    # A small independent bistro: the owner approves, so the agent's own limits are tight.
    "restaurant": Mandate(
        mandate_id="mandate-restaurant-001",
        buyer_org="Bistro Lumo Oy",
        agent_id="agent:bistro-lumo:purchasing-1",
        valid_from=_VALID_FROM,
        valid_until=_VALID_UNTIL,
        limits=SpendLimits(per_order_auto_eur=600, per_order_max_eur=3000, per_week_auto_eur=2500),
        substitution=SubstitutionPolicy(
            relaxable_preferences=["brand", "pack_size"], allow_split_orders=True
        ),
        approver="jussi.heikkinen",
        approver_name="Jussi Heikkinen",
    ),
}


def canonical_json(mandate: Mandate) -> bytes:
    """Deterministic bytes to sign: everything except the signature fields (same encoding as protocol.signing)."""
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
            "(Ed25519 over the canonical JSON without `signature` and `signer_key_id`). Valio Aimo's gateway "
            "verifies it against the organisation's registered key and enforces its hard limits."
        ),
        **schema,
    }
