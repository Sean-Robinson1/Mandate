"""Valio Aimo's signing key and its registry of trusted buyer organisations.

The gateway holds one private key (it signs offers and order confirmations) and only public keys of buyers.
"""
from __future__ import annotations

from pathlib import Path

import settings as config
from protocol.registry import TrustRegistry
from protocol.signing import Signer

GATEWAY_KEY_ID = "valio-aimo-gateway-2026"


def gateway_signer(root: Path | None = None) -> Signer:
    path = Path(root or config.KEYS_DIR) / "gateway" / f"{GATEWAY_KEY_ID}.pem"
    return Signer.load_or_create(GATEWAY_KEY_ID, path)


def registry(root: Path | None = None) -> TrustRegistry:
    return TrustRegistry(Path(root or config.KEYS_DIR) / "registry")
