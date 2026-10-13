# Running the Integration Assistant on your machine

This guide takes you from nothing to the chatbot running at http://localhost:8000, in about 15 minutes.

You'll need:

| What | Where from |
|---|---|
| A Mac, Linux or Windows (WSL) machine with **Python 3.10+** and **git** | Check with `python3 --version` and `git --version` |
| Access to the GitHub repo | `SuvitKumar03/DSA-API-Integration-bot` |
| **`private-files.zip`** | From Suvit, shared privately. It holds the docs the bot answers from and the list of internal names it must hide; neither is in the public repo |
| **Your own Claude** (Pro, Max, Team or Enterprise), or a free Gemini key | Step 4 |

## 1. Get the code

```bash
git clone https://github.com/SuvitKumar03/DSA-API-Integration-bot.git
cd DSA-API-Integration-bot
git checkout feature/partner-assistant
```

## 2. Add the private files

Put `private-files.zip` in the repo folder, then:

```bash
unzip private-files.zip
```

Check that you now have these files:

```
knowledge/01-b2b-api-suite.md
knowledge/02-b2b-lead-api.md
knowledge/03-known-gaps.md
knowledge/04-faq.md
knowledge/PARTNER_API_INTEGRATION_GUIDE.md
internal_terms.txt
```

They are git-ignored, so they can't be committed by accident. **Never commit or upload them anywhere public.**

## 3. Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows (WSL) too; plain Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run `source .venv/bin/activate` again in every new terminal before running the app.

## 4. Choose which AI answers

**Option A: your own Claude (recommended).** Answers come from your own Claude account through
Claude Code, the same way Suvit runs it. No API key needed.

1. Install Claude Code:
   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ```
   (or `npm install -g @anthropic-ai/claude-code` if you use Node 18+)
2. Sign in once: run `claude` in a terminal, follow the browser sign-in with your Claude
   account, then type `/exit`.
3. Check: `claude --version` prints a version.

Good to know:
- Answers count against **your** Claude plan's usage limits. If you hit the limit, the chat says
  so; it resets after a few hours, or switch to Gemini meanwhile.
- This mode only answers on your own machine (localhost). It can't be shared with partners.
- `sonnet` is used by default. To change it, set `CLAUDE_CODE_MODEL=opus` or `haiku` in `.env`.

**Option B: Gemini (free).**
1. Open https://aistudio.google.com/apikey and sign in with your Google account.
2. Click **Create API key** and copy it.

**Option C: Claude API key** (for the shared deployment): an `ANTHROPIC_API_KEY` from the org's
Anthropic Console account. Ask Suvit.

Use your own account or key. Don't share keys in chat or commit them.

## 5. Create your `.env`

```bash
cp .env.example .env
```

The example is already set up for **option A** (`PROVIDER=claude_code`), so there's nothing to
change. For another option, set **one** of these blocks in `.env` instead:

```bash
# Option B: Gemini
PROVIDER=openai_compatible
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=<your Gemini key>
LLM_MODEL=gemini-flash-lite-latest
```

```bash
# Option C: Claude API
PROVIDER=anthropic
ANTHROPIC_API_KEY=<the key>
```

Leave `ACCESS_CODES` empty for local use (no partner login).

**Check everything in one go:**

```bash
python scripts/check_setup.py
```

It checks Python, the packages, the private files and `.env`, and sends one tiny test question to
your chosen AI. Each ❌ line says how to fix it. When it prints "All good", go on.

## 6. Run it

```bash
uvicorn app.main:app --port 8000
```

You should see `Loaded documentation (... chars)` and `Uvicorn running on http://127.0.0.1:8000`.

Open **http://localhost:8000** (use `localhost`, not `127.0.0.1`; it matters for the admin sign-in). Stop the server with `Ctrl+C`.

Quick checks:

| Ask | Expect |
|---|---|
| What values does ageType accept in the /v1 create order API? | A one or two line answer: `YEAR`, `MONTH`, `DAY`, with a Source line |
| my order is failing | A short clarifying question (which API, which error) |
| Which internal service handles webhooks? | A polite refusal |

Or test in the terminal without the browser: `python cli.py "How do I create a lead?"`

## 7. Admin page (optional)

The admin page at http://localhost:8000/admin updates the bot's docs section by section, with preview, history, restore and an audit trail. Changes affect **your local** `knowledge/` files only.

**Sign in with Google:** add to your `.env`:

```bash
ADMIN_EMAILS=<your work email>
GOOGLE_CLIENT_ID=<ask Suvit; it is not a secret>
ADMIN_SESSION_SECRET=<any long random text>     # e.g. output of: python3 -c "import secrets;print(secrets.token_urlsafe(48))"
```

Restart the server, open http://localhost:8000/admin and click **Sign in with Google**.

**Or, quicker for local testing**, an admin code:

```bash
ADMIN_CODES=<your name>:<a code you choose>
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `RuntimeError: No knowledge files found` | Step 2 wasn't done: unzip `private-files.zip` in the repo root |
| `ModuleNotFoundError` | The virtual environment isn't active: `source .venv/bin/activate`, then `pip install -r requirements.txt` |
| `Address already in use` | Something else uses port 8000. Stop it, or run on `--port 8001` (then Google admin sign-in needs that origin added, so prefer freeing 8000) |
| Chat says "⚠️ Local setup issue: Claude Code isn't installed" | Do option A in step 4, or set `CLAUDE_CODE_BIN` to the full path of `claude` |
| Chat says "Claude Code isn't signed in" | Run `claude` in a terminal and sign in, then ask again |
| Chat says "usage limit was reached" | Your Claude plan's limit; wait a few hours or switch to Gemini (option B) |
| "Credit balance is too low" | An API key with no credits is being used. With option A, remove `ANTHROPIC_API_KEY` from your shell (`unset ANTHROPIC_API_KEY`); with option C, ask for a key with credits |
| Gemini answers very slowly or times out | Use `LLM_MODEL=gemini-flash-lite-latest` |
| Server won't start: "Claude Code isn't installed" | `claude --version` must work in the same terminal; run `python scripts/check_setup.py` |
| Google sign-in: `Error 400: origin_mismatch` | Open `http://localhost:8000/admin` exactly (not `127.0.0.1`), or ask Suvit to add your origin to the OAuth client |
| Google sign-in: "... is not an admin" | Your email isn't in `ADMIN_EMAILS` in your `.env` |
| `/admin` says "Admin is not enabled" | Set `ADMIN_EMAILS` (with `GOOGLE_CLIENT_ID`) or `ADMIN_CODES`, then restart |

## Good to know

- **What's private:** `.env`, `knowledge/*.md`, `internal_terms.txt`, `logs/`, `chat_export/output/` and the PDFs are git-ignored. Keep it that way: the repo is public.
- **Where things are:** see the Architecture section of the [README](../README.md) for diagrams of every flow and which file does what.
- **Logs:** your questions and answers go to `logs/chats.jsonl` on your machine only.
- **Getting updates:** `git pull` for code. For new docs, either update them through `/admin` or get a fresh `private-files.zip`.
