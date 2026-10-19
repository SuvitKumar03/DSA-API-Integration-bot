"""Builds and refreshes the knowledge document for one Google Chat space.

The same steps as the manual pipeline in chat_export/, run automatically:
    1. Read the messages with the Google Chat API, using the admin's own Google sign-in
       (GCHAT_TOKEN_FILE, created once with `python chat_export/export_gchat.py --list-spaces`).
    2. Clean and redact them (chat_export/clean_chat.py: tokens, phone numbers, patient data,
       client names) and cut them into windows.
    3. The AI pulls question-and-answer pairs out of each window (chat_export/extraction_prompt.md).
    4. Resolved, reusable pairs are written as a Markdown doc, one section per API area.

Scheduled runs read only the messages since the last run (plus two days before, as context
for late replies) and add the new pairs to the end of their sections; earlier entries and
any manual edits stay as they are.
"""

import asyncio
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from bot import config
from bot.engine import complete
from bot.github_docs import SourceError
from bot.guardrails import scrub_secrets
from bot.sections import append_to_sections

log = logging.getLogger(__name__)

if str(config.ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(config.ROOT_DIR))

EXPORT_DIR = config.ROOT_DIR / "chat_export"
CONTEXT_DAYS = 2
WINDOW_CHARS = 24000
KEEP_TYPES = {"api_howto", "error_debug", "announcement"}
KEEP_STATUS = {"resolved", "workaround"}
MIN_CONFIDENCE = 0.6
API_TITLES = {"auth": "Authentication", "catalog": "Catalog and pricing", "slots": "Slots and serviceability",
              "create_order": "Create order", "lead": "Leads", "order_details": "Order details",
              "cancel_reschedule": "Cancel and reschedule", "report": "Reports", "webhook": "Webhooks",
              "portal": "Partner portal", "other": "Other"}
SPACE_RES = [re.compile(r"(?:#chat/space/|/room/|/space/|spaces/)([A-Za-z0-9_-]{6,})"),
             re.compile(r"^([A-Za-z0-9_-]{6,})$")]
SOURCE_LINE_RE = re.compile(r"^> Source: Google Chat space .*$", re.M)


def parse_space(text: str) -> str:
    """Space id from a Chat link (mail.google.com/chat/...#chat/space/ID, chat.google.com/room/ID) or an id."""
    t = text.strip()
    for r in SPACE_RES:
        m = r.search(t)
        if m:
            return m.group(1)
    raise SourceError(f"Not a Google Chat space link: {t}")


def doc_name(space: str, team: str | None = None) -> str:
    return f"internal/{team + '/' if team else ''}gchat/{space}.md"


def _modules():
    """The chat_export scripts, loaded only when needed (clean_chat needs the private names file)."""
    if not config.PRIVATE_NAMES_FILE.exists():
        raise SourceError(f"{config.PRIVATE_NAMES_FILE} is missing (who asks and who answers in the "
                          "group). Copy private_names.example.json and fill it in.")
    from chat_export import clean_chat, export_gchat
    return export_gchat, clean_chat


def _service():
    export_gchat, _ = _modules()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = config.GCHAT_TOKEN_FILE
    hint = ("Google Chat isn't connected. On the server's computer, run "
            "`python chat_export/export_gchat.py --list-spaces` once and sign in with an account that is a "
            "member of the space.")
    if not path.exists():
        raise SourceError(hint)
    try:
        creds = Credentials.from_authorized_user_file(str(path), export_gchat.SCOPES)
        if not creds.valid:
            creds.refresh(Request())
            try:
                path.write_text(creds.to_json())
            except OSError:
                pass          # read-only secret mount (Cloud Run): the refreshed token lives in memory
    except Exception as e:
        raise SourceError(f"{hint} ({type(e).__name__})")
    return export_gchat, build("chat", "v1", credentials=creds), build("people", "v1", credentials=creds)


def connected() -> bool:
    return config.GCHAT_TOKEN_FILE.exists()


def _fetch(space: str, after: str | None) -> tuple[str, list[dict]]:
    """(space display name, normalised messages after `after`, oldest first)."""
    export_gchat, chat, people = _service()
    name = f"spaces/{space}"
    try:
        info = export_gchat.with_retry(chat.spaces().get(name=name))
    except Exception as e:
        raise SourceError(f"Can't open space {space}: the signed-in account must be a member ({type(e).__name__}).")
    cache = config.DATA_DIR / "gchat" / space / "users_cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    resolver = export_gchat.UserResolver(people, cache)
    out, token = [], None
    while True:
        resp = export_gchat.with_retry(chat.spaces().messages().list(
            parent=name, pageSize=1000, pageToken=token, orderBy="createTime ASC", showDeleted=False,
            filter=f'createTime > "{after}"' if after else None))
        out += [export_gchat.normalize(m, resolver) for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    resolver.save()
    return info.get("displayName") or space, out


def _windows(messages: list[dict], new_after: str | None) -> list[str]:
    """Cleaned transcript windows covering the new messages; older ones only appear as context."""
    _, clean_chat = _modules()
    recs = clean_chat.clean(messages, Counter())
    by_short = {r["id"]: r for r in recs}
    cutoff = datetime.fromisoformat(new_after.replace("Z", "+00:00")) if new_after else None
    first_new = next((i for i, r in enumerate(recs) if not cutoff or clean_chat.ts(r) > cutoff), len(recs))
    texts = []
    for start, end in clean_chat.cut_windows(recs[first_new:], WINDOW_CHARS):
        start, end = start + first_new, end + first_new
        ctx = clean_chat.window_context(recs, start, end, by_short, 12, 4000)
        body = []
        if ctx:
            body.append("## CONTEXT (earlier messages, for understanding only; do not extract Q&A that starts here)")
            body += [clean_chat.render(r, context=True) for r in ctx] + [""]
        body.append("## MESSAGES")
        body += [clean_chat.render(r) for r in recs[start:end]]
        texts.append("\n".join(body))
    return texts


def _parse_entries(text: str) -> list[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        return json.loads(text[start:end + 1]).get("entries", [])
    except (ValueError, AttributeError):
        return []


def _keep(e: dict) -> bool:
    return (e.get("type") in KEEP_TYPES and e.get("status") in KEEP_STATUS
            and (e.get("confidence") or 0) >= MIN_CONFIDENCE and e.get("question") and e.get("answer"))


def _render(entries: list[dict]) -> dict[str, list[str]]:
    by_api = defaultdict(list)
    for e in sorted(entries, key=lambda x: x.get("date") or ""):
        title = API_TITLES.get(e.get("api"), "Other")
        tags = " · ".join(filter(None, [e.get("date"), e.get("environment"), e.get("type", "").replace("_", " "),
                                        "one partner's setup" if e.get("partner_specific") else ""]))
        by_api[title] += [f"**{scrub_secrets(e['question']).strip()}**",
                          scrub_secrets(e["answer"]).strip(), f"_{tags}_", ""]
    for lines in by_api.values():
        while lines and not lines[-1]:
            lines.pop()
    return dict(by_api)


def _source_line(space: str, name: str, last_time: str | None) -> str:
    return (f"> Source: Google Chat space \"{name}\" (`{space}`). Questions and answers extracted by the knowledge "
            f"bot; last message read {(last_time or '-')[:16].replace('T', ' ')} UTC.")


async def run(space: str, current_doc: str | None, last_time: str | None, progress=lambda msg: None) -> dict:
    """Full build when current_doc is None, else a delta. Returns {"text" (None = no change),
    "last_time", "summary", "name"}."""
    full = current_doc is None or not last_time
    since = None
    if not full:
        since = (datetime.fromisoformat(last_time.replace("Z", "+00:00")) - timedelta(days=CONTEXT_DAYS)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    progress("Reading messages from Google Chat")
    name, messages = await asyncio.to_thread(_fetch, space, since)
    new = [m for m in messages if full or m["create_time"] > last_time]
    newest = max((m["create_time"] for m in messages), default=last_time)
    if not new:
        return {"text": None, "last_time": last_time, "summary": "No new messages", "name": name}
    windows = await asyncio.to_thread(_windows, messages, None if full else last_time)
    system = (EXPORT_DIR / "extraction_prompt.md").read_text(encoding="utf-8")
    entries = []
    for n, w in enumerate(windows, 1):
        progress(f"Extracting questions and answers: window {n} of {len(windows)}")
        out = await complete(system, f"Window id: {n:03d}\n\n{w}", max_tokens=8000,
                             timeout=config.DOC_AI_TIMEOUT_SECONDS)
        if out is None:
            raise SourceError("The AI call failed or timed out (details in the server log). Try Run again later.")
        entries += [e for e in _parse_entries(out) if _keep(e)]
    summary = f"{len(new)} new message(s), {len(entries)} new Q&A entr{'y' if len(entries) == 1 else 'ies'}"
    added = _render(entries)
    if full:
        intro = (f"# Google Chat: {name}\n\n{_source_line(space, name, newest)}\n\n"
                 f"Questions raised in the \"{name}\" Google Chat space and the team's final answers, "
                 "grouped by API area. Generated from chat history: check the date of an entry, as rules may "
                 "have changed since.\n")
        text = append_to_sections(intro, added) if added else intro + "\n_No resolved questions found yet._\n"
    else:
        text = SOURCE_LINE_RE.sub(lambda _: _source_line(space, name, newest), current_doc, count=1)
        text = append_to_sections(text, added) if added else text
    return {"text": text, "last_time": newest, "summary": summary, "name": name}
