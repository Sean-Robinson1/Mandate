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
# Valio Aimo's operators use this to open /operator on the gateway (pause an agent). Unset = the page is off.
OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "")
CATALOG_INDEX = Path(os.getenv("CATALOG_INDEX") or ROOT / "data" / "catalog_index.json")

# ---- buyer's purchasing agent and console
GATEWAY_URL = os.getenv("GATEWAY_URL") or "http://127.0.0.1:8100"
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'junction.db'}"
# Which LLM reads requests (see app/llm.py): auto, claude, claude_cli, openai, codex_cli, codex, gemini, none.
# "auto" picks the first available: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, claude CLI, codex CLI.
LLM_PROVIDER = os.getenv("LLM_PROVIDER") or "auto"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL") or "claude-haiku-5-5"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5-mini"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
CLAUDE_CLI_MODEL = os.getenv("CLAUDE_CLI_MODEL") or "haiku"
CODEX_CLI_MODEL = os.getenv("CODEX_CLI_MODEL", "")          # empty = the Codex CLI's own default
# Optional link to a product in Valio Aimo's webshop, e.g. "https://<shop>/search?q={gtin}" ({gtin} or {id}).
VALIO_SHOP_URL = os.getenv("VALIO_SHOP_URL", "")
# Shared secret a kitchen's own stock system sends to POST /api/stock-counts; unset = that endpoint is off.
STOCK_API_TOKEN = os.getenv("STOCK_API_TOKEN", "")
