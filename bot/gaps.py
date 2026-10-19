"""Documentation gaps: questions the docs couldn't answer, turned into drafts admins can add.

1. During the day, a gap is recorded when the AI ends an answer with the hidden marker
   [[GAP: <what is missing>]] (stripped before the user sees it), or a user gives 👎 with a
   comment. Each gap goes to a bucket:
       <team slug>   an internal user's question with that team chosen   -> that team's admins
       shared        an internal user's question across all teams        -> super admins
       partner       a partner's question (partner docs)                 -> super admins
2. Once a day per bucket (default GAP_DEFAULT_TIME, 15:00 in SCHEDULE_TIMEZONE) a job:
       groups duplicate gaps (one AI call), skipping ones the admins rejected before;
       researches each group: the team's docs, the repo code (GitHub code search, when the
       token allows) and, for partner gaps, the internal docs as background;
       drafts the missing section with evidence, confidence and open questions (AI). The AI
       must not invent facts: what it can't find becomes an open question for the admin;
       emails the bucket's admins a summary with a link to the admin page.
3. An admin reviews each draft on the admin page (Gaps): edit, preview, Add (saved like any
   admin edit, with history and audit) or Reject (remembered, so it isn't proposed again).

Storage (data store): gaps/items/<bucket>/<id>.json, gaps/proposals/<bucket>/<id>.json,
gaps/state.json (schedule and last run per bucket).
"""

import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import requests

from bot import config, retrieval, sources, teams
from bot.blobs import data, read_json, write_json
from bot.engine import complete
from bot.github_docs import GitHub, SourceError, scrub_code
from bot.guardrails import redact, scrub_secrets
from bot.knowledge_store import store, team_of
from bot.mailer import MailError, send

log = logging.getLogger(__name__)

MARKER = "[[GAP:"
STATE = "gaps/state.json"
PARTNER, SHARED = "partner", "shared"
EVIDENCE_DOC_CHARS = 24000
EVIDENCE_CODE_FILES = 3
EVIDENCE_FILE_CHARS = 12000
KEEP_DONE_DAYS = 60

_lock = threading.RLock()


class GapCatcher:
    """Removes the hidden [[GAP: ...]] marker from a streamed answer and remembers its text."""

    def __init__(self):
        self.buf, self.topic = "", None

    def feed(self, text: str) -> str:
        self.buf += text
        out = ""
        while True:
            i = self.buf.find(MARKER)
            if i < 0:
                break
            j = self.buf.find("]]", i)
            if j < 0:                                   # marker not complete yet: hold it back
                out += self.buf[:i]
                self.buf = self.buf[i:]
                return out
            self.topic = self.buf[i + len(MARKER):j].strip()[:200] or "unspecified"
            self.buf = self.buf[:i].rstrip(" \n") + self.buf[j + 2:]
        # Hold back a possible start of the marker at the end of the buffer.
        for k in range(min(len(MARKER) - 1, len(self.buf)), 0, -1):
            if MARKER.startswith(self.buf[-k:]):
                out += self.buf[:-k]
                self.buf = self.buf[-k:]
                return out
        out += self.buf
        self.buf = ""
        return out

    def flush(self) -> str:
        rest, self.buf = self.buf, ""
        if rest.startswith(MARKER):                      # cut off before "]]"
            self.topic = rest[len(MARKER):].strip()[:200] or "unspecified"
            return ""
        return rest


# ---------------------------------------------------------------- buckets and recording

def bucket_for(internal: bool, team: str | None) -> str:
    return (team or SHARED) if internal else PARTNER


def buckets() -> list[str]:
    return [t["slug"] for t in teams.all_teams()] + [SHARED, PARTNER]


def bucket_name(bucket: str) -> str:
    return {SHARED: "All teams (shared docs)", PARTNER: "Partner docs"}.get(bucket) or teams.name_of(bucket)


def recipients(bucket: str) -> list[str]:
    team = teams.get(bucket)
    return (team["admins"] if team and team["admins"] else sorted(config.ADMIN_EMAILS))


def record(bucket: str, topic: str, question: str, answer: str, *, source: str, conversation: str | None,
           message_id: str | None, user_uid: str, comment: str = "") -> dict:
    gap = {"id": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}",
           "ts": sources.iso(sources.now()), "bucket": bucket, "topic": scrub_secrets(topic)[:200],
           "question": scrub_secrets(question)[:3000], "answer": scrub_secrets(answer)[:2000],
           "comment": scrub_secrets(comment)[:1000], "source": source, "conversation": conversation,
           "message_id": message_id, "user": user_uid, "status": "new"}
    write_json(f"gaps/items/{bucket}/{gap['id']}.json", gap)
    return gap


def items(bucket: str, status: str | None = None) -> list[dict]:
    out = [read_json(p) for p in data().list(f"gaps/items/{bucket}") if p.endswith(".json")]
    return sorted((g for g in out if g and (status is None or g["status"] == status)), key=lambda g: g["ts"])


def _set_gap(gap: dict, **changes) -> None:
    gap.update(changes)
    write_json(f"gaps/items/{gap['bucket']}/{gap['id']}.json", gap)


def proposals(bucket: str, status: str | None = None) -> list[dict]:
    out = [read_json(p) for p in data().list(f"gaps/proposals/{bucket}") if p.endswith(".json")]
    return sorted((x for x in out if x and (status is None or x["status"] == status)),
                  key=lambda x: x["created"], reverse=True)


def get_proposal(bucket: str, pid: str) -> dict | None:
    return read_json(f"gaps/proposals/{bucket}/{pid}.json") if re.fullmatch(r"[\w-]{6,80}", pid) else None


def save_proposal(p: dict) -> None:
    write_json(f"gaps/proposals/{p['bucket']}/{p['id']}.json", p)


def close_proposal(p: dict, status: str, by: str, reason: str = "", version: str | None = None) -> None:
    """Accept or reject a draft; its gaps become "fixed" or "dismissed"."""
    p.update(status=status, closed_by=by, closed_at=sources.iso(sources.now()), reason=reason, version=version)
    save_proposal(p)
    for gid in p["gap_ids"]:
        g = read_json(f"gaps/items/{p['bucket']}/{gid}.json")
        if g:
            _set_gap(g, status="fixed" if status == "accepted" else "dismissed")


# ---------------------------------------------------------------- schedule state

DEFAULT_SCHEDULE = {"type": "weekly", "days": list(range(7)), "time": config.GAP_DEFAULT_TIME}


def state() -> dict:
    """Schedule and last run per bucket (missing buckets get the daily default)."""
    with _lock:
        st = read_json(STATE, {}) or {}
        changed = False
        for b in buckets():
            if b not in st:
                st[b] = {"schedule": dict(DEFAULT_SCHEDULE), "status": "new",
                         "next_run": sources.iso(sources.next_run(DEFAULT_SCHEDULE, sources.now()))}
                changed = True
        if changed:
            write_json(STATE, st)
        return st


def update_state(bucket: str, **changes) -> dict:
    with _lock:
        st = state()
        st.setdefault(bucket, {}).update(changes)
        write_json(STATE, st)
        return st[bucket]


def due(at: datetime) -> list[str]:
    return [b for b, s in state().items() if b in buckets() and s.get("next_run")
            and sources.parse_iso(s["next_run"]) <= at and s.get("status") not in ("queued", "running")]


# ---------------------------------------------------------------- research

GROUP_SYSTEM = """You sort questions that a documentation assistant could not answer. Group questions that ask about the same missing information. For each group give a short topic and 1-3 search terms to look for in source code (identifiers, endpoint paths, error codes or exact phrases; not generic words).

Skip a group (give skip_reason) when it is not a documentation gap (chit-chat, a request about one specific order or account, an outage report) or when it matches a topic the admins already rejected (listed below).

Return only JSON: {"groups": [{"topic": "...", "gap_ids": ["..."], "search_terms": ["..."], "skip_reason": null}]}"""

DRAFT_SYSTEM = """You fill a gap in a knowledge base that an assistant answers questions from. Users asked the questions below and the knowledge base had no answer.

Write the missing section, using ONLY facts found in the evidence (knowledge base excerpts and source code) that the knowledge base doesn't already state. Never invent endpoints, fields, values, limits, behaviour or reasons. If the evidence answers only part of it, write that part and list the rest as open questions for the admin. If the evidence adds no new fact, leave markdown empty and only list the open questions. Never write sentences saying something is not documented or unknown: that goes in open_questions.
{audience_rule}
Choose where it belongs: target_file must be one of the allowed files listed below. Prefer an existing file whose topic matches; otherwise use the default file. The markdown must be one section starting with a "## " heading. If you reuse the heading of an existing section in that file, your text replaces that whole section, so then include everything that section already says plus the addition; otherwise use a new heading.

If the current knowledge base in fact already answers the questions, set skip to true and say where in skip_reason.

Return only JSON:
{{"skip": false, "skip_reason": "", "summary": "one line: what this adds", "target_file": "...", "markdown": "## ...", "evidence": ["short reference: file/doc + what it shows"], "confidence": "high|medium|low", "open_questions": ["..."]}}"""

PARTNER_RULE = """This section goes into the PARTNER documentation that external partners read: describe only the public API contract (endpoints, fields, values, errors, what the partner must do). Never mention internal systems, services, databases, hosts, code, class or function names, internal headers or internal reasons, even if the evidence shows them; describe the effect in partner terms."""
INTERNAL_RULE = """This section is for internal team members: internal names, files and classes are fine. Never include secrets (passwords, tokens, keys, connection strings)."""


def _json(text: str | None) -> dict:
    if not text:
        return {}
    a, b = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[a:b + 1]) if a >= 0 and b > a else {}
    except ValueError:
        return {}


def _bucket_files(bucket: str) -> tuple[list[str], str]:
    """(files a draft for this bucket may go into, the default file)."""
    s = store()
    if bucket == PARTNER:
        names = s.names()
        default = next((n for n in names if "faq" in n.lower()), "05-faq-additions.md")
        return names, default
    # Internal drafts go into notes files only: repo and Chat docs are rewritten by their sources.
    team = None if bucket == SHARED else bucket
    prefix = f"internal/{team + '/' if team else ''}notes/"
    names = [n for n in s.internal_names() if n.startswith(prefix) and team_of(n) == team]
    return names, f"{prefix}answers.md"


def _headings(name: str) -> str:
    text = store().read(name) or ""
    heads = [l.strip() for l in text.splitlines() if l.startswith("## ")][:40]
    return f"- {name}: " + (" | ".join(h[3:] for h in heads) if heads else "(no sections yet)")


def _code_evidence(bucket: str, terms: list[str]) -> tuple[str, str]:
    """(code excerpts from GitHub code search, note on why there are none)."""
    from bot.github_docs import auth_mode

    if not terms:
        return "", ""
    if auth_mode() == "token" and not config.GITHUB_TOKEN:
        return "", "code search off (no GitHub token)"
    team = None if bucket in (SHARED, PARTNER) else bucket
    repos = sorted({f"{s['github']['owner']}/{s['github']['repo']}" for s in sources.all_sources()
                    if s["kind"] == "github" and (team is None or s.get("team") == team)})
    if not repos:
        return "", "no GitHub repos are sources for this team"
    if auth_mode() == "git":
        return _local_code_evidence(repos, terms)
    scope = " ".join(f"repo:{r}" for r in repos) if len(repos) <= 5 else \
        " ".join(f"org:{o}" for o in sorted({r.split('/')[0] for r in repos}))
    try:
        gh = GitHub()
        seen, parts = set(), []
        for term in terms[:3]:
            q = f'"{term}" {scope}' if " " in term else f"{term} {scope}"
            r = gh.s.get(f"{config.GITHUB_API}/search/code", params={"q": q, "per_page": 5}, timeout=60)
            if r.status_code in (403, 429):
                return "\n".join(parts), "GitHub code search limit reached; try again later"
            if r.status_code == 422:
                continue
            gh._check(r, "Code search")
            for item in r.json().get("items", []):
                key = (item["repository"]["full_name"], item["path"])
                if key in seen or len(seen) >= EVIDENCE_CODE_FILES:
                    continue
                seen.add(key)
                f = gh.s.get(item["url"], headers={"Accept": "application/vnd.github.raw"}, timeout=60)
                if not f.ok:
                    continue
                text = f.text
                pos = max(0, text.find(term) - EVIDENCE_FILE_CHARS // 3) if term in text else 0
                parts.append(f"### CODE: {key[0]}/{key[1]}\n```\n{scrub_code(text[pos:pos + EVIDENCE_FILE_CHARS])}\n```")
        return "\n\n".join(parts), "" if parts else "code search found nothing"
    except (SourceError, requests.RequestException) as e:
        return "", f"code search unavailable: {str(e)[:150]}"


def _local_code_evidence(repos: list[str], terms: list[str]) -> tuple[str, str]:
    """Code search in the local clones (GITHUB_AUTH=git): git grep in each repo's checked-out copy."""
    from bot.git_local import GitLocal, clone_dir

    g, parts, seen = GitLocal(), [], set()
    for term in terms[:3]:
        for full in repos:
            owner, repo = full.split("/", 1)
            for path in g.grep(owner, repo, term):
                if (full, path) in seen or len(seen) >= EVIDENCE_CODE_FILES:
                    continue
                seen.add((full, path))
                try:
                    text = (clone_dir(owner, repo) / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                at = text.lower().find(term.lower())
                pos = max(0, at - EVIDENCE_FILE_CHARS // 3) if at >= 0 else 0
                parts.append(f"### CODE: {full}/{path}\n```\n{scrub_code(text[pos:pos + EVIDENCE_FILE_CHARS])}\n```")
    return "\n\n".join(parts), "" if parts else "code search found nothing (local clones)"


async def _draft(bucket: str, group: dict, gaps: list[dict]) -> dict:
    questions = "\n".join(f"- {g['question'][:600]}" + (f" (user's note: {g['comment'][:200]})" if g["comment"] else "")
                          for g in gaps)
    query = " ".join([group.get("topic", "")] + group.get("search_terms", []) + [g["question"] for g in gaps][:5])
    team = None if bucket in (SHARED, PARTNER) else bucket
    if bucket == PARTNER:
        docs = retrieval.search(query, EVIDENCE_DOC_CHARS // 2, scope="partner") + \
            retrieval.search(query, EVIDENCE_DOC_CHARS // 2, team=None)
    else:
        docs = retrieval.search(query, EVIDENCE_DOC_CHARS, team=team)
    doc_text = "\n\n".join(f'<excerpt doc="{h["doc"]}" section="{h["section"]}">\n{h["text"]}\n</excerpt>' for h in docs)
    code_text, code_note = await asyncio.to_thread(_code_evidence, bucket, group.get("search_terms", []))
    files, default = _bucket_files(bucket)
    allowed = "\n".join(_headings(n) for n in files) + f"\n- {default} (default{'' if default in files else ', new file'})"
    prompt = (f"Questions:\n{questions}\n\nAllowed files and their sections:\n{allowed}\n\n"
              f"Knowledge base excerpts:\n{doc_text or '(none found)'}\n\n"
              f"Source code:\n{code_text or '(none: ' + (code_note or 'nothing found') + ')'}")
    system = DRAFT_SYSTEM.format(audience_rule=PARTNER_RULE if bucket == PARTNER else INTERNAL_RULE)
    out = _json(await complete(system, prompt, max_tokens=6000, timeout=config.DOC_AI_TIMEOUT_SECONDS))
    if not out:
        raise SourceError("The AI's draft wasn't readable JSON")
    target = out.get("target_file") if out.get("target_file") in files + [default] else default
    md = scrub_code((out.get("markdown") or "").strip())
    if md and not md.startswith("## "):
        md = f"## {group.get('topic', 'Answer')}\n\n{md}"
    hits = sorted(set(redact(md)[1])) if bucket == PARTNER else []
    return {"skip": bool(out.get("skip")), "skip_reason": str(out.get("skip_reason", ""))[:300],
            "summary": str(out.get("summary", group.get("topic", "")))[:300], "target_file": target,
            "markdown": md, "evidence": [str(e)[:300] for e in out.get("evidence", [])][:12],
            "confidence": out.get("confidence") if out.get("confidence") in ("high", "medium", "low") else "low",
            "open_questions": [str(q)[:400] for q in out.get("open_questions", [])][:8],
            "code_note": code_note, "internal_terms": hits}


def _email(bucket: str, made: list[dict], skipped: int, new_gaps: int) -> str:
    name = bucket_name(bucket)
    link = f"{config.PUBLIC_URL}/admin#gaps={bucket}"
    lines = [f"{config.BOT_NAME}: documentation gaps for {name}", "",
             f"{new_gaps} question(s) could not be answered from the docs. {len(made)} draft(s) are ready for review"
             + (f"; {skipped} were set aside (not documentation gaps, already covered, or rejected before)." if skipped else "."), ""]
    for p in made:
        lines += [f"- {p['summary']}  [{p['confidence']} confidence, asked {len(p['gap_ids'])}x]",
                  f"  Goes into: {p['target_file']}"]
        for q in p["open_questions"][:3]:
            lines.append(f"  Needs your answer: {q}")
    lines += ["", f"Review, edit and add them here: {link}", "Nothing is added until an admin clicks Add."]
    text = "\n".join(lines)
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    items = "".join(
        f"<li><b>{esc(p['summary'])}</b> <span style='color:#64748b'>({p['confidence']} confidence, asked "
        f"{len(p['gap_ids'])}x, into <code>{esc(p['target_file'])}</code>)</span>"
        + "".join(f"<br><span style='color:#9a6700'>Needs your answer: {esc(q)}</span>" for q in p["open_questions"][:3])
        + "</li>" for p in made)
    html = (f"<p>{new_gaps} question(s) could not be answered from the <b>{esc(name)}</b> docs. "
            f"{len(made)} draft(s) are ready for review.</p><ul>{items}</ul>"
            f"<p><a href='{link}'>Review, edit and add them</a>. Nothing is added until an admin clicks Add.</p>")
    send(recipients(bucket), f"[{name}] {len(made)} documentation gap draft(s) to review", text, html)
    return f"emailed {', '.join(recipients(bucket))}"


def _prune(bucket: str) -> None:
    cutoff = sources.iso(sources.now() - timedelta(days=KEEP_DONE_DAYS))
    for g in items(bucket):
        if g["status"] in ("fixed", "dismissed") and g["ts"] < cutoff:
            data().delete(f"gaps/items/{bucket}/{g['id']}.json")


async def run_bucket(bucket: str, by: str, progress=lambda msg: None) -> tuple[str, str]:
    """The daily job for one bucket. Returns (outcome, message)."""
    new = items(bucket, "new")
    if not new:
        return "no_changes", "No new gaps"
    rejected = [f"{p['summary']} (reason: {p.get('reason') or '-'})" for p in proposals(bucket, "rejected")][:30]
    progress(f"Grouping {len(new)} gap(s)")
    listing = "\n".join(f"{g['id']}: [{g['topic']}] {g['question'][:400]}" for g in new)
    grouped = _json(await complete(GROUP_SYSTEM, f"Questions:\n{listing}\n\nRejected before:\n"
                                   + ("\n".join(rejected) or "(none)"), max_tokens=4000,
                                   timeout=config.DOC_AI_TIMEOUT_SECONDS)).get("groups")
    by_id = {g["id"]: g for g in new}
    if not grouped:                                        # AI failed: one group per gap
        grouped = [{"topic": g["topic"], "gap_ids": [g["id"]], "search_terms": [], "skip_reason": None} for g in new]
    made, skipped, drafted = [], 0, 0
    for group in grouped:
        gaps = [by_id[i] for i in group.get("gap_ids", []) if i in by_id and by_id[i]["status"] == "new"]
        if not gaps:
            continue
        if group.get("skip_reason"):
            for g in gaps:
                _set_gap(g, status="dismissed", reason=str(group["skip_reason"])[:300])
            skipped += 1
            continue
        if drafted >= config.GAP_MAX_DRAFTS:
            continue                                       # left "new" for the next run
        drafted += 1
        progress(f"Researching and drafting {drafted}: {group.get('topic', '')[:60]}")
        try:
            d = await _draft(bucket, group, gaps)
        except SourceError as e:
            log.warning("Gap draft failed for %s: %s", bucket, e)
            continue
        if d["skip"]:
            for g in gaps:
                _set_gap(g, status="dismissed", reason=d["skip_reason"] or "already covered")
            skipped += 1
            continue
        p = {"id": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}", "bucket": bucket,
             "created": sources.iso(sources.now()), "status": "pending", "topic": group.get("topic", ""),
             "gap_ids": [g["id"] for g in gaps], "questions": [g["question"] for g in gaps][:10], **d}
        save_proposal(p)
        for g in gaps:
            _set_gap(g, status="drafted", proposal=p["id"])
        made.append(p)
    _prune(bucket)
    message = f"{len(new)} gap(s): {len(made)} draft(s), {skipped} set aside"
    if made:
        try:
            message += "; " + await asyncio.to_thread(_email, bucket, made, skipped, len(new))
        except MailError as e:
            message += f"; email not sent: {e}"
    return ("ok" if made or skipped else "error"), message
