"""Ed25519 signing of offers, mandates and approvals over canonical JSON."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Cannot canonicalise value of type {type(value).__name__}")


def canonical_bytes(payload: dict) -> bytes:
    """Deterministic JSON encoding: the exact bytes that get signed and hashed."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    # Accept both padded and unpadded input (JWK values are unpadded).
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """An Ed25519 private key with the key id that verifiers look it up by."""

    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def generate(cls, key_id: str) -> "Signer":
        return cls(key_id, Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, key_id: str, seed: bytes) -> "Signer":
        """Deterministic key, so the demo produces identical signatures every run."""
        if len(seed) != 32:
            seed = hashlib.sha256(seed).digest()
        return cls(key_id, Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def load_or_create(cls, key_id: str, path: Path) -> "Signer":
        path = Path(path)
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{path} does not contain an Ed25519 private key")
            return cls(key_id, key)
        signer = cls.generate(key_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            signer._private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        return signer

    def sign(self, payload: dict) -> str:
        return _b64encode(self._private_key.sign(canonical_bytes(payload)))

    def _public_raw(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_key_b64(self) -> str:
        return _b64encode(self._public_raw())

    def public_jwk(self) -> dict:
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            # RFC 7517 requires base64url without padding.
            "x": _b64encode(self._public_raw()).rstrip("="),
            "kid": self.key_id,
            "use": "sig",
        }


def verify(payload: dict, signature: str, public_key_b64: str) -> bool:
    """True only if the signature is valid; never raises."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_b64decode(public_key_b64))
        public_key.verify(_b64decode(signature), canonical_bytes(payload))
        return True
    except Exception:
        return False


def _unsigned(doc: dict, sig_field: str, kid_field: str) -> dict:
    return {k: v for k, v in doc.items() if k not in (sig_field, kid_field)}


def sign_document(
    doc: dict,
    signer: Signer,
    sig_field: str = "signature",
    kid_field: str = "signer_key_id",
) -> dict:
    """Copy of doc with the signature and key id set; the signature covers the rest."""
    body = _unsigned(doc, sig_field, kid_field)
    return {**body, kid_field: signer.key_id, sig_field: signer.sign(body)}


def verify_document(
    doc: dict,
    public_key_b64: str,
    sig_field: str = "signature",
    kid_field: str = "signer_key_id",
) -> bool:
    signature = doc.get(sig_field) if isinstance(doc, dict) else None
    if not isinstance(signature, str):
        return False
    return verify(_unsigned(doc, sig_field, kid_field), signature, public_key_b64)
