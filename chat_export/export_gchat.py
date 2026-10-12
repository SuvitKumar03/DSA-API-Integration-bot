"""Export every message from a Google Chat space (group/room) to local files.

Uses the official Google Chat API with *your own* OAuth login, so it can read
any space you are a member of. Nothing is scraped from the browser.

Outputs (in chat_export/output/<space_id>/):
  messages.jsonl   one normalized message per line (source of truth, resumable)
  raw.jsonl        untouched API payloads, in case you need fields later
  threads.json     messages grouped by thread -> natural Q&A candidates
  chunks/*.txt     plain-text transcripts in N-day windows, ready to feed Claude

Usage:
  python chat_export/export_gchat.py --list-spaces
  python chat_export/export_gchat.py --space spaces/AAAAxxxx
  python chat_export/export_gchat.py --space spaces/AAAAxxxx --since 2025-01-01 --chunk-days 4
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERE = Path(__file__).resolve().parent
CREDENTIALS_FILE = HERE / "credentials.json"
TOKEN_FILE = HERE / "token.json"
OUTPUT_ROOT = HERE / "output"

SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    # Chat API returns senders as users/<id> without names under user auth;
    # the People API resolves those ids to names/emails within your domain.
    "https://www.googleapis.com/auth/directory.readonly",
]


def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not CREDENTIALS_FILE.exists():
            sys.exit(f"Missing {CREDENTIALS_FILE}. Download the OAuth Desktop client JSON "
                     "from Google Cloud Console and save it there (see README).")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def with_retry(request, attempts=6):
    """Execute a googleapiclient request, backing off on 429/5xx."""
    for i in range(attempts):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in (429, 500, 502, 503, 504) and i < attempts - 1:
                wait = 2 ** i
                print(f"  HTTP {e.resp.status}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise


def list_spaces(chat):
    page_token = None
    print(f"{'SPACE ID':<25} {'TYPE':<15} NAME")
    while True:
        resp = with_retry(chat.spaces().list(pageSize=1000, pageToken=page_token))
        for s in resp.get("spaces", []):
            print(f"{s['name']:<25} {s.get('spaceType', ''):<15} {s.get('displayName', '(direct message)')}")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


class UserResolver:
    """Maps users/<id> -> display name via the People API, cached on disk."""

    def __init__(self, people, cache_file):
        self.people = people
        self.cache_file = cache_file
        self.cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}

    def resolve(self, user_name, fallback=None):
        if not user_name:
            return fallback or "unknown"
        if user_name in self.cache:
            return self.cache[user_name]
        name = fallback or user_name
        if self.people and user_name.startswith("users/"):
            try:
                person = with_retry(self.people.people().get(
                    resourceName="people/" + user_name.split("/", 1)[1],
                    personFields="names,emailAddresses",
                ))
                names = person.get("names") or []
                emails = person.get("emailAddresses") or []
                display = names[0]["displayName"] if names else None
                email = emails[0]["value"] if emails else None
                if display or email:
                    name = f"{display or ''} <{email}>" if email else display
            except HttpError:
                pass  # external client accounts often aren't resolvable; keep the id
        self.cache[user_name] = name
        return name

    def save(self):
        self.cache_file.write_text(json.dumps(self.cache, indent=2))


def normalize(msg, resolver):
    sender = msg.get("sender", {})
    thread = msg.get("thread", {}).get("name")
    return {
        "id": msg["name"],
        "create_time": msg.get("createTime"),
        "sender_id": sender.get("name"),
        "sender": resolver.resolve(sender.get("name"), sender.get("displayName")),
        "sender_type": sender.get("type"),  # HUMAN or BOT
        "thread": thread,
        "is_reply": bool(msg.get("threadReply")),
        "text": msg.get("text") or msg.get("argumentText") or "",
        "quoted_message": (msg.get("quotedMessageMetadata") or {}).get("name"),
        "attachments": [a.get("contentName") for a in msg.get("attachment", [])],
        "deleted": "deletionMetadata" in msg,
    }


def export_messages(chat, resolver, space, out_dir, since=None):
    messages_file = out_dir / "messages.jsonl"
    raw_file = out_dir / "raw.jsonl"

    # Resume: continue after the newest message already saved.
    last_time = None
    if messages_file.exists():
        with messages_file.open() as f:
            for line in f:
                last_time = json.loads(line)["create_time"]
    start = last_time or (since and f"{since}T00:00:00Z")
    query_filter = f'createTime > "{start}"' if start else None
    if last_time:
        print(f"Resuming after {last_time}")

    count = 0
    page_token = None
    with messages_file.open("a") as mf, raw_file.open("a") as rf:
        while True:
            resp = with_retry(chat.spaces().messages().list(
                parent=space,
                pageSize=1000,
                pageToken=page_token,
                filter=query_filter,
                orderBy="createTime ASC",
                showDeleted=False,
            ))
            for msg in resp.get("messages", []):
                rf.write(json.dumps(msg) + "\n")
                mf.write(json.dumps(normalize(msg, resolver), ensure_ascii=False) + "\n")
                count += 1
            mf.flush()
            rf.flush()
            resolver.save()
            print(f"  fetched {count} messages so far...")
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    print(f"Fetched {count} new messages -> {messages_file}")


def load_messages(out_dir):
    with (out_dir / "messages.jsonl").open() as f:
        return [json.loads(line) for line in f]


def build_threads(messages, out_dir):
    threads = defaultdict(list)
    for m in messages:
        threads[m["thread"] or m["id"]].append(m)
    result = [
        {
            "thread": tid,
            "started": msgs[0]["create_time"],
            "starter": msgs[0]["sender"],
            "message_count": len(msgs),
            "messages": [{k: m[k] for k in ("create_time", "sender", "text", "attachments")} for m in msgs],
        }
        for tid, msgs in threads.items()
    ]
    result.sort(key=lambda t: t["started"])
    (out_dir / "threads.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Wrote {len(result)} threads -> {out_dir / 'threads.json'}")


def write_chunks(messages, out_dir, chunk_days):
    chunk_dir = out_dir / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    for old in chunk_dir.glob("*.txt"):
        old.unlink()
    if not messages:
        return

    def ts(m):
        return datetime.fromisoformat(m["create_time"].replace("Z", "+00:00"))

    first = ts(messages[0]).date()
    buckets = defaultdict(list)
    for m in messages:
        buckets[(ts(m).date() - first).days // chunk_days].append(m)

    for idx, msgs in sorted(buckets.items()):
        start = first + timedelta(days=idx * chunk_days)
        end = start + timedelta(days=chunk_days - 1)
        lines = []
        for m in msgs:
            if not m["text"] and not m["attachments"]:
                continue
            # Short thread tag lets Claude tell parallel conversations apart.
            tag = (m["thread"] or m["id"]).rsplit("/", 1)[-1][:8]
            prefix = "  ↳ " if m["is_reply"] else ""
            att = f" [attachments: {', '.join(filter(None, m['attachments']))}]" if m["attachments"] else ""
            stamp = ts(m).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M")
            lines.append(f"{prefix}[{stamp} IST] [thread:{tag}] {m['sender']}: {m['text']}{att}")
        (chunk_dir / f"{start}_to_{end}.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(buckets)} chunk files ({chunk_days}-day windows) -> {chunk_dir}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list-spaces", action="store_true", help="list spaces you belong to and exit")
    p.add_argument("--space", help="space resource name, e.g. spaces/AAAAxxxx")
    p.add_argument("--since", help="only fetch messages after this date (YYYY-MM-DD)")
    p.add_argument("--chunk-days", type=int, default=4, help="days per transcript chunk (default 4)")
    p.add_argument("--no-resolve-names", action="store_true", help="skip People API name lookup")
    args = p.parse_args()

    creds = get_credentials()
    chat = build("chat", "v1", credentials=creds)

    if args.list_spaces:
        list_spaces(chat)
        return
    if not args.space:
        p.error("--space is required (run --list-spaces to find it)")

    space = args.space if args.space.startswith("spaces/") else f"spaces/{args.space}"
    out_dir = OUTPUT_ROOT / space.split("/", 1)[1]
    out_dir.mkdir(parents=True, exist_ok=True)

    people = None if args.no_resolve_names else build("people", "v1", credentials=creds)
    resolver = UserResolver(people, out_dir / "users_cache.json")

    export_messages(chat, resolver, space, out_dir, since=args.since)
    messages = load_messages(out_dir)
    build_threads(messages, out_dir)
    write_chunks(messages, out_dir, args.chunk_days)


if __name__ == "__main__":
    main()
