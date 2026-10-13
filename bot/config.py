"""Runtime settings, read from environment variables (see .env.example)."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR", ROOT_DIR / "knowledge"))
LOG_DIR = Path(os.getenv("LOG_DIR", ROOT_DIR / "logs"))

# Where the knowledge files live: "local" (KNOWLEDGE_DIR) or "gcs" (a Cloud Storage bucket).
KNOWLEDGE_BACKEND = os.getenv("KNOWLEDGE_BACKEND", "local").strip().lower()
KNOWLEDGE_BUCKET = os.getenv("KNOWLEDGE_BUCKET", "")
KNOWLEDGE_PREFIX = os.getenv("KNOWLEDGE_PREFIX", "knowledge/")
# How often each running instance checks whether an admin changed the knowledge.
KNOWLEDGE_REFRESH_SECONDS = int(os.getenv("KNOWLEDGE_REFRESH_SECONDS", "60"))

# "anthropic" = Claude API. "openai_compatible" = any OpenAI-compatible endpoint
# (Gemini, Groq, OpenRouter, Mistral, a local Ollama server, ...).
PROVIDER = os.getenv("PROVIDER", "anthropic").strip().lower()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

# "claude_code" = answers come from the local Claude Code CLI (`claude -p`) on your own
# subscription. Personal local testing only: the app refuses partner access codes and
# non-localhost requests in this mode.
CLAUDE_CODE_MODEL = os.getenv("CLAUDE_CODE_MODEL", "sonnet")
# Path or name of the Claude Code CLI, if it isn't on PATH as "claude".
CLAUDE_CODE_BIN = os.getenv("CLAUDE_CODE_BIN", "claude")

MODEL = {
    "anthropic": os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
    "claude_code": CLAUDE_CODE_MODEL,
}.get(PROVIDER, LLM_MODEL)
EFFORT = os.getenv("CLAUDE_EFFORT", "medium")
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "16000"))
# Server-side refusal fallback. Claude API only - set to "false" on Bedrock/Vertex.
ENABLE_FALLBACKS = os.getenv("ENABLE_FALLBACKS", "true").lower() == "true"

BOT_NAME = os.getenv("BOT_NAME", "Thyrocare Integration Assistant")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "the Thyrocare integration team")

# Comma-separated "partner:code" pairs, e.g. "acme:k3x9q,globex:p7m2w".
# Empty = no login required (local dev only).
ACCESS_CODES: dict[str, str] = {}
for _pair in os.getenv("ACCESS_CODES", "").split(","):
    if ":" in _pair:
        _partner, _code = _pair.split(":", 1)
        ACCESS_CODES[_code.strip()] = _partner.strip()

# Admins sign in to /admin with Google. Only these emails are admins (comma separated).
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
# OAuth "Web application" client ID from Google Cloud console (Credentials).
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
# Signs admin session cookies. Set a long random value in production.
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "")
# true only when the app is reachable solely through Google IAP (Cloud Run).
TRUST_IAP = os.getenv("TRUST_IAP", "false").lower() == "true"

# Local-development fallback: admin codes as "name:code" pairs. Leave empty in production.
ADMIN_CODES: dict[str, str] = {}
for _pair in os.getenv("ADMIN_CODES", "").split(","):
    if ":" in _pair:
        _name, _code = _pair.split(":", 1)
        ADMIN_CODES[_code.strip()] = _name.strip()

RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "60"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "8000"))

# Internal names the bot must never reveal (see bot/guardrails.py for the syntax).
INTERNAL_TERMS_FILE = Path(os.getenv("INTERNAL_TERMS_FILE", ROOT_DIR / "internal_terms.txt"))

# Session memory: a short summary of the partner's past conversations, kept only in the
# partner's browser and sent along with each question.
SUMMARY_IDLE_MINUTES = int(os.getenv("SUMMARY_IDLE_MINUTES", "10"))
SUMMARY_EVERY_MESSAGES = int(os.getenv("SUMMARY_EVERY_MESSAGES", "12"))
SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "1200"))
SUMMARY_TTL_DAYS = int(os.getenv("SUMMARY_TTL_DAYS", "30"))
