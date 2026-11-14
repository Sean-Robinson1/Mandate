"""Deployment settings for both services. Secrets come only from environment variables (or a git-ignored .env)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

HELSINKI = ZoneInfo("Europe/Helsinki")
KEYS_DIR = Path(os.getenv("KEYS_DIR") or ROOT / "keys")    # keys/buyer, keys/gateway, keys/registry

# ---- Valio Aimo gateway (the wholesaler's side)
VALIO_API_BASE_URL = os.getenv("VALIO_API_BASE_URL") or "https://junction26.valioaimo.fi"
VALIO_API_KEY = os.getenv("VALIO_API_KEY", "")
GATEWAY_DATABASE_URL = os.getenv("GATEWAY_DATABASE_URL") or f"sqlite:///{ROOT / 'gateway.db'}"
CATALOG_INDEX = Path(os.getenv("CATALOG_INDEX") or ROOT / "data" / "catalog_index.json")

# ---- buyer's purchasing agent and console
GATEWAY_URL = os.getenv("GATEWAY_URL") or "http://127.0.0.1:8100"
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'junction.db'}"
# "claude_cli" (local: uses the Claude Code subscription), "gemini", or "none" (rule-based parser only).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude_cli")
CLAUDE_CLI_MODEL = os.getenv("CLAUDE_CLI_MODEL", "haiku")
# Shared secret a kitchen's own stock system sends to POST /api/stock-counts; unset = that endpoint is off.
STOCK_API_TOKEN = os.getenv("STOCK_API_TOKEN", "")
