"""Start the demo afresh: clear both databases and re-issue version 1 of each mandate. Keys are kept.

    .venv/Scripts/python -m scripts.reset          (stop scripts.dev first: the databases must not be in use)
"""
import shutil
import sys
from pathlib import Path

import settings


def _sqlite_file(url: str) -> Path | None:
    return Path(url.removeprefix("sqlite:///")) if url.startswith("sqlite:///") else None


def main() -> int:
    targets = [p for p in (_sqlite_file(settings.DATABASE_URL), _sqlite_file(settings.GATEWAY_DATABASE_URL)) if p]
    try:
        for path in targets:
            path.unlink(missing_ok=True)
    except PermissionError:
        print("A database is in use. Stop the services (scripts.dev) and run this again.")
        return 1
    mandates = settings.KEYS_DIR / "buyer" / "mandates"
    if mandates.exists():
        shutil.rmtree(mandates)
    print("Cleared:", ", ".join(str(p.name) for p in targets) + ", mandate versions. Keys kept.")
    print("Next start issues version 1 of each mandate, a fresh supplies sheet and an empty audit log.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
