"""Interaction insights: every question, answer and feedback saved as JSON, analysed weekly.

1. record() saves one event per interaction in analytics/events/ (data store), secrets
   scrubbed: question, answer (shortened), audience, team, conversation and turn, latency,
   tokens, whether it came from the answer cache, gap / off-topic / spam flags, feedback.
   Users appear only as a short hash, never by email.
2. On the insights schedule (default weekly, Monday 10:00 in SCHEDULE_TIMEZONE) a job:
       counts the numbers in code (questions per team and day, cache hits, feedback, gaps);
       sends the events to the AI in batches for notes, then writes one report (themes, top
       questions, where users struggle, FAQ candidates, suggestions);
       saves the report (analytics/reports/), emails it to super admins, and deletes the raw
       events, so the next period starts fresh (ANALYTICS_KEEP_EXPORTS keeps copies).
Admins can download the raw JSON collected so far at any time from the admin page.
"""

import json
import logging
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone

from bot import config, sources
from bot.blobs import data, read_json, write_json
from bot.engine import complete
from bot.guardrails import scrub_secrets
from bot.mailer import MailError, send

log = logging.getLogger(__name__)

STATE = "analytics/state.json"
DEFAULT_SCHEDULE = {"type": "weekly", "days": [0], "time": config.ANALYTICS_DEFAULT_TIME}
ANSWER_CHARS = 1500
_lock = threading.RLock()


def record(kind: str, **fields) -> None:
    """Save one event ("chat", "feedback" or "spam"). Never raises: insights must not break chat."""
    if not config.ANALYTICS_ENABLED:
        return
    try:
        now = datetime.now(timezone.utc)
        ev = {"id": f"{now:%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:6]}", "ts": now.isoformat(), "kind": kind}
        for k, v in fields.items():
            if isinstance(v, str) and k in ("question", "answer", "comment"):
                v = scrub_secrets(v)[: ANSWER_CHARS if k == "answer" else 4000]
            ev[k] = v
        write_json(f"analytics/events/{ev['id']}.json", ev)
    except Exception:
        log.exception("Could not record insights event")


def events() -> list[dict]:
    out = [read_json(p) for p in data().list("analytics/events") if p.endswith(".json")]
    return sorted((e for e in out if e), key=lambda e: e["ts"])


# ---------------------------------------------------------------- schedule state

def state() -> dict:
    with _lock:
        st = read_json(STATE)
        if st is None:
            st = {"schedule": dict(DEFAULT_SCHEDULE), "status": "new",
                  "next_run": sources.iso(sources.next_run(DEFAULT_SCHEDULE, sources.now()))}
            write_json(STATE, st)
        return st


def update_state(**changes) -> dict:
    with _lock:
        st = state()
        st.update(changes)
        write_json(STATE, st)
        return st


def due(at: datetime) -> bool:
    st = state()
    return bool(config.ANALYTICS_ENABLED and st.get("next_run") and sources.parse_iso(st["next_run"]) <= at
                and st.get("status") not in ("queued", "running"))


def reports() -> list[dict]:
    out = [read_json(p) for p in data().list("analytics/reports") if p.endswith(".json")]
    return sorted((r for r in out if r), key=lambda r: r["created"], reverse=True)


def get_report(rid: str) -> dict | None:
    return read_json(f"analytics/reports/{rid}.json") if rid.replace("-", "").replace("T", "").isalnum() else None


# ---------------------------------------------------------------- analysis

NOTES_SYSTEM = """You analyse how people use an integration support assistant (a chatbot that answers questions about Thyrocare's partner APIs and, for internal teams, about their code and team chats). You get a batch of interaction records as JSON lines.

Write compact notes (bullets) on: the topics and APIs asked about (with rough counts), repeated or near-identical questions, questions the assistant could not answer (gap = true) or answered badly (feedback down), confusion patterns (what users misunderstand), unusual or off-topic use, and anything a product owner should know. Quote short example questions. No introduction."""

REPORT_SYSTEM = """You write the weekly insights report for the team that runs an integration support assistant. You get exact numbers (computed by code) and notes written from all interaction records of the period.

Write Markdown with these sections:
## Summary (3-5 bullets: the most important findings)
## What users asked about (topics and APIs, most asked first, with counts where the notes give them)
## Where users struggled (unanswered questions, 👎 answers, confusion; per team when relevant)
## Questions to add to the docs or FAQ (concrete, asked more than once)
## Usage and quality (use the numbers: volume, partners vs internal, teams, cache hits, feedback, off-topic and spam)
## Suggestions (what to improve in the docs, the assistant or the integration)

Use the numbers exactly as given; don't invent figures. Short and concrete. No names or emails."""


def _numbers(evs: list[dict]) -> dict:
    chats = [e for e in evs if e["kind"] == "chat"]
    fb = [e for e in evs if e["kind"] == "feedback"]
    return {
        "period": {"from": evs[0]["ts"][:16] if evs else None, "to": evs[-1]["ts"][:16] if evs else None},
        "questions": len(chats),
        "users": len({e.get("user") for e in chats}),
        "conversations": len({e.get("conversation") for e in chats if e.get("conversation")}),
        "by_audience": dict(Counter(e.get("audience") or "-" for e in chats)),
        "by_team": dict(Counter(e.get("team") or "(all teams / partner)" for e in chats)),
        "by_day": dict(sorted(Counter(e["ts"][:10] for e in chats).items())),
        "answered_from_cache": sum(1 for e in chats if e.get("cached")),
        "gaps": sum(1 for e in chats if e.get("gap")),
        "off_topic": sum(1 for e in chats if e.get("off_topic")),
        "spam_or_repeats": sum(1 for e in evs if e["kind"] == "spam"),
        "feedback_up": sum(1 for e in fb if e.get("rating") == "up"),
        "feedback_down": sum(1 for e in fb if e.get("rating") == "down"),
        "avg_latency_s": round(sum(e.get("latency_s") or 0 for e in chats) / len(chats), 1) if chats else None,
    }


def _compact(e: dict) -> str:
    keep = {k: e.get(k) for k in ("ts", "kind", "audience", "team", "turn", "question", "answer", "gap",
                                  "off_topic", "cached", "rating", "comment", "reason") if e.get(k) not in (None, "", False)}
    return json.dumps(keep, ensure_ascii=False)


async def run(by: str, progress=lambda msg: None) -> tuple[str, str]:
    evs = events()
    if not evs:
        return "no_changes", "No interactions recorded since the last report"
    numbers = _numbers(evs)
    lines = [_compact(e) for e in evs]
    batches, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > config.ANALYTICS_BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    batches.append(cur)
    notes = []
    for n, b in enumerate(batches, 1):
        progress(f"Analysing interactions: part {n} of {len(batches)}")
        out = await complete(NOTES_SYSTEM, "\n".join(b), max_tokens=4000, timeout=config.DOC_AI_TIMEOUT_SECONDS)
        if not out:
            return "error", "The AI call failed or timed out (details in the server log); records kept, try again"
        notes.append(out)
    progress("Writing the report")
    report = await complete(REPORT_SYSTEM, f"Numbers:\n{json.dumps(numbers, indent=1)}\n\nNotes:\n\n"
                            + "\n\n---\n\n".join(notes), max_tokens=6000, timeout=config.DOC_AI_TIMEOUT_SECONDS)
    if not report:
        return "error", "The AI call failed or timed out (details in the server log); records kept, try again"
    rid = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    rec = {"id": rid, "created": sources.iso(sources.now()), "by": by, "numbers": numbers, "markdown": report.strip(),
           "events": len(evs)}
    write_json(f"analytics/reports/{rid}.json", rec)
    if config.ANALYTICS_KEEP_EXPORTS > 0:
        write_json(f"analytics/exports/{rid}.json", evs)
        for old in sorted(data().list("analytics/exports"))[:-config.ANALYTICS_KEEP_EXPORTS]:
            data().delete(old)
    for e in evs:                                   # flush: the next period starts fresh
        data().delete(f"analytics/events/{e['id']}.json")
    message = f"Report for {len(evs)} records ({numbers['questions']} questions); raw records cleared"
    try:
        link = f"{config.PUBLIC_URL}/admin#insights"
        send(sorted(config.ADMIN_EMAILS), f"[{config.BOT_NAME}] Weekly insights: {numbers['questions']} questions",
             f"{report.strip()}\n\nOpen in the admin page: {link}")
        message += "; emailed super admins"
    except MailError as e:
        message += f"; email not sent: {e}"
    return "ok", message
