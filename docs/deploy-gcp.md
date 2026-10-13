# Deploying on Google Cloud

```
Partners ──> Cloud Run (bot) ──reads──> Cloud Storage bucket: knowledge/*.md
Admins ───> /admin (behind IAP) ──writes─┘        Secret Manager: API key, internal_terms.txt
```

## Pieces

| What | Where |
|---|---|
| The app | Cloud Run service built from the `Dockerfile` |
| Knowledge files | A private Cloud Storage bucket, `KNOWLEDGE_BACKEND=gcs` |
| AI key (`ANTHROPIC_API_KEY` / `LLM_API_KEY`) | Secret Manager, exposed to Cloud Run as an env var |
| `internal_terms.txt` | Secret Manager, mounted as a file; set `INTERNAL_TERMS_FILE` to its path |
| Admin sign-in | "Sign in with Google" on `/admin` (`GOOGLE_CLIENT_ID`, `ADMIN_EMAILS`, `ADMIN_SESSION_SECRET` from Secret Manager). Optionally IAP in front as well (`TRUST_IAP=true`) |
| Admin audit trail | `_audit/` in the same bucket: one object per event (sign-ins, changes with diff and reason) |

## Why the bucket needs no keys

Cloud Run runs as a service account. Give that account **Storage Object Admin on this one
bucket only**. The app then reads and writes the bucket with no key file and no public
access. Partners never touch the bucket; they only talk to the bot.

## How a knowledge update reaches the bot

1. An admin opens `/admin`, pastes the updated section, previews the diff and applies it.
2. The app saves the old file under `_history/`, writes the new one, and bumps `_version.json`.
3. Every running instance checks `_version.json` at most every `KNOWLEDGE_REFRESH_SECONDS`
   (default 60) and reloads the docs on its next question. No redeploy is needed.
4. A bad update is undone with **Restore** on the admin page.

## First-time setup (outline)

```bash
gcloud storage buckets create gs://<bucket> --location=asia-south1 --uniform-bucket-level-access
gcloud storage cp knowledge/*.md gs://<bucket>/knowledge/
gcloud run deploy partner-assistant --source . --region asia-south1 \
  --service-account <sa>@<project>.iam.gserviceaccount.com \
  --set-env-vars KNOWLEDGE_BACKEND=gcs,KNOWLEDGE_BUCKET=<bucket>,PROVIDER=anthropic \
  --set-secrets ANTHROPIC_API_KEY=anthropic-key:latest,/secrets/terms/internal_terms.txt=internal-terms:latest \
  --set-env-vars INTERNAL_TERMS_FILE=/secrets/terms/internal_terms.txt
gcloud storage buckets add-iam-policy-binding gs://<bucket> \
  --member=serviceAccount:<sa>@<project>.iam.gserviceaccount.com --role=roles/storage.objectAdmin
```

## Before going live

- Logs: Cloud Run's disk is temporary. Move `logs/*.jsonl` to the bucket, BigQuery or
  Cloud Logging, or they are lost on every restart.
- Rate limit: kept in memory per instance. With several instances, move it to Redis
  (Memorystore) or set a max of one instance during the pilot.
- Leave `ADMIN_CODES` empty in production; admins use Google sign-in. Add the production
  URL to the OAuth client's Authorized JavaScript origins.
- For a tamper-proof audit trail, turn on Object Versioning or a retention policy on the
  `_audit/` prefix's bucket so audit records can't be deleted.
