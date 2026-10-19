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
# Claude API key (console.anthropic.com > API keys). Used when PROVIDER=anthropic: put the key in
# .env as ANTHROPIC_API_KEY=... and set PROVIDER=anthropic.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
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

# Super admins sign in to /admin with Google (comma separated). Team admins are added by a super
# admin on the admin page (Teams).
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

# Rate limits per user (browser + IP; see bot/abuse.py).
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "8"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "60"))
# Spam: this many strikes (gibberish, repeats, off-topic, manipulation) within the window
# blocks the user for BLOCK_HOURS. A warning is shown from WARN_AFTER_STRIKES.
STRIKES_TO_BLOCK = int(os.getenv("STRIKES_TO_BLOCK", "5"))
STRIKE_WINDOW_MINUTES = int(os.getenv("STRIKE_WINDOW_MINUTES", "30"))
WARN_AFTER_STRIKES = int(os.getenv("WARN_AFTER_STRIKES", "3"))
BLOCK_HOURS = float(os.getenv("BLOCK_HOURS", "2"))
# Also block the IP (not just the browser). Note: people behind one office IP share it.
BLOCK_BY_IP = os.getenv("BLOCK_BY_IP", "true").lower() == "true"
# Answers being generated at the same time, across all users (protects the AI quota and CPU).
MAX_CONCURRENT_ANSWERS = int(os.getenv("MAX_CONCURRENT_ANSWERS", "20"))
# Proxies in front of the app that append to X-Forwarded-For: 0 = use the socket address
# (local), 1 = Cloud Run directly, 2 = Cloud Run behind an external load balancer.
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "8000"))

# Internal names the bot must never reveal (see bot/guardrails.py for the syntax).
INTERNAL_TERMS_FILE = Path(os.getenv("INTERNAL_TERMS_FILE", ROOT_DIR / "internal_terms.txt"))

# Session memory: a short summary of each user's past conversations, saved in their account
# (DATA_DIR / DATA_BUCKET) and added to every question.
SUMMARY_IDLE_MINUTES = int(os.getenv("SUMMARY_IDLE_MINUTES", "10"))
SUMMARY_EVERY_MESSAGES = int(os.getenv("SUMMARY_EVERY_MESSAGES", "12"))
SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "1200"))
SUMMARY_TTL_DAYS = int(os.getenv("SUMMARY_TTL_DAYS", "30"))

# ---------------------------------------------------------------- users
# "google" = everyone signs in with Google (GOOGLE_CLIENT_ID); each user has their own chat
# history and memory. "none" = no sign-in (local development; one shared local user).
USER_LOGIN = os.getenv("USER_LOGIN", "google" if GOOGLE_CLIENT_ID else "none").strip().lower()
# Signed-in users with a verified Google Workspace account on these domains are internal:
# they also get the internal knowledge (repo docs, Google Chat docs). Everyone else is a partner.
INTERNAL_DOMAINS = {d.strip().lower().lstrip("@") for d in
                    os.getenv("INTERNAL_DOMAINS", "pharmeasy.in,thyrocare.in").split(",") if d.strip()}
USER_SESSION_DAYS = int(os.getenv("USER_SESSION_DAYS", "14"))
# Teams created the first time the team list is read ("slug:Name" pairs, set in .env: team names
# can be internal). After that, super admins manage teams and their admins on the admin page.
DEFAULT_TEAMS = [tuple(p.strip().split(":", 1)) for p in os.getenv("DEFAULT_TEAMS", "").split(",") if ":" in p]
# With USER_LOGIN=none, treat the local user as internal (only for requests from this machine).
LOCAL_USER_INTERNAL = os.getenv("LOCAL_USER_INTERNAL", "false").lower() == "true"
MAX_CONVERSATIONS = int(os.getenv("MAX_CONVERSATIONS", "200"))

# App data (users, sources, jobs): same backend choice as the knowledge files.
DATA_BACKEND = os.getenv("DATA_BACKEND", KNOWLEDGE_BACKEND).strip().lower()
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))
DATA_BUCKET = os.getenv("DATA_BUCKET", KNOWLEDGE_BUCKET)
DATA_PREFIX = os.getenv("DATA_PREFIX", "data/")

# ---------------------------------------------------------------- internal knowledge sources
# Read-only GitHub token (fine-grained, "Contents: read" on the org's repos). Used by the admin
# "Sources" page to build and refresh one .md per repo.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
# How repos are read: "token" (GitHub API with GITHUB_TOKEN; servers), "git" (this computer's own
# git login, e.g. saved in the macOS Keychain; reads whatever you can read, no token needed),
# "auto" = token if GITHUB_TOKEN is set, else git.
GITHUB_AUTH = os.getenv("GITHUB_AUTH", "auto").strip().lower()
GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")
# Only repos of these GitHub orgs/users can be added (comma separated; empty = any).
GITHUB_ORGS = {o.strip().lower() for o in os.getenv("GITHUB_ORGS", "pharmeasy").split(",") if o.strip()}
# How much source code one repo doc is built from, and how much the AI reads per call.
REPO_MAX_CHARS = int(os.getenv("REPO_MAX_CHARS", "900000"))
REPO_BATCH_CHARS = int(os.getenv("REPO_BATCH_CHARS", "150000"))
# Weekly updates read the diff; a bigger diff than this rebuilds the doc from scratch.
DELTA_MAX_CHARS = int(os.getenv("DELTA_MAX_CHARS", "200000"))
# Google Chat: the OAuth token of the admin who signed in once with
# `python chat_export/export_gchat.py --list-spaces` (reads the spaces that person is in).
GCHAT_TOKEN_FILE = Path(os.getenv("GCHAT_TOKEN_FILE", ROOT_DIR / "chat_export" / "token.json"))
# Who asks and who answers in the Chat groups (chat_export/private_names.example.json).
PRIVATE_NAMES_FILE = Path(os.getenv("PRIVATE_NAMES_FILE", ROOT_DIR / "chat_export" / "private_names.json"))
# Timeout for one document-writing AI call.
DOC_AI_TIMEOUT_SECONDS = int(os.getenv("DOC_AI_TIMEOUT_SECONDS", "900"))
# Internal knowledge added to an internal user's question (the most relevant parts only).
INTERNAL_CONTEXT_CHARS = int(os.getenv("INTERNAL_CONTEXT_CHARS", "60000"))

# Scheduled refreshes. Times are in SCHEDULE_TIMEZONE. On Cloud Run (which can scale to zero)
# use Cloud Scheduler to call POST /api/cron/tick with the X-Cron-Secret header instead.
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "Asia/Kolkata")
CRON_SECRET = os.getenv("CRON_SECRET", "")

# ---------------------------------------------------------------- documentation gaps
# Questions the docs couldn't answer are collected; once a day per team (default 15:00) the AI
# researches them (docs, repo code, chats), drafts the missing sections and emails the team's
# admins, who add or reject each draft on the admin page (Gaps).
GAP_DEFAULT_TIME = os.getenv("GAP_DEFAULT_TIME", "15:00")
GAP_MAX_DRAFTS = int(os.getenv("GAP_MAX_DRAFTS", "10"))       # drafts per team per run
# Gmail account that sends the emails (token from `python scripts/connect_gmail.py`).
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", ROOT_DIR / "chat_export" / "gmail_token.json"))
# Address of this app, for links in emails.
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")

# ---------------------------------------------------------------- answer cache
# The same first question (same audience, team and knowledge version) gets the saved answer
# instead of a new AI call. Any knowledge change starts a fresh cache; a 👎 removes that answer.
ANSWER_CACHE_ENABLED = os.getenv("ANSWER_CACHE_ENABLED", "true").lower() == "true"
ANSWER_CACHE_DAYS = float(os.getenv("ANSWER_CACHE_DAYS", "7"))

# ---------------------------------------------------------------- interaction insights
# Every question, answer and feedback is saved as JSON (secrets scrubbed). On the insights
# schedule (default Monday 10:00) the AI analyses everything since the last run, the report is
# saved and emailed to super admins, and the raw records are deleted.
ANALYTICS_ENABLED = os.getenv("ANALYTICS_ENABLED", "true").lower() == "true"
ANALYTICS_DEFAULT_TIME = os.getenv("ANALYTICS_DEFAULT_TIME", "10:00")
ANALYTICS_BATCH_CHARS = int(os.getenv("ANALYTICS_BATCH_CHARS", "150000"))
# Keep a copy of the analysed raw records for this many runs (0 = delete right after the report).
ANALYTICS_KEEP_EXPORTS = int(os.getenv("ANALYTICS_KEEP_EXPORTS", "0"))
