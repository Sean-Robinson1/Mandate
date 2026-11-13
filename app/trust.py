"""Who signs what.

Demo keys are derived from fixed seeds so every run is identical (see ASSUMPTIONS.md).
In production the buyer organisation and the approver hold their own keys.
"""
from __future__ import annotations

from functools import lru_cache

from app.models import Mandate
from app.signing import Signer, sign_document, verify_document


@lru_cache(maxsize=None)
def org_signer(mandate_id: str) -> Signer:
    return Signer.from_seed(f"org:{mandate_id}", f"demo-org-key:{mandate_id}".encode())


@lru_cache(maxsize=None)
def approver_signer(approver: str) -> Signer:
    return Signer.from_seed(f"approver:{approver}", f"demo-approver-key:{approver}".encode())


def sign_mandate(mandate: Mandate) -> Mandate:
    doc = sign_document(mandate.model_dump(mode="json"), org_signer(mandate.mandate_id))
    return Mandate.model_validate(doc)


def verify_mandate(mandate: Mandate) -> bool:
    """True only if the mandate carries a valid signature from its organisation's key."""
    return verify_document(mandate.model_dump(mode="json"), org_signer(mandate.mandate_id).public_key_b64())


def sign_approval(record: dict, approver: str) -> dict:
    return sign_document(record, approver_signer(approver))


def verify_approval(record: dict, approver: str) -> bool:
    return verify_document(record, approver_signer(approver).public_key_b64())
