# Before you deploy: checklist

What is needed to put the Integration Assistant on Google Cloud (Cloud Run), and why each
item is needed. The exact commands are in [deploy-gcp.md](deploy-gcp.md).

Legend: 🔴 blocker (can't deploy without it) · 🟡 needed for one feature · 🟢 setup step

---

## 🔴 1. Blockers

| # | What | Why |
|---|---|---|
| 1 | **Claude API key with credits** (Anthropic Console, org account). Or a Gemini key as a stop-gap. | On a laptop the bot can use a person's own Claude subscription. A server can't: it needs an API key that the company pays for. Without it the bot can't answer anything. |
| 2 | **Google Cloud project with billing**, and someone with these roles: Cloud Run Admin, Service Account Admin + User, Storage Admin, Secret Manager Admin, Artifact Registry Admin, Cloud Build Editor, Cloud Scheduler Admin. | Cloud Run runs the app, Storage keeps the docs and data, Secret Manager keeps the keys, Cloud Scheduler starts the daily and weekly jobs. Creating them needs these permissions. |
| 3 | **gcloud CLI** installed and signed in (`gcloud auth login`) on the machine that deploys. | All deploy steps are `gcloud` commands. |
| 4 | **OAuth consent screen: user type "External", publishing status "In production".** | Partners sign in with their own Google accounts. With "Internal" only company accounts can sign in; with "Testing" only listed test users can. |

## 🟡 2. Needed for specific features

| # | What | Needed for | Why |
|---|---|---|---|
| 5 | **GitHub token approved by the org** (fine-grained, read-only "Contents"). | Repo docs (Sources) | On a laptop the bot can use the person's own git login. A server has no such login, so it needs a token. Until an org admin approves it, the token only sees public repos. |
| 6 | **Gmail sign-in file**: enable the Gmail API, run `python scripts/connect_gmail.py`. | Gap emails, weekly insights email | The emails are sent from a real Gmail account. The file lets the server send mail (send-only, it can't read mail). |
| 7 | **Google Chat sign-in file** (`chat_export/token.json`) and **`private_names.json`**. | Google Chat docs (Sources) | Reading a Chat space needs the sign-in of a member of that space. The names file says who asks and who answers, so questions and answers are matched correctly. |
| 8 | **Team admins' email addresses**. | Teams, gap emails | Each team admin manages their team's docs and gets their team's gap emails. Added on `/admin` → Teams after deploying. |

## 🟢 3. Google Cloud setup (in this order)

| # | Step | Why |
|---|---|---|
| 9 | Turn on the services: Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Storage, Cloud Scheduler. | Google Cloud services are off until turned on. |
| 10 | Create a **service account** for the app. | The app runs as this account and can only reach its own bucket and secrets. No key files are created or shared. |
| 11 | Create a **private bucket** and upload `knowledge/` (including `knowledge/internal/`), `data/teams` and `data/sources`. | The server's own disk is wiped on every restart. Docs, users' chats, teams, sources, gaps, stats and the answer cache must live in the bucket. Uploading teams and sources keeps what was already set up on the laptop. |
| 12 | Store the **secrets** in Secret Manager: Claude API key, admin session secret, internal terms list, cron secret, GitHub token, Gmail file, Chat file, names file. | Keys and private files must never be in the code or the image (the repo is public). The app reads them from Secret Manager at start. |
| 13 | Fill in **`deploy/env.yaml`** (copy of `deploy/env.example.yaml`): bucket name, Google client ID, super admin emails, first teams, app URL. | These are the app's settings (not secret). The file is git-ignored. |
| 14 | **Deploy** with exactly one instance that stays on, with CPU always on and 2 GB memory. | Repo builds, gap drafts and the weekly report run in the background after a request ends; they need a running instance with CPU. One instance because rate limits, blocks and the job queue are kept in memory (see Known limits). |
| 15 | Add the app's URL to the OAuth client's **Authorized JavaScript origins**, set it as `PUBLIC_URL`, deploy again. | Google sign-in only works on listed addresses. `PUBLIC_URL` makes the links in emails point to the app. |
| 16 | Create a **Cloud Scheduler** job that calls `/api/cron/tick` every 15 minutes (with the cron secret). | It wakes the app to run whatever is due: repo and Chat refreshes, daily gap drafts (3 pm), the weekly insights report (Monday). Each one keeps its own schedule set on `/admin`. |

## ✅ 4. After deploying, check

| # | Check | Expected |
|---|---|---|
| 17 | `<url>/healthz` and the logs | `{"status":"ok"}`, and the logs show `provider=anthropic` |
| 18 | Sign in on the chat page with a company account, then with a Gmail account | Company account: "Internal" and the team picker. Gmail: "Partner", straight to the chat |
| 19 | `/admin` with a super admin, then with a team admin | Super admin sees everything; team admin sees only their team |
| 20 | Run one source, "Draft now" for one gap list, send the Gmail test email | Each finishes without errors |

## ⚠️ 5. Known limits of this first deploy

- **Keep one instance.** Rate limits, blocks and the job queue are in memory. More instances need
  Redis first.
- **`logs/` is cleared on restart.** Everything important (chats, stats, insights, gaps, cache)
  is in the bucket.
- **Repo docs need the approved GitHub token** in the cloud (item 5). Workaround below.

## 💡 6. Workaround while the GitHub token isn't approved

Build repo (and Chat) docs **on a laptop** with the person's own git login, but point that laptop
at the **same bucket**: in its `.env` set `KNOWLEDGE_BACKEND=gcs`, `DATA_BACKEND=gcs`,
`KNOWLEDGE_BUCKET=<the bucket>`, and sign in with `gcloud auth application-default login`.
Docs built on the laptop show up in the deployed bot within a minute. No token needed.

## 7. Who provides what

| Item | From |
|---|---|
| Claude API key | Whoever owns the company Anthropic account |
| Google Cloud project, roles, gcloud access | DevOps / platform team |
| OAuth consent screen and client | Owner of the Google Cloud project |
| GitHub token approval | GitHub org owner |
| Gmail and Chat sign-in files, names file, internal terms | The project owner (from their laptop) |
| Team admin emails | Each team lead |
