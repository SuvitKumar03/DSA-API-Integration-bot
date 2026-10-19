# CLAUDE.md: project guide for AI sessions

Read this first. It is enough to continue work without the earlier conversation. If
`CLAUDE.local.md` exists, read it too: it holds the private context (decisions, people, data
locations) that can't be in this public repo. For the full picture with diagrams, see the
"Architecture (current build)" section of `README.md`.

## What this is

A chatbot that answers Thyrocare B2B/DSA partners' API-integration questions from the partner
documentation, in short, exact answers. It never reveals internal architecture to partners.
Everyone signs in with Google; each user's chats and memory are saved per user. Verified
Workspace accounts on `INTERNAL_DOMAINS` (pharmeasy.in, thyrocare.in) are **internal** and also
get internal knowledge: one doc per GitHub repo and per Google Chat space, built and refreshed on
a per-source schedule by the admin's Sources page. Admins update everything through `/admin`.
Hierarchy: super admin (`ADMIN_EMAILS`) → team admins per team (`bot/teams.py`; first teams from
`DEFAULT_TEAMS` in `.env`, names kept out of code since some are internal terms). Each team has its own internal knowledge (`internal/<team>/...`);
internal users pick a team on the chat page and answers use only that team's + shared docs.

Status: V0 done and shared with the team for testing (company devs run it locally on their own
Claude). Not deployed yet; Google Cloud (Cloud Run) is the target.

## Ground rules (must follow)

1. **The GitHub repo is PUBLIC.** Work on branch `feature/partner-assistant`; never commit to
   `main`. Never commit `.env`, `knowledge/*.md` (except `knowledge/README.md`), PDFs,
   `internal_terms.txt`, `chat_export/private_names.json`, `chat_export/output/`, `notes/`,
   `logs/`, `deploy/env.yaml`, `private-files.zip`, `CLAUDE.local.md`. All are git-ignored; keep
   it that way.
2. **Before every commit:** `git add -A && .venv/bin/python scripts/leak_scan.py`. Fix any real
   finding (names, keys, emails, internal terms); explain expected ones. Commit only when the
   user asks; push only when the user asks.
3. **Never expose internals to partners**: internal service/DB/host/class/function names,
   internal headers or values. Answers cover only the public API contract: what the API expects,
   what the partner sent, why the error happens. New words to hide go in `internal_terms.txt`
   (never in code, since the list itself is sensitive). Internal knowledge (`knowledge/internal/`)
   must only ever reach users with `user.is_internal` (prompt audience "internal"); partners' prompts
   load `knowledge/*.md` only. Never send secrets to the AI: repo code passes `scrub_code()`.
4. **Answer style** (team feedback): short and crisp, only what was asked; ask one clarifying
   question when a question is ambiguous; partner orders are home collection only.
5. **Tests that use the real AI or a browser: one process at a time.** The user's Mac has frozen
   under parallel server + `claude -p` + headless Chrome (load average > 180). Check `uptime`
   first; stop test servers afterwards.
6. Commit messages end with the `Co-Authored-By` line the session's instructions give.
7. Match the existing code: small focused modules, docstring at the top explaining why, sparse
   comments, plain functions. Keep user-facing text simple (the team includes non-native
   English speakers; the user writes casual English/Hinglish).

## Run

```bash
source .venv/bin/activate              # after every restart / new terminal
python scripts/check_setup.py          # checks Python, packages, private files, .env, AI
uvicorn app.main:app --port 8000       # chat: http://localhost:8000   admin: /admin
```

Use `localhost`, not `127.0.0.1` (Google sign-in origin). Private files come in
`private-files.zip` (made by `scripts/pack-private.sh`). Teammate setup: `docs/LOCAL_SETUP.md`.

## Code map

| File | Role |
|---|---|
| `app/main.py` | FastAPI app. Endpoints below, startup checks, logging, admin, abuse wiring |
| `app/static/index.html` | Chat page (single file): Google sign-in, saved chats ("Your chats"), streaming markdown, feedback, memory card, browser ID |
| `app/static/admin.html` | Admin page: Google sign-in, partner + internal files, section updates with red/green diff, history/restore, last-modified, audit trail, blocked users, knowledge Sources (add links, schedules, run/rebuild/pause/remove) |
| `bot/config.py` | All settings from env / `.env` (see the README configuration table) |
| `bot/prompt.py` | System prompt per audience: INSTRUCTIONS (partner) or INTERNAL_INSTRUCTIONS + docs block (+ internal catalog) cached; then matched internal sections + memory block; SUMMARY_INSTRUCTIONS |
| `bot/engine.py` | Providers: `anthropic` (Claude API, prompt caching), `openai_compatible` (Gemini etc.), `claude_code` (local `claude -p` on the user's own subscription, localhost only). `stream_answer()`, `complete()` |
| `bot/guardrails.py` | Internal-terms rules: `sanitize_doc` (docs), `StreamRedactor` (answers), `scrub_secrets` (logs), `probe_flags` |
| `bot/knowledge_store.py` | Docs storage: local folder or GCS bucket; `internal/{repos,gchat,notes}/*.md` (staff only); `_version.json` (instances reload within 60 s), `_history/`, `_audit/`, `_last_modified.json` |
| `bot/users.py` | `User` (email, is_internal, method); internal = verified + `hd` claim on INTERNAL_DOMAINS; per-user `users/<uid>/` profile, memory, conversations |
| `bot/teams.py` | Team registry (`teams/registry.json`, seeded from DEFAULT_TEAMS in .env); `admin_teams(email)`, `is_super_email` |
| `bot/gaps.py` | Gaps: `GapCatcher` strips `[[GAP: ...]]` from answers; records per bucket (team / shared / partner); daily `run_bucket` (group → research docs + GitHub code search → draft JSON → proposals → email) |
| `bot/analytics.py` | `record()` one JSON per interaction (`analytics/events/`); weekly `run()` (code numbers + AI notes per batch + report) then deletes the events |
| `bot/stats.py` | Daily counters per bucket (`stats/daily/<date>.json`): asked, answered, gaps, cached, off_topic, spam, errors, up, down; `GET /api/admin/stats?days=` |
| `bot/answer_cache.py` | Cache of first answers keyed by version + audience + team + normalised question; checked in `/api/chat` before spam checks and the AI |
| `bot/mailer.py` | Gmail API send-only (`GMAIL_TOKEN_FILE` from `scripts/connect_gmail.py`) |
| `bot/blobs.py` | App data storage (users, sources): `DATA_DIR` or GCS (`DATA_BUCKET`/`DATA_PREFIX`) |
| `bot/retrieval.py` | Internal docs: catalog (cached) + BM25 section search for internal users' questions |
| `bot/sources.py` | Source registry (`sources/registry.json`) and schedules: weekly days+time, every N days, manual (SCHEDULE_TIMEZONE) |
| `bot/jobs.py` | One-at-a-time job queue, per-minute scheduler, `/api/cron/tick`; saves docs with history + audit |
| `bot/github_docs.py` | Repo → doc: tarball of master/default branch, file priority, `scrub_code`, map-reduce build; weekly delta via compare API + section replace + change log |
| `bot/git_local.py` | `GITHUB_AUTH=git`: same interface as `GitHub` via the local `git` login (clones in `data/git/`), compare from `git diff`, `grep` for gap code search |
| `bot/gchat_docs.py` | Chat space → doc: Chat API with the admin's token, `clean_chat` windows, `extraction_prompt.md`, new Q&A appended per API area |
| `bot/sections.py` | Merge a pasted `## Heading` section into a doc (replace same title, else append); diff |
| `bot/admin_auth.py` | Admin identity: Google ID token → signed HttpOnly cookie; IAP header; dev admin codes |
| `bot/abuse.py` | Rate limits (8/min, 60/h per browser ID + IP), spam strikes, 2-hour blocks, concurrency limits. In memory |
| `cli.py` | Terminal chat for testing |
| `chat_export/` | Offline pipeline: `export_gchat.py` → `clean_chat.py` (redaction) → AI extraction with `extraction_prompt.md` → `build_faq.py` → `knowledge/04-faq.md` |
| `scripts/` | `check_setup.py`, `pack-private.sh`, `leak_scan.py` |
| `docs/` | `LOCAL_SETUP.md` (teammates), `deploy-gcp.md` (Cloud Run step by step) |
| `deploy/env.example.yaml`, `.gcloudignore`, `Dockerfile` | Cloud Run deploy; private files come from Secret Manager + bucket, never baked into the image |

Endpoints: `GET /`, `/admin`, `/healthz`, `/api/config`, `/api/me`, `/api/conversations[/{id}]`;
`DELETE /api/conversations/{id}`, `/api/memory`; `POST /api/user/google-login|logout`, `/api/login`
(access codes), `/api/chat` (streams text; `conversation_id`), `/api/feedback`, `/api/summarize`,
`/api/cron/tick` (X-Cron-Secret); admin: `GET /api/admin/auth-config|me|files|files/{name:path}|audit|blocks|sources`,
`POST /api/admin/google-login|logout|preview|apply|restore|unblock|block|sources`,
`POST /api/admin/sources/{id}/settings|run|remove`; super only: `GET|POST /api/admin/teams`,
`POST /api/admin/teams/{slug}[/remove]`, blocks.

## How the main flows work (short)

- **Answer:** `/api/chat` → user (Google cookie `user_session`, aud "user") → block check → rate
  limit → saved conversation + new question → cheap spam checks (gibberish, repeat, manipulation)
  → concurrency slot → `prompt.system_blocks(summary, audience, extra)` (partner: instructions +
  all `knowledge/*.md` sanitized; internal: internal instructions + docs + catalog, plus BM25
  sections) → provider → hidden `[[OFF_TOPIC]]` marker stripped (strike) → `StreamRedactor`
  (partners only) → user; turn saved to `users/<uid>/conversations/`. Logged to `logs/chats.jsonl`.
- **Session memory:** ≤120-word summary saved per user (`users/<uid>/memory.json`), made via
  `/api/summarize` (conversation id + `since`) after 10 min idle / 12 messages / New chat / tab
  hidden; ignored after 30 days; Forget = `DELETE /api/memory`.
- **Teams:** `_admin()` returns `{admin, method, super, teams}`; `_can_edit()` limits team admins
  to `internal/<their team>/...` and their sources. Chat: `team` from the saved conversation, else
  the request; `retrieval.context_for(history, team)` and `static_blocks(audience, team)` (catalog
  per team, so claude_code keeps one system file per team).
- **Cache and insights:** `/api/chat` returns a cached first answer (header `X-Answer: cached`)
  before any AI call; clean complete first answers are saved; 👎 drops it. Every chat / feedback /
  spam reply is recorded for the weekly insights job (`insights` in the job queue).
- **Gaps:** answer ends with hidden `[[GAP: topic]]` (prompt rule) or 👎 with conversation id →
  `gaps/items/<bucket>/`. Job `gaps:<bucket>` in the same queue, daily (default 15:00) → drafts in
  `gaps/proposals/`; admin Add goes through `_apply_change` (history + audit). Internal drafts only
  into `internal/<team>/notes/`. Never auto-adds.
- **Sources:** admin adds repo / org / Chat links + team + schedule → `sources/registry.json` → job queue
  (one at a time). GitHub: first run full build; later runs compare `last_sha...head` (no commits =
  no AI call; huge diff or force-push = rebuild). Chat: messages after `last_message_time`. Doc
  saved like an admin edit (history, version bump, audit `source_update`). `USER_LOGIN=none` +
  `LOCAL_USER_INTERNAL=true` lets you test internal answers locally without Google.
- **Knowledge admin:** preview → apply → old file to `_history/`, version bump, audit record,
  last-modified record → every instance reloads docs on the next question. No AI involved.
- **Abuse:** 5 strikes in 30 min → browser ID + IP blocked 2 h (IP alone at 15). Admins
  unblock in `/admin`.

## Testing patterns that work

- Offline, no AI cost: `fastapi.testclient.TestClient(app, client=("127.0.0.1", 5000))` (the
  local-only guard rejects other hosts with `PROVIDER=claude_code`); monkeypatch
  `app.main.stream_answer` with a fake async generator.
- Isolate data: run with `KNOWLEDGE_DIR=<scratch copy of knowledge/>` and `LOG_DIR=<scratch>`
  so tests never touch the real docs or logs.
- Live: one server or `TestClient` with `PROVIDER=claude_code`, a few sequential questions.
- Browser: Playwright with system Chrome (`channel="chrome"`), one browser at a time.
- README diagrams are Mermaid: validate by rendering with mermaid@10.9.1 in Playwright. In
  sequence diagrams `;` breaks a message line.

## Gotchas

- Prompt caching: instructions and docs must stay byte-identical between requests; per-request
  data only in the partner-context block.
- `claude_code` strips `ANTHROPIC_API_KEY` from the CLI environment, or the CLI bills that API
  account instead of the subscription. It refuses non-localhost requests and partner access codes.
- A deployed server cannot use a visitor's own Claude subscription (it can't reach their
  laptop, and subscription logins may not power another service). See CLAUDE.local.md for
  the open decision.
- `mcp` Python SDK 2.x renamed FastMCP to `MCPServer` (`mcp.server.mcpserver`).
- Cloud Run: set `TRUSTED_PROXY_HOPS=1`; `logs/` is ephemeral; rate limits/blocks and the job
  queue are per instance (keep max instances 1 until moved to Redis). Use `DATA_BACKEND=gcs`, CPU
  always allocated for background jobs, and Cloud Scheduler → `/api/cron/tick`.
- A `GOOGLE_CLIENT_ID` in `.env` turns on Google sign-in for the chat page too (`USER_LOGIN`
  defaults to google). Set `USER_LOGIN=none` for login-free local testing.
- Google Chat sources need `chat_export/token.json` and `private_names.json` on the machine
  running the jobs (local today; not yet in the Cloud Run setup).
- Repo docs are written by the configured PROVIDER (claude_code locally = slow but free).
  Bulk-adding the whole org queues ~190 full builds; they run one at a time.
- Google sign-in `origin_mismatch`: open `http://localhost:8000`, not `127.0.0.1`.
