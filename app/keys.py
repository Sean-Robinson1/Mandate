"""Who signs what on the buying side.

Each buyer organisation, its purchasing agent and its approver hold their own Ed25519 key (keys/buyer/*.pem,
created on first use). At onboarding the organisation registers its public key with Valio Aimo
(keys/registry/); Valio Aimo never sees a private key, and nobody can sign as someone else.

The organisation signs its mandate once, naming its agent's and its approver's public keys. The signed copy is
stored (keys/buyer/mandates/<buyer>.json) and loaded as is: nothing re-signs it at startup. Editing the file
breaks the signature, and then both the buyer's policy engine and Valio Aimo's gateway refuse to act on it.
"""
from __future__ import annotations

import re
from pathlib import Path

import settings as config
from app.mandates import DEMO_MANDATES
from protocol.mandate import Mandate
from protocol.registry import TrustRegistry
from protocol.signing import Signer, sign_document, verify_document


def _root(root: Path | None) -> Path:
    return Path(root or config.KEYS_DIR)


def signer(key_id: str, root: Path | None = None) -> Signer:
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", key_id) + ".pem"
    return Signer.load_or_create(key_id, _root(root) / "buyer" / filename)


def org_key_id(mandate_id: str) -> str:
    return f"org:{mandate_id}"


def approver_key_id(approver: str) -> str:
    return f"approver:{approver}"


def issue_mandate(draft: Mandate, root: Path | None = None) -> Mandate:
    """The organisation signs its mandate, naming the keys of its agent and its approver."""
    named = draft.model_copy(update={
        "agent_public_key": signer(draft.agent_id, root).public_key_b64(),
        "approver_public_key": signer(approver_key_id(draft.approver), root).public_key_b64(),
    })
    doc = sign_document(named.model_dump(mode="json", exclude={"signature", "signer_key_id"}),
                        signer(org_key_id(draft.mandate_id), root))
    return Mandate.model_validate(doc)


def onboard(mandate: Mandate, root: Path | None = None) -> None:
    """Register the organisation's public key with Valio Aimo (idempotent)."""
    org = signer(org_key_id(mandate.mandate_id), root)
    TrustRegistry(_root(root) / "registry").register(org.key_id, mandate.buyer_org, org.public_key_b64())


def load_mandates(root: Path | None = None) -> dict[str, Mandate]:
    """The signed mandates, issued and registered on first run, loaded unchanged afterwards."""
    out = {}
    for key, draft in DEMO_MANDATES.items():
        path = _root(root) / "buyer" / "mandates" / f"{key}.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(issue_mandate(draft, root).model_dump_json(indent=2), encoding="utf-8")
        mandate = Mandate.model_validate_json(path.read_text(encoding="utf-8"))
        onboard(mandate, root)
        out[key] = mandate
    return out


def verify_mandate(mandate: Mandate, root: Path | None = None) -> bool:
    """True only if the mandate carries a valid signature from its organisation's key."""
    org = signer(org_key_id(mandate.mandate_id), root)
    return mandate.signer_key_id == org.key_id and \
        verify_document(mandate.model_dump(mode="json"), org.public_key_b64())


def sign_order(order: dict, mandate: Mandate, root: Path | None = None) -> dict:
    return sign_document(order, signer(mandate.agent_id, root))


def sign_approval(record: dict, mandate: Mandate, root: Path | None = None) -> dict:
    return sign_document(record, signer(approver_key_id(mandate.approver), root))


def verify_approval(record: dict, mandate: Mandate) -> bool:
    return bool(mandate.approver_public_key) and verify_document(record, mandate.approver_public_key)
