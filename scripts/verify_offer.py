"""Show that a Valio Aimo signed offer cannot be altered without breaking its signature.

Usage: python -m scripts.verify_offer <offer.json> [--tamper]

Uses only protocol/ code. No network.
"""
from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from protocol.signing import Signer, verify_document

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_KEY_ID = "valio-aimo-gateway-2026"
USAGE = "usage: python -m scripts.verify_offer <offer.json> [--tamper]"


def gateway_public_key(keys_dir: Path = ROOT / "keys") -> str:
    # shortcut: the gateway has no published public key file, so derive it from the local PEM.
    # Revisit by fetching the gateway's JWKS. Only the public half is ever used or printed.
    pem = keys_dir / "gateway" / f"{GATEWAY_KEY_ID}.pem"
    key = serialization.load_pem_private_key(pem.read_bytes(), password=None)
    return Signer(GATEWAY_KEY_ID, key).public_key_b64()


def tamper(offer: dict) -> dict:
    """Copy of the offer with the first line's unit price lowered by EUR 0.01."""
    bad = copy.deepcopy(offer)
    item = bad["items"][0]
    item["unit_price"] = float(Decimal(str(item["unit_price"])) - Decimal("0.01"))
    return bad


def main(argv: list[str]) -> int:
    files = [a for a in argv if not a.startswith("--")]
    if len(files) != 1:
        print(USAGE)
        return 2
    offer = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    public_key = gateway_public_key()

    ok = verify_document(offer, public_key)
    print(f"Offer {offer.get('offer_id')} signed by {offer.get('signer_key_id')}")
    print("Signature valid" if ok else "Signature INVALID")
    if "--tamper" not in argv:
        return 0 if ok else 1

    if not offer.get("items"):
        print("Offer has no lines to tamper with")
        return 1
    bad = tamper(offer)
    item, old = bad["items"][0], offer["items"][0]["unit_price"]
    print(f"\nTampering: {item.get('name')} unit price EUR {old} -> EUR {item['unit_price']}")
    if verify_document(bad, public_key):
        print("Signature still valid: tamper NOT detected")
        return 1
    print("Signature check FAILED: the altered offer is rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
