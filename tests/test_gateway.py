"""Valio Aimo's gateway on its own: it must hold the line even when the buyer's agent is wrong or compromised."""
from datetime import date, timedelta

import pytest

from app import keys
from gateway import catalog
from protocol.signing import Signer, sign_document, verify_document
from tests.conftest import INDEX, NOW, product

MONDAY = date(2026, 11, 16)


def offer_for(world, ids=("100001",)) -> dict:
    status, body = world.client._call("POST", "/v1/offers", json={"products": list(ids),
                                                                  "delivery_by": MONDAY.isoformat()})
    assert status == 201
    return body


def place(world, offer: dict, units: int, buyer: str = "hotel", *, approval: dict | None = None,
          mandate: dict | None = None, signer=None, product_id: str = "100001", reference: str = "test"):
    m = world.mandates[buyer]
    order = {"offer_id": offer["offer_id"], "mandate_id": m.mandate_id, "buyer_reference": reference,
             "lines": [{"product_id": product_id, "sales_units": units}], "approval": approval}
    signed = sign_document(order, signer) if signer else keys.sign_order(order, m)
    return world.client._call("POST", "/v1/orders", json={"order": signed,
                                                         "mandate": mandate or m.model_dump(mode="json")})


def approval_for(world, units: int, total: float, signer=None, product_id: str = "100001") -> dict:
    m = world.mandates["hotel"]
    record = {"trace_id": "t", "mandate_id": m.mandate_id, "approver": m.approver, "approver_name": m.approver_name,
              "decision": "APPROVED", "decided_at": NOW.isoformat(),
              "lines": [{"product_id": product_id, "sales_units": units}], "total_net": total}
    return sign_document(record, signer) if signer else keys.sign_approval(record, m)


def failed(body) -> list[str]:
    return [c["check"] for c in body["checks"] if not c["ok"]]


def test_offer_is_signed_and_published_key_verifies_it(world):
    offer = offer_for(world)
    key = world.client.public_key()
    assert verify_document(offer, key)
    assert offer["expires_at"] > offer["issued_at"]
    offer["items"][0]["unit_price"] = 0.01
    assert not verify_document(offer, key)


def test_order_within_limits_is_confirmed_and_signed(world):
    status, body = place(world, offer_for(world), 1000)
    assert status == 201 and body["status"] == "CONFIRMED" and body["total_net"] == 1000
    assert verify_document(body, world.client.public_key())
    assert failed(body) == []


def test_an_offer_can_be_used_once(world):
    offer = offer_for(world)
    assert place(world, offer, 100)[0] == 201
    status, body = place(world, offer, 100, reference="another order")
    assert status == 409 and "offer.unused" in failed(body)
    status, body = place(world, offer, 90)                                  # same reference, different lines
    assert status == 409 and "offer.unused" in failed(body)


def test_a_resent_order_gets_the_same_confirmation_not_a_second_order(world):
    offer = offer_for(world)
    first, again = place(world, offer, 100), place(world, offer, 100)       # the first answer was lost
    assert first[0] == again[0] == 201 and first[1]["order_id"] == again[1]["order_id"]


def test_mandate_changed_after_signing_is_refused(world):
    forged = world.mandates["hotel"].model_dump(mode="json")
    forged["limits"]["per_order_auto_eur"] = 1_000_000
    status, body = place(world, offer_for(world), 5000, mandate=forged)
    assert status == 403 and failed(body) == ["mandate.signature"]


def test_mandate_from_an_unregistered_organisation_is_refused(world):
    rogue = Signer.generate("org:rogue")
    m = world.mandates["hotel"].model_dump(mode="json", exclude={"signature", "signer_key_id"})
    status, body = place(world, offer_for(world), 100, mandate=sign_document(m, rogue))
    assert status == 403 and failed(body) == ["mandate.signature"]


def test_order_signed_by_someone_other_than_the_agent_is_refused(world):
    status, body = place(world, offer_for(world), 100, signer=Signer.generate("agent:impostor"))
    assert status == 403 and "order.signature" in failed(body)


def test_over_the_agents_limit_without_approval_is_refused(world):
    status, body = place(world, offer_for(world), 3000)
    assert status == 409 and failed(body) == ["authority"]


def test_signed_approval_of_exactly_these_lines_is_accepted(world):
    status, body = place(world, offer_for(world), 3000, approval=approval_for(world, 3000, 3000.0))
    assert status == 201 and body["approved_by"] == "Mikko Laine"


@pytest.mark.parametrize("units,total,forge", [
    (2000, 3000.0, False),      # approved different lines
    (3000, 2000.0, False),      # approved a lower total
    (3000, 3000.0, True),       # signed by someone other than the approver
])
def test_approval_that_does_not_cover_the_order_is_refused(world, units, total, forge):
    approval = approval_for(world, units, total, signer=Signer.generate("approver:x") if forge else None)
    status, body = place(world, offer_for(world), 3000, approval=approval)
    assert status == 409 and failed(body) == ["authority"]


def test_an_approval_can_be_used_once(world):
    approval = approval_for(world, 3000, 3000.0)
    assert place(world, offer_for(world), 3000, approval=approval)[0] == 201
    status, body = place(world, offer_for(world), 3000, approval=approval)
    assert status == 409 and "approval.unused" in failed(body)


def test_absolute_cap_holds_even_with_approval(world):
    status, body = place(world, offer_for(world), 12000, approval=approval_for(world, 12000, 12000.0))
    assert status == 409 and "mandate.order_cap" in failed(body)


def test_expired_offer_is_refused(world):
    offer = offer_for(world)
    world.clock.now = NOW + timedelta(minutes=16)
    status, body = place(world, offer, 100)
    assert status == 409 and failed(body) == ["offer.expiry"]


def test_more_than_available_or_not_offered_is_refused(world):
    status, body = place(world, offer_for(world), 200_000)     # also over every spend limit: all are reported
    assert status == 409 and failed(body)[0] == "offer.lines"
    status, body = place(world, offer_for(world), 10, product_id="100002")
    assert status == 409 and failed(body) == ["offer.lines"]


def test_allergen_rules_are_enforced_by_valio_aimo_too(world, monkeypatch):
    """The school's mandate forbids nuts, including traces. Even if the school's agent tries, the gateway refuses."""
    monkeypatch.setattr(catalog, "load_index", lambda: INDEX + [product("200001", "Nut milk", may_contain=["AN"])])
    status, body = place(world, offer_for(world, ["200001"]), 10, buyer="school", product_id="200001")
    assert status == 409 and failed(body) == ["mandate.hard_constraints"]
    assert "may contain AN" in next(c["message"] for c in body["checks"] if not c["ok"])


def test_search_reports_near_misses(world):
    status, body = world.client._call("GET", "/v1/products", params={"q": "milk", "claims": ["VEGAN"]})
    assert status == 200 and body["products"] == []
    assert {n["reason"] for n in body["not_matching"]} == {"is not vegan"}


def test_product_page_shows_catalog_facts_and_rejects_odd_ids(world):
    status, body = world.client._call("GET", "/v1/products/100001")
    assert status == 200 and body["product_id"] == "100001" and "description" in body
    assert world.client._call("GET", "/v1/products/nope")[0] == 404
    assert world.client._call("GET", "/v1/products/a%20b")[0] == 422
