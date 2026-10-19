# Deploying on Google Cloud (Cloud Run)

```
Partners ──HTTPS──> Cloud Run: partner-assistant ──reads/writes──> Cloud Storage bucket
Admins ──/admin (Google sign-in)──┘      │                          knowledge/*.md, _history/, _audit/
                                         ├── Secret Manager: API key, partner codes, session secret,
                                         │                   internal_terms.txt (mounted as a file)
                                         └── Claude API (Anthropic) ── answers
```

## Before you start

**Which AI answers in the cloud.** `PROVIDER=claude_code` (your own Claude subscription) only
works on a personal machine; the app refuses it for partners. In the cloud you need one of:

| Option | What's needed |
|---|---|
| **Claude API** (recommended) | An org Anthropic Console account with credits, and an API key |
| Gemini | A Gemini API key (`PROVIDER=openai_compatible`, see `.env.example`) |
| Claude through Vertex AI (billed to Google Cloud) | Not supported by the code yet; a small change |

**Access you need** in the Google Cloud project (or ask the DevOps/platform team to run this):
Cloud Run Admin, Service Account Admin + User, Storage Admin, Secret Manager Admin,
Artifact Registry Admin, Cloud Build Editor, Service Usage Admin. The project needs billing.

**Tools:** the [gcloud CLI](https://cloud.google.com/sdk/docs/install), signed in:
`gcloud auth login`.

**From your machine:** the repo (this branch), `knowledge/*.md` and `internal_terms.txt`.

## Cost, roughly

Cloud Run (scales to zero), the bucket and Secret Manager cost very little at pilot volume.
The main cost is AI usage: every question sends the instructions and docs (cached after the
first question) plus the answer.

---

## Step by step

Set these once in your terminal (change the values):

```bash
PROJECT=your-gcp-project-id
REGION=asia-south1                       # Mumbai
SERVICE=partner-assistant
BUCKET=${PROJECT}-assistant-knowledge
SA=partner-assistant
SA_EMAIL=${SA}@${PROJECT}.iam.gserviceaccount.com

gcloud config set project $PROJECT
```

### 1. Turn on the services

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com cloudscheduler.googleapis.com
```

### 2. Create the app's own identity (service account)

The app runs as this account. It gets access only to its bucket and its secrets, so no key
files are ever created or shared.

```bash
gcloud iam service-accounts create $SA --display-name "Partner assistant"
```

### 3. Create the private bucket and upload the docs

```bash
gcloud storage buckets create gs://$BUCKET --location=$REGION \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage cp knowledge/*.md gs://$BUCKET/knowledge/
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$SA_EMAIL --role=roles/storage.objectAdmin
```

After this, docs are changed through `/admin`, not by uploading files again.

### 4. Store the secrets

```bash
# AI key (paste it when the command waits, then press Ctrl+D)
gcloud secrets create anthropic-api-key --data-file=-

# Signs admin sessions: random, generated here
python3 -c "import secrets;print(secrets.token_urlsafe(48),end='')" | \
  gcloud secrets create admin-session-secret --data-file=-

# Internal names the bot must never reveal
gcloud secrets create internal-terms --data-file=internal_terms.txt

# Lets Cloud Scheduler start the scheduled jobs (random, generated here; keep a copy for step 9)
python3 -c "import secrets;print(secrets.token_urlsafe(32),end='')" | gcloud secrets create cron-secret --data-file=-

# Read-only GitHub token for repo sources (org-approved fine-grained token, Contents: read)
gcloud secrets create github-token --data-file=-

# Google sign-ins made on a laptop: Gmail (gap / insights emails) and Google Chat (Chat sources)
python scripts/connect_gmail.py                     # creates chat_export/gmail_token.json
gcloud secrets create gmail-token --data-file=chat_export/gmail_token.json
gcloud secrets create gchat-token --data-file=chat_export/token.json
gcloud secrets create private-names --data-file=chat_export/private_names.json

for s in anthropic-api-key admin-session-secret internal-terms cron-secret github-token gmail-token gchat-token private-names; do
  gcloud secrets add-iam-policy-binding $s \
    --member=serviceAccount:$SA_EMAIL --role=roles/secretmanager.secretAccessor
done
```

Pick partner codes that are long and random (for example `acme:` followed by the output of
`python3 -c "import secrets;print(secrets.token_urlsafe(12))"`) and send each partner only theirs.

### 5. Fill in the settings file

```bash
cp deploy/env.example.yaml deploy/env.yaml
```

Edit `deploy/env.yaml`: set `KNOWLEDGE_BUCKET` to your bucket name, `GOOGLE_CLIENT_ID`,
`ADMIN_EMAILS`, `DEFAULT_TEAMS` and `PUBLIC_URL` (fill it in after the first deploy, then
deploy again). `deploy/env.yaml` is git-ignored.

### 6. Deploy

```bash
gcloud run deploy $SERVICE --source . --region $REGION \
  --service-account $SA_EMAIL \
  --env-vars-file deploy/env.yaml \
  --set-secrets "ANTHROPIC_API_KEY=anthropic-api-key:latest,ADMIN_SESSION_SECRET=admin-session-secret:latest,CRON_SECRET=cron-secret:latest,GITHUB_TOKEN=github-token:latest,/secrets/terms/internal_terms.txt=internal-terms:latest,/secrets/gmail/gmail_token.json=gmail-token:latest,/secrets/gchat/token.json=gchat-token:latest,/secrets/names/private_names.json=private-names:latest" \
  --allow-unauthenticated \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --memory 2Gi --cpu 1 --timeout 300 --concurrency 40
```

- `--source .` builds the image with Cloud Build from the `Dockerfile`. `.gcloudignore` keeps
  `.env`, keys, private docs, internal terms and logs out of the upload.
- `--allow-unauthenticated` lets partners reach the page; the app's own access codes protect
  it. If your organisation blocks public services, ask the platform team for an exception or
  put it behind a load balancer.
- `--max-instances 1` because rate limits, blocks and the job queue are kept in memory (pilot).
  Raise it after moving them to Redis.
- `--min-instances 1 --no-cpu-throttling`: repo builds, gap drafts and insights run in the
  background after a request returns, so the instance must stay up with CPU (small always-on cost).
- `--memory 2Gi`: repo builds download and read whole repos.
- `--timeout 300` allows long streamed answers.

It prints the service URL, for example `https://partner-assistant-xxxxx-el.a.run.app`.

### 7. Finish Google sign-in

1. Google Cloud console → APIs & Services → Credentials → your OAuth web client → **Authorized
   JavaScript origins**: add the service URL (no trailing slash). Wait a few minutes.
2. **OAuth consent screen:** partners sign in with their own Google accounts, so the user type
   must be **External** and the app **published** (In production). With "Internal" only company
   accounts can sign in; with "Testing" only listed test users can.

### 8. Check it

```bash
URL=$(gcloud run services describe $SERVICE --region $REGION --format 'value(status.url)')
curl $URL/healthz                                   # {"status":"ok"}
gcloud run services logs read $SERVICE --region $REGION --limit 30
```

In the logs, look for `Loaded documentation (... chars), provider=anthropic`. Then open the URL,
sign in with Google (a company account should show "Internal" and the team picker), ask a
question, and open `$URL/admin` and sign in with a super admin account. Then do step 9.

### 9. Schedule the jobs (Cloud Scheduler)

One job that wakes the app every 15 minutes; the app then runs whatever is due (repo and Chat
refreshes, daily gap drafts, the weekly insights report), each on its own admin-set schedule.

```bash
gcloud scheduler jobs create http assistant-tick --location $REGION --schedule "*/15 * * * *" \
  --uri "$URL/api/cron/tick" --http-method POST \
  --headers "X-Cron-Secret=$(gcloud secrets versions access latest --secret cron-secret)"
```

---

## Day-to-day

| Change | How |
|---|---|
| Docs (sections, new files) | `/admin`: preview, apply; live within a minute, no redeploy |
| Code | Pull the latest branch and run the step 6 command again |
| Internal terms | `gcloud secrets versions add internal-terms --data-file=internal_terms.txt`, then step 6 again (secrets are read when an instance starts) |
| Partner codes | `gcloud secrets versions add partner-access-codes --data-file=-`, then step 6 again |
| AI key | `gcloud secrets versions add anthropic-api-key --data-file=-`, then step 6 again |
| Roll back the code | Cloud Run console → the service → Revisions → send traffic to an earlier revision |

## Good to know

- **Logs.** Cloud Run's disk is temporary: `logs/chats.jsonl` and `feedback.jsonl` are lost when
  an instance stops. Server messages (startup, strikes, errors) are kept in Cloud Logging.
  Before relying on chat logs for the learning loop, move them to the bucket or BigQuery.
- **Audit trail and history** are in the bucket (`_audit/`, `_history/`), so they persist. For
  a tamper-proof trail, enable a retention policy or Object Versioning on the bucket.
- **Real client IP.** Keep `TRUSTED_PROXY_HOPS=1` (2 if you add an external load balancer), or
  every user looks like Google's proxy and one block would hit everyone.
- **Scaling past one instance.** Move rate limits and blocks (`bot/abuse.py`) to Redis
  (Memorystore), then raise `--max-instances`.
- **Floods.** Put Cloud Armor in front (needs an external load balancer) to drop abusive traffic
  before it reaches the app.
- **Custom domain** (for example `assistant.thyrocare.com`): Cloud Run domain mapping or a load
  balancer; then add that origin to the OAuth client too.
- **User sign-in and saved chats.** Set `USER_LOGIN=google` and `DATA_BACKEND=gcs` (users' chats
  and memory go to the bucket under `data/`; the instance disk is wiped on restart).
- **Knowledge sources (repos, Google Chat).** Jobs run inside the app, in the background. On Cloud
  Run, deploy with `--no-cpu-throttling` (background work needs CPU after the response) and keep
  one instance (`--min-instances 1 --max-instances 1`). Set `SCHEDULER_ENABLED=false`, store a
  random `CRON_SECRET` in Secret Manager, and create a Cloud Scheduler job that calls
  `POST https://<service>/api/cron/tick` every 15 minutes with header `X-Cron-Secret: <secret>`.
  `GITHUB_TOKEN` goes in Secret Manager. Google Chat sources need the admin's OAuth token and
  `private_names.json`; until those are moved to Secret Manager, run Chat sources locally.
  The AI that writes the docs is the deployed `PROVIDER` (needs an org API key).
- **Cold start.** With `--min-instances 0` the first request after idle takes a few seconds.
  Set `--min-instances 1` to avoid it (small always-on cost).
