import base64
from datetime import date, datetime, timezone
from enum import Enum

import pytest

from app.signing import (
    Signer,
    canonical_bytes,
    sign_document,
    verify,
    verify_document,
)


class Unit(Enum):
    KG = "kg"
    CASE = "case"


PAYLOAD = {
    "offer_id": "OFF-1",
    "sku": "VAL-1001",
    "quantity": 12,
    "unit_price": 3.45,
    "lines": [{"sku": "VAL-1001", "qty": 12}],
}


@pytest.fixture
def signer() -> Signer:
    return Signer.from_seed("gateway-test", b"test-seed")


def test_round_trip(signer):
    signature = signer.sign(PAYLOAD)
    assert verify(PAYLOAD, signature, signer.public_key_b64())


@pytest.mark.parametrize(
    "field,value",
    [
        ("offer_id", "OFF-2"),
        ("sku", "VAL-1002"),
        ("quantity", 13),
        ("unit_price", 3.44),
        ("lines", [{"sku": "VAL-1001", "qty": 120}]),
    ],
)
def test_tampering_with_any_field_fails(signer, field, value):
    signature = signer.sign(PAYLOAD)
    tampered = {**PAYLOAD, field: value}
    assert not verify(tampered, signature, signer.public_key_b64())


def test_added_or_removed_field_fails(signer):
    signature = signer.sign(PAYLOAD)
    key = signer.public_key_b64()
    assert not verify({**PAYLOAD, "extra": 1}, signature, key)
    assert not verify({k: v for k, v in PAYLOAD.items() if k != "sku"}, signature, key)


def test_wrong_key_fails(signer):
    other = Signer.generate("someone-else")
    assert not verify(PAYLOAD, signer.sign(PAYLOAD), other.public_key_b64())


def test_key_order_does_not_matter(signer):
    reordered = dict(reversed(list(PAYLOAD.items())))
    assert canonical_bytes(reordered) == canonical_bytes(PAYLOAD)
    assert verify(reordered, signer.sign(PAYLOAD), signer.public_key_b64())


def test_from_seed_is_deterministic():
    a = Signer.from_seed("k", b"demo-seed")
    b = Signer.from_seed("k", b"demo-seed")
    c = Signer.from_seed("k", b"other-seed")
    assert a.public_key_b64() == b.public_key_b64()
    assert a.sign(PAYLOAD) == b.sign(PAYLOAD)
    assert a.public_key_b64() != c.public_key_b64()


def test_from_seed_accepts_exact_32_bytes():
    seed = bytes(range(32))
    assert (
        Signer.from_seed("k", seed).public_key_b64()
        == Signer.from_seed("k", seed).public_key_b64()
    )


def test_load_or_create_persists_and_reloads(tmp_path):
    path = tmp_path / "keys" / "nested" / "gateway.pem"
    created = Signer.load_or_create("gateway", path)
    assert path.exists()
    reloaded = Signer.load_or_create("gateway", path)
    assert reloaded.key_id == "gateway"
    assert reloaded.public_key_b64() == created.public_key_b64()
    assert verify(PAYLOAD, reloaded.sign(PAYLOAD), created.public_key_b64())


@pytest.mark.parametrize(
    "signature", ["", "not base64 !!!", "AAAA", "é", "a" * 86, None, 123]
)
def test_garbage_signature_returns_false(signer, signature):
    assert verify(PAYLOAD, signature, signer.public_key_b64()) is False


def test_garbage_public_key_returns_false(signer):
    signature = signer.sign(PAYLOAD)
    assert verify(PAYLOAD, signature, "nonsense") is False
    assert verify(PAYLOAD, signature, "") is False


def test_dates_and_enums_serialise(signer):
    payload = {
        "delivery_date": date(2026, 11, 14),
        "issued_at": datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc),
        "unit": Unit.KG,
        "name": "Kevytmaito 1 l äö",
    }
    assert canonical_bytes(payload) == (
        '{"delivery_date":"2026-11-14","issued_at":"2026-11-13T09:30:00+00:00",'
        '"name":"Kevytmaito 1 l äö","unit":"kg"}'
    ).encode("utf-8")
    signature = signer.sign(payload)
    assert verify(payload, signature, signer.public_key_b64())
    # A verifier that only has the JSON form sees the same bytes.
    as_json = {
        "delivery_date": "2026-11-14",
        "issued_at": "2026-11-13T09:30:00+00:00",
        "unit": "kg",
        "name": "Kevytmaito 1 l äö",
    }
    assert verify(as_json, signature, signer.public_key_b64())


def test_unserialisable_value_raises_on_sign(signer):
    with pytest.raises(TypeError):
        signer.sign({"bad": object()})


def test_public_jwk(signer):
    jwk = signer.public_jwk()
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    assert jwk["kid"] == "gateway-test"
    assert jwk["use"] == "sig"
    raw = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    assert len(raw) == 32
    # The unpadded JWK value is accepted directly as a verification key.
    assert verify(PAYLOAD, signer.sign(PAYLOAD), jwk["x"])


def test_sign_document_round_trip(signer):
    signed = sign_document(PAYLOAD, signer)
    assert signed["signer_key_id"] == "gateway-test"
    assert "signature" not in PAYLOAD  # original untouched
    assert verify_document(signed, signer.public_key_b64())


def test_sign_document_tamper_and_missing_signature(signer):
    signed = sign_document(PAYLOAD, signer)
    key = signer.public_key_b64()
    assert not verify_document({**signed, "quantity": 999}, key)
    assert not verify_document(PAYLOAD, key)
    assert not verify_document({**signed, "signature": None}, key)


def test_sign_document_resigning_ignores_old_signature(signer):
    signed = sign_document(PAYLOAD, signer)
    assert sign_document(signed, signer) == signed


def test_sign_document_custom_fields(signer):
    signed = sign_document(PAYLOAD, signer, sig_field="sig", kid_field="kid")
    assert set(signed) == set(PAYLOAD) | {"sig", "kid"}
    assert verify_document(signed, signer.public_key_b64(), "sig", "kid")
    assert not verify_document(signed, signer.public_key_b64())
