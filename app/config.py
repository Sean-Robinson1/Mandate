"""Configuration. Secrets come only from environment variables (or a local, git-ignored .env)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

HELSINKI = ZoneInfo("Europe/Helsinki")

VALIO_API_BASE_URL = os.getenv("VALIO_API_BASE_URL") or "https://junction26.valioaimo.fi"
VALIO_API_KEY = os.getenv("VALIO_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'junction.db'}"
CATALOG_INDEX = Path(os.getenv("CATALOG_INDEX") or ROOT / "data" / "catalog_index.json")
KEYS_DIR = Path(os.getenv("KEYS_DIR") or ROOT / "keys")

# "claude_cli" (local: uses the Claude Code subscription), "gemini", or "none" (rule-based parser only).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude_cli")
CLAUDE_CLI_MODEL = os.getenv("CLAUDE_CLI_MODEL", "haiku")
