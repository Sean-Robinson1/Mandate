"""Valio Aimo's registry of buyer organisations' public keys.

A buyer organisation registers its public key once, at onboarding; afterwards the gateway accepts mandates
signed with it. The registry is a folder of small JSON files {"key_id", "org", "public_key"}; in production
this is Valio Aimo's customer onboarding, not a shared folder.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _file(directory: Path, key_id: str) -> Path | None:
    if not key_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", key_id):
        return None
    return directory / (key_id.replace(":", "_") + ".json")


class TrustRegistry:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def public_key(self, key_id: str | None, org: str | None = None) -> str | None:
        """The key registered under key_id; with org, only if it was registered for that organisation."""
        path = _file(self.dir, key_id or "")
        if path is None or not path.is_file():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        if org is not None and entry.get("org") != org:
            return None
        return entry.get("public_key")

    def register(self, key_id: str, org: str, public_key: str) -> None:
        path = _file(self.dir, key_id)
        if path is None:
            raise ValueError(f"invalid key id {key_id!r}")
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key_id": key_id, "org": org, "public_key": public_key}, indent=2),
                        encoding="utf-8")
