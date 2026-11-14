from protocol.signing import Signer, sign_document, verify_document
from scripts.verify_offer import tamper


def test_tampered_offer_fails_verification():
    signer = Signer.from_seed("valio-aimo-gateway-2026", b"test-seed")
    offer = sign_document(
        {
            "offer_id": "off-1",
            "issuer": "Valio Aimo",
            "items": [{"product_id": "p1", "name": "Milk", "unit_price": 1.25}],
        },
        signer,
    )
    key = signer.public_key_b64()
    assert verify_document(offer, key) is True
    bad = tamper(offer)
    assert bad["items"][0]["unit_price"] == 1.24
    assert verify_document(bad, key) is False
