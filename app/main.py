"""FastAPI app: serves the chat UI and the chat API."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot import admin_auth, analytics, answer_cache, config, gaps, stats, gchat_docs, github_docs, jobs, mailer, retrieval, sources, teams, users
from bot.abuse import CANNED, Limited, guard
from bot.engine import CUT_SHORT_TEXT, ERROR_TEXT, INSTALL_HINT, REFUSAL_TEXT, claude_code_bin, complete, stream_answer
from bot.guardrails import StreamRedactor, probe_flags, redact, scrub_secrets
from bot.knowledge_store import current_version, is_internal_name, store, team_of, valid_name
from bot.prompt import OFF_TOPIC_MARKER, SUMMARY_INSTRUCTIONS, load_documentation
from bot.sections import apply_update, unified_diff
from bot.users import User

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"
config.LOG_DIR.mkdir(parents=True, exist_ok=True)
CHAT_LOG = config.LOG_DIR / "chats.jsonl"
FEEDBACK_LOG = config.LOG_DIR / "feedback.jsonl"

app = FastAPI(title=config.BOT_NAME, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=config.MAX_MESSAGE_CHARS * 4)


class ChatRequest(BaseModel):
    # With conversation_id, the server uses the saved conversation and only the last message
    # here (the new question). Without it, `messages` is the whole conversation.
    messages: list[Message] = Field(min_length=1)
    conversation_id: str | None = Field(default=None, max_length=64)
    # Internal users: the team chosen on the chat page (None = all teams). A saved conversation
    # keeps the team it was started in.
    team: str | None = Field(default=None, max_length=40)
    # Unused since memory moved to the user's account; accepted so older pages keep working.
    summary: str = Field(default="", max_length=config.SUMMARY_MAX_CHARS * 2)


class SummaryRequest(BaseModel):
    messages: list[Message] = Field(default_factory=list, max_length=60)
    conversation_id: str | None = Field(default=None, max_length=64)
    # Index of the first message of the conversation not yet folded into the memory.
    since: int = Field(default=0, ge=0)


class GoogleLogin(BaseModel):
    credential: str = Field(min_length=10, max_length=5000)


class Feedback(BaseModel):
    message_id: str
    # With the conversation, a 👎 also becomes a documentation gap (bot/gaps.py).
    conversation_id: str | None = Field(default=None, max_length=64)
    rating: Literal["up", "down"]
    comment: str = Field(default="", max_length=2000)


def _partner_for(code: str | None) -> str:
    """Return the partner name for an access code, or raise 401."""
    if not config.ACCESS_CODES:
        return "local"
    if code and code in config.ACCESS_CODES:
        return config.ACCESS_CODES[code]
    raise HTTPException(status_code=401, detail="Invalid access code")


def _client_ip(request: Request) -> str:
    """The caller's IP. Behind proxies, take the entry the nearest trusted proxy added."""
    hops = config.TRUSTED_PROXY_HOPS
    fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if hops and len(fwd) >= hops:
        return fwd[-hops]
    return request.client.host if request.client else ""


def _subjects(request: Request, user: User | None = None) -> list[str]:
    email = user.email if user and user.method == "google" else None
    return guard.subjects(_client_ip(request), request.headers.get("x-client-id"), email)


USER_COOKIE = "user_session"


def _user_label(user: User) -> str:
    """How a user appears on the admin dashboard."""
    return user.email if user.method == "google" else f"partner code: {user.name}" if user.method == "code" else "local user"


def _is_local(request: Request) -> bool:
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1", "testclient")


def _user(request: Request, x_access_code: str | None = None) -> User:
    """Who is asking: the signed-in Google user, a partner access code, or the local user."""
    if config.USER_LOGIN == "google":
        data = admin_auth.read_session_data(request.cookies.get(USER_COOKIE), "user")
        if not data:
            raise HTTPException(status_code=401, detail="Please sign in with Google")
        email = data["email"]
        # Re-checked on every request, so removing a domain from INTERNAL_DOMAINS takes effect at once.
        internal = bool(data.get("internal")) and email.rsplit("@", 1)[-1] in config.INTERNAL_DOMAINS
        return User(email=email, name=data.get("name", ""), is_internal=internal, method="google")
    if config.ACCESS_CODES:
        partner = _partner_for(x_access_code)
        return User(email=f"{partner}@partner.code", name=partner, is_internal=False, method="code")
    return User(email="local@localhost", name="Local user",
                is_internal=config.LOCAL_USER_INTERNAL and _is_local(request), method="local")


@app.exception_handler(Limited)
async def _limited(_: Request, exc: Limited) -> JSONResponse:
    return JSONResponse({"detail": exc.message}, status_code=exc.status,
                        headers={"Retry-After": str(max(1, exc.retry_after))})


def _local_only(request: Request) -> None:
    """The claude_code provider runs on the developer's own subscription: localhost only."""
    if config.PROVIDER == "claude_code" and request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="This test setup only answers on the local machine.")


def _clean_summary(text: str) -> str:
    text, _ = redact(scrub_secrets(text or ""))
    return text.strip()[: config.SUMMARY_MAX_CHARS]


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


@app.on_event("startup")
def _warm_up() -> None:
    if config.PROVIDER == "claude_code" and config.ACCESS_CODES:
        raise RuntimeError("PROVIDER=claude_code is for personal local testing only and cannot be used "
                           "with partner ACCESS_CODES. Use PROVIDER=anthropic with an API key.")
    if config.PROVIDER == "claude_code":
        binary = claude_code_bin()
        if not binary:
            raise RuntimeError(INSTALL_HINT)
        log.info("Answers come from your own Claude subscription via Claude Code (%s, model %s)",
                 binary, config.CLAUDE_CODE_MODEL)
    if config.PROVIDER == "anthropic" and not config.ANTHROPIC_API_KEY:
        raise RuntimeError("PROVIDER=anthropic needs ANTHROPIC_API_KEY in .env (console.anthropic.com > API keys). "
                           "Until you have it, use PROVIDER=claude_code.")
    if config.USER_LOGIN == "google" and not config.GOOGLE_CLIENT_ID:
        raise RuntimeError("USER_LOGIN=google needs GOOGLE_CLIENT_ID (OAuth web client ID).")
    production = bool(config.ACCESS_CODES) or config.USER_LOGIN == "google"
    if production and not config.INTERNAL_TERMS_FILE.exists():
        raise RuntimeError(f"Internal terms file {config.INTERNAL_TERMS_FILE} is missing. Partner-facing "
                           "deployments must filter internal names; see internal_terms.example.txt.")
    # Fail fast if the knowledge base is missing.
    docs = load_documentation()
    # Internal names still left in the docs after sanitising: the docs need cleaning.
    _, hits = redact(docs)
    if hits:
        log.warning("Knowledge files still contain %d internal term(s) the model can see: %s",
                    len(hits), sorted(set(hits))[:20])
    log.info("Loaded documentation (%d chars), provider=%s, model=%s", len(docs), config.PROVIDER, config.MODEL)


@app.on_event("startup")
async def _start_jobs() -> None:
    jobs.start()


@app.on_event("shutdown")
async def _stop_jobs() -> None:
    await jobs.stop()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/dashboard")
def dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
def ui_config() -> dict:
    login = "google" if config.USER_LOGIN == "google" else "code" if config.ACCESS_CODES else "none"
    return {
        "botName": config.BOT_NAME,
        "login": login,
        "googleClientId": config.GOOGLE_CLIENT_ID if login == "google" else "",
        "requiresCode": login == "code",
        "maxChars": config.MAX_MESSAGE_CHARS,
        "supportContact": config.SUPPORT_CONTACT,
        "summaryIdleMinutes": config.SUMMARY_IDLE_MINUTES,
        "summaryEveryMessages": config.SUMMARY_EVERY_MESSAGES,
        "summaryTtlDays": config.SUMMARY_TTL_DAYS,
    }


@app.post("/api/login")
def login(x_access_code: str | None = Header(default=None)) -> dict:
    return {"partner": _partner_for(x_access_code)}


# ---------------------------------------------------------------- users: sign-in, history, memory

@app.post("/api/user/google-login")
def user_google_login(body: GoogleLogin, request: Request) -> JSONResponse:
    if config.USER_LOGIN != "google":
        raise HTTPException(status_code=404, detail="Google sign-in is not enabled")
    guard.check_blocked(_subjects(request))
    guard.check_rate(f"login|{_client_ip(request)}", 10, 60)
    try:
        claims = admin_auth.verify_google_credential(body.credential)
    except ValueError:
        raise HTTPException(status_code=401, detail="Google sign-in could not be verified")
    email = claims["email"].lower()
    user = User(email=email, name=claims.get("name", ""), is_internal=users.is_internal_account(claims))
    guard.check_blocked(_subjects(request, user))
    users.touch_profile(user, claims.get("picture", ""))
    log.info("User sign-in: %s (%s)", email, user.role)
    res = JSONResponse({"email": email, "name": user.name, "picture": claims.get("picture", ""), "role": user.role})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    res.set_cookie(USER_COOKIE, admin_auth.make_session(email, "user", config.USER_SESSION_DAYS * 24,
                                                        internal=user.is_internal, name=user.name[:80]),
                   max_age=config.USER_SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")
    return res


@app.post("/api/user/logout")
def user_logout() -> JSONResponse:
    res = JSONResponse({"ok": True})
    res.delete_cookie(USER_COOKIE, path="/")
    return res


@app.get("/api/me")
def me(request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    user = _user(request, x_access_code)
    memory = users.get_memory(user)
    team_list = [{k: t[k] for k in ("slug", "name", "description")} for t in teams.all_teams()] \
        if user.is_internal else []
    return {"email": user.email if user.method == "google" else "", "name": user.name, "role": user.role,
            "method": user.method, "memory": memory["summary"] if memory else "", "teams": team_list}


@app.get("/api/conversations")
def conversations(request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    return {"conversations": users.list_conversations(_user(request, x_access_code))}


@app.get("/api/conversations/{cid}")
def conversation(cid: str, request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    conv = users.get_conversation(_user(request, x_access_code), cid)
    if not conv:
        raise HTTPException(status_code=404, detail="No such conversation")
    return conv


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    return {"ok": users.delete_conversation(_user(request, x_access_code), cid)}


@app.delete("/api/memory")
def forget_memory(request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    users.forget_memory(_user(request, x_access_code))
    return {"ok": True}


class _NoRedaction:
    """Internal users may see internal names, so their answers are not filtered."""
    hits: list[str] = []

    def feed(self, text: str) -> str:
        return text

    def flush(self) -> str:
        return ""


def _strike_note(strikes: int, blocked_until: float | None) -> str:
    """Text added to an answer after a strike: a warning, or the notice that access is paused."""
    if blocked_until:
        return (f"\n\n_Access is now paused for {config.BLOCK_HOURS:g} hours because of repeated unrelated "
                f"or spam messages._")
    if strikes >= config.WARN_AFTER_STRIKES:
        left = config.STRIKES_TO_BLOCK - strikes
        return (f"\n\n_Please keep questions to the Thyrocare partner API integration. After {left} more "
                f"unrelated message{'s' if left != 1 else ''}, access will be paused for {config.BLOCK_HOURS:g} hours._")
    return ""


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request, x_access_code: str | None = Header(default=None)):
    _local_only(request)
    user = _user(request, x_access_code)
    partner = user.name if user.method == "code" else user.email
    subjects = _subjects(request, user)
    guard.check_blocked(subjects)
    client_key = f"{partner}|{subjects[0]}"
    guard.check_rate(client_key, config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_PER_HOUR)

    cid = req.conversation_id if users.valid_conversation_id(req.conversation_id) else None
    if req.conversation_id and not cid:
        raise HTTPException(status_code=400, detail="Bad conversation id")
    sent = [m.model_dump() for m in req.messages]
    # A saved conversation is the source of truth: earlier turns can't be rewritten by the client.
    history = (users.history_for(user, cid) + sent[-1:]) if cid else sent
    history = history[-config.MAX_HISTORY_MESSAGES:]
    while history and history[0]["role"] != "user":
        history = history[1:]
    question = history[-1]
    if question["role"] != "user":
        raise HTTPException(status_code=400, detail="Last message must be from the user")
    if len(question["content"]) > config.MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail="Message is too long")

    message_id = uuid.uuid4().hex
    memory = users.get_memory(user)
    summary = _clean_summary(memory["summary"]) if memory else ""
    audience = user.role
    team = None
    if user.is_internal:
        saved = users.get_conversation(user, cid) if cid else None
        team = saved.get("team") if saved else req.team
        if team and not teams.get(team):
            team = None                    # unknown or removed team: answer across all teams
    flags = probe_flags(question["content"])
    if flags:
        log.warning("Probe-like question from %s %s: %s", partner, subjects, flags)

    def _log(answer: str, **extra) -> None:
        _append_jsonl(CHAT_LOG, {
            "id": message_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "partner": partner,
            "role": user.role,
            "team": team,
            "conversation": cid,
            "client": subjects,
            # Tokens, keys, emails and phone numbers are never written to the server log.
            "question": scrub_secrets(question["content"]),
            "answer": scrub_secrets(answer),
            "turns": len(history),
            "flags": flags,
            "had_summary": bool(summary),
            **extra,
        })

    bucket = gaps.bucket_for(user.is_internal, team)      # for the admin page counts (bot/stats.py)
    who = _user_label(user)

    # Answer cache: a first question asked before (same audience, team, knowledge) needs no AI call.
    first_turn = len(history) == 1
    hit = answer_cache.get(question["content"], audience, team) if first_turn and not {"prompt_extraction", "instruction_override"} & set(flags) else None
    if hit:
        text = hit["answer"]
        if cid:
            users.save_turn(user, cid, question["content"], text, message_id, team=team)
        _log(text, cached=True)
        analytics.record("chat", audience=audience, team=team, user=user.uid, conversation=cid, turn=1,
                         question=question["content"], answer=text, cached=True, latency_s=0)
        stats.bump(bucket, who, asked=1, answered=1, cached=1)
        return PlainTextResponse(text, headers={"X-Message-Id": message_id, "Cache-Control": "no-cache", "X-Answer": "cached"})

    # Cheap spam checks first: these never reach the AI.
    reason = guard.precheck(subjects, question["content"])
    if not reason and {"prompt_extraction", "instruction_override"} & set(flags):
        reason = "manipulation"
    if reason:
        strikes, until = guard.strike(subjects, reason)
        stats.bump(bucket, who, reason=reason, strikes=1, blocks=int(bool(until)))
        log.warning("Strike (%s) for %s: %d in window%s", reason, subjects, strikes, ", BLOCKED" if until else "")
        if until:
            _log("", spam=reason, strikes=strikes, blocked=True)
            guard.check_blocked(subjects)          # raises the "access paused" response
        if reason in CANNED:
            text = CANNED[reason] + _strike_note(strikes, None)
            _log(text, spam=reason, strikes=strikes)
            analytics.record("spam", reason=reason, audience=audience, team=team, user=user.uid,
                             question=question["content"])
            stats.bump(bucket, who, asked=1, spam=1)
            return PlainTextResponse(text, headers={"X-Message-Id": message_id, "Cache-Control": "no-cache"})
        # Manipulation attempts still get an answer: the AI declines politely.

    # Internal users: the internal sections that match the question (none for partners).
    extra = retrieval.context_for(history, team) if user.is_internal else ""
    guard.acquire(client_key)

    async def generate():
        answer_parts: list[str] = []
        usage: dict = {}
        started = time.time()
        # Every partner answer passes the internal-name filter before it reaches the partner.
        redactor = StreamRedactor() if not user.is_internal else _NoRedaction()
        catcher = gaps.GapCatcher()          # removes the hidden [[GAP: ...]] line, keeps its text
        head, decided, off_topic, finished = "", False, False, False
        try:
            async for chunk in stream_answer(history, usage_out=usage, summary=summary,
                                             audience=audience, extra=extra, team=team):
                if not decided:
                    # Hold the first characters until we know whether the AI marked the
                    # question as off-topic; the marker itself is never shown.
                    head += chunk
                    stripped = head.lstrip()
                    if len(stripped) < len(OFF_TOPIC_MARKER) and OFF_TOPIC_MARKER.startswith(stripped):
                        continue
                    decided = True
                    off_topic = stripped.startswith(OFF_TOPIC_MARKER)
                    chunk = stripped[len(OFF_TOPIC_MARKER):].lstrip() if off_topic else head
                out = redactor.feed(catcher.feed(chunk.replace(OFF_TOPIC_MARKER, "")))
                if out:
                    answer_parts.append(out)
                    yield out
            if not decided and head:
                answer_parts.append(redactor.feed(catcher.feed(head.replace(OFF_TOPIC_MARKER, ""))))
                yield answer_parts[-1]
            tail = redactor.feed(catcher.flush()) + redactor.flush()
            if off_topic:
                strikes, until = guard.strike(subjects, "off_topic")
                stats.bump(bucket, who, reason="off_topic", strikes=1, blocks=int(bool(until)))
                log.warning("Strike (off_topic) for %s: %d in window%s", subjects, strikes, ", BLOCKED" if until else "")
                tail += _strike_note(strikes, until)
            elif reason == "manipulation":
                tail += _strike_note(guard.strike_count(subjects), None)
            if tail:
                answer_parts.append(tail)
                yield tail
            finished = True
        finally:
            guard.release(client_key)
            if cid and not off_topic:
                # Saved even when the user pressed Stop (the partial answer is what they saw).
                try:
                    users.save_turn(user, cid, question["content"], "".join(answer_parts), message_id,
                                    stopped=not finished, team=team)
                except Exception:
                    log.exception("Could not save conversation %s", cid)
        if redactor.hits:
            log.warning("Redacted internal terms from answer %s: %s", message_id, sorted(set(redactor.hits)))
        if catcher.topic and not off_topic:
            try:
                gaps.record(gaps.bucket_for(user.is_internal, team), catcher.topic, question["content"],
                            "".join(answer_parts), source="answer", conversation=cid, message_id=message_id,
                            user_uid=user.uid)
            except Exception:
                log.exception("Could not record gap for %s", message_id)
        answer = "".join(answer_parts)
        _log(answer, latency_s=round(time.time() - started, 2), usage=usage,
             redacted=sorted(set(redactor.hits)), spam="off_topic" if off_topic else reason,
             internal_context_chars=len(extra), gap=catcher.topic)
        analytics.record("chat", audience=audience, team=team, user=user.uid, conversation=cid, turn=len(history),
                         question=question["content"], answer=answer, gap=catcher.topic, off_topic=off_topic,
                         latency_s=round(time.time() - started, 2), tokens=usage.get("output_tokens"),
                         message_id=message_id)
        failed = not answer.strip() or any(t in answer for t in (ERROR_TEXT, REFUSAL_TEXT, "Local setup issue"))
        stats.bump(bucket, who, asked=1, gaps=int(bool(catcher.topic) and not off_topic), off_topic=int(off_topic),
                   spam=int(reason == "manipulation"), errors=int(failed),
                   answered=int(not (catcher.topic or off_topic or reason or failed)))
        # Only clean, complete first answers are reused.
        if (first_turn and finished and not off_topic and not catcher.topic and not reason and answer.strip()
                and not any(t in answer for t in (ERROR_TEXT, REFUSAL_TEXT, CUT_SHORT_TEXT.strip(), "Local setup issue"))):
            try:
                answer_cache.put(question["content"], audience, team, answer, message_id)
            except Exception:
                log.exception("Could not save answer to cache")

    return StreamingResponse(
        generate(),
        media_type="text/plain; charset=utf-8",
        headers={"X-Message-Id": message_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/feedback")
def feedback(fb: Feedback, request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    user = _user(request, x_access_code)
    partner = user.name if user.method == "code" else user.email
    subjects = _subjects(request, user)
    guard.check_blocked(subjects)
    guard.check_rate(f"{partner}|{subjects[0]}:feedback", 20, 200)
    _append_jsonl(FEEDBACK_LOG, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "partner": partner,
        "role": user.role,
        **fb.model_dump(),
        "comment": scrub_secrets(fb.comment),
    })
    analytics.record("feedback", user=user.uid, audience=user.role, message_id=fb.message_id, rating=fb.rating,
                     comment=fb.comment, conversation=fb.conversation_id)
    fb_conv = users.get_conversation(user, fb.conversation_id) if fb.conversation_id else None
    stats.bump(gaps.bucket_for(user.is_internal, (fb_conv or {}).get("team") if user.is_internal else None),
               _user_label(user), **{fb.rating: 1})
    conv = users.get_conversation(user, fb.conversation_id) if fb.rating == "down" and fb.conversation_id else None
    msgs = (conv or {}).get("messages", [])
    at = next((i for i, m in enumerate(msgs) if m.get("id") == fb.message_id), None)
    if at:                                   # the 👎 answer and the question before it
        team = conv.get("team") if user.is_internal else None
        answer_cache.drop(msgs[at - 1]["content"], user.role, team)       # don't serve that answer again
        gaps.record(gaps.bucket_for(user.is_internal, team), fb.comment or "answer marked not helpful",
                    msgs[at - 1]["content"], msgs[at]["content"], source="feedback", conversation=fb.conversation_id,
                    message_id=fb.message_id, user_uid=user.uid, comment=fb.comment)
    return {"ok": True}


@app.post("/api/summarize")
async def summarize(req: SummaryRequest, request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    """Fold the newer messages of a conversation into the user's saved session memory."""
    _local_only(request)
    user = _user(request, x_access_code)
    subjects = _subjects(request, user)
    guard.check_blocked(subjects)
    guard.check_rate(f"{user.email}|{subjects[0]}:summary", 4, 30)
    if req.conversation_id:
        saved = users.history_for(user, req.conversation_id)
        messages, up_to = saved[req.since:][-60:], len(saved)
    else:
        messages = [m.model_dump() for m in req.messages]
        up_to = req.since + len(messages)
    if len(messages) < 2:
        return {"summary": (users.get_memory(user) or {}).get("summary", ""), "upTo": req.since}
    previous = (users.get_memory(user) or {}).get("summary", "")
    transcript = "\n\n".join(f"[{m['role'].upper()}]\n{m['content'][:3000]}" for m in messages)
    prompt = (f"Previous memory:\n{_clean_summary(previous) or '(none)'}\n\n"
              f"New conversation:\n{scrub_secrets(transcript)}")
    text = await complete(SUMMARY_INSTRUCTIONS, prompt)
    if not text:
        raise HTTPException(status_code=503, detail="Could not update the summary right now.")
    summary = _clean_summary(text)
    users.set_memory(user, summary)
    return {"summary": summary, "upTo": up_to}


# ---------------------------------------------------------------- admin: knowledge updates
# Users only chat. Admins change the knowledge: super admins (ADMIN_EMAILS) everything, team
# admins (bot/teams.py) only their team's internal docs and sources. Admins sign in with Google;
# see bot/admin_auth.py. Every admin action is written to the audit trail in the knowledge store
# (_audit/), next to the files it describes.

MAX_KNOWLEDGE_CHARS = 400_000
MAX_AUDIT_DIFF_CHARS = 20_000


def _admin_role(email: str | None, method: str) -> dict | None:
    """{"admin", "method", "super", "teams"} if this email is a super or team admin."""
    if not email:
        return None
    if teams.is_super_email(email):
        return {"admin": email, "method": method, "super": True, "teams": [t["slug"] for t in teams.all_teams()]}
    mine = teams.admin_teams(email)
    return {"admin": email, "method": method, "super": False, "teams": mine} if mine else None


def _admin(request: Request) -> dict:
    """The signed-in admin as {"admin", "method", "super", "teams"}, or 401/404."""
    if not admin_auth.enabled():
        raise HTTPException(status_code=404, detail="Admin is not enabled on this server")
    who = _admin_role(admin_auth.read_session(request.cookies.get(admin_auth.SESSION_COOKIE)), "google") \
        or _admin_role(admin_auth.iap_email(request.headers.get("x-goog-authenticated-user-email")), "iap")
    if who:
        return who
    code = request.headers.get("x-admin-code")
    if code and code in config.ADMIN_CODES:
        return {"admin": config.ADMIN_CODES[code], "method": "code", "super": True,
                "teams": [t["slug"] for t in teams.all_teams()]}
    raise HTTPException(status_code=401, detail="Please sign in as an admin")


def _super(request: Request) -> dict:
    who = _admin(request)
    if not who["super"]:
        raise HTTPException(status_code=403, detail="Only a super admin can do this")
    return who


def _can_edit(who: dict, name: str) -> bool:
    """Super admins: every file. Team admins: only internal files in their team's folder."""
    return who["super"] or (is_internal_name(name) and team_of(name) in who["teams"])


def _check_file(who: dict, name: str) -> None:
    if not _can_edit(who, name):
        mine = ", ".join(f"internal/{t}/notes/..." for t in who["teams"])
        raise HTTPException(status_code=403, detail=f"You can only change your team's files ({mine})")


def _audit(request: Request, event: str, who: dict | None = None, **details) -> None:
    if "file" in details and "team" not in details:
        details["team"] = team_of(details["file"] or "")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "admin": (who or {}).get("admin"),
        "method": (who or {}).get("method"),
        "ip": _client_ip(request),
        "user_agent": request.headers.get("user-agent", "")[:200],
        **details,
    }
    try:
        store().append_audit(record)
    except Exception:
        # The change itself succeeded; never lose the record silently.
        log.exception("Could not write audit record: %s", record)


def _record_last_modified(who: dict, file: str, event: str, **details) -> None:
    """Keep only the latest change per file (shown on the admin page as "Last modified")."""
    try:
        store().set_last_modified(file, {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
                                         "admin": who["admin"], "method": who["method"], **details})
    except Exception:
        log.exception("Could not record last modification of %s", file)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class KnowledgeChange(BaseModel):
    file: str
    text: str = Field(min_length=1, max_length=MAX_KNOWLEDGE_CHARS)
    # "section": text holds one or more sections that replace (or add to) the file's sections.
    # "replace": text is the whole new file. "new": create a new file.
    mode: Literal["section", "replace", "new"] = "section"
    # Version the admin previewed against; applying fails if someone changed it since.
    base_version: str | None = None
    # Optional reason shown in the audit trail.
    note: str = Field(default="", max_length=500)


class Restore(BaseModel):
    file: str
    stamp: str
    note: str = Field(default="", max_length=500)


@app.get("/api/admin/auth-config")
def admin_auth_config() -> dict:
    return {
        "enabled": admin_auth.enabled(),
        "googleClientId": config.GOOGLE_CLIENT_ID if config.ADMIN_EMAILS else "",
        "codesEnabled": bool(config.ADMIN_CODES),
    }


@app.post("/api/admin/google-login")
def admin_google_login(body: GoogleLogin, request: Request) -> JSONResponse:
    if not config.ADMIN_EMAILS:
        raise HTTPException(status_code=404, detail="Google admin sign-in is not enabled")
    try:
        claims = admin_auth.verify_google_credential(body.credential)
    except ValueError as e:
        _audit(request, "login_failed", reason=str(e)[:200])
        raise HTTPException(status_code=401, detail="Google sign-in could not be verified")
    email = claims["email"].lower()
    who = _admin_role(email, "google")
    if not who:
        _audit(request, "login_denied", {"admin": email, "method": "google"})
        raise HTTPException(status_code=403, detail=f"{email} is not an admin")
    _audit(request, "login", who)
    res = JSONResponse({"admin": email, "name": claims.get("name", ""), "picture": claims.get("picture", "")})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    res.set_cookie(admin_auth.SESSION_COOKIE, admin_auth.make_session(email), max_age=admin_auth.SESSION_HOURS * 3600,
                   httponly=True, samesite="strict", secure=secure, path="/api/admin")
    return res


@app.post("/api/admin/logout")
def admin_logout(request: Request) -> JSONResponse:
    try:
        _audit(request, "logout", _admin(request))
    except HTTPException:
        pass
    res = JSONResponse({"ok": True})
    res.delete_cookie(admin_auth.SESSION_COOKIE, path="/api/admin")
    return res


@app.get("/api/admin/me")
def admin_me(request: Request) -> dict:
    who = _admin(request)
    return {**who, "team_names": {t["slug"]: t["name"] for t in teams.all_teams()}}


def _plan(change: KnowledgeChange) -> tuple[str, str, list[dict]]:
    """Work out the old text, the new text and what changes, without saving anything."""
    if not valid_name(change.file):
        raise HTTPException(status_code=400, detail="File name must look like 05-topic.md "
                                                    "(or internal/notes/topic.md for staff-only knowledge)")
    old = store().read(change.file)
    if change.mode == "new":
        if old is not None:
            raise HTTPException(status_code=409, detail="That file already exists")
        return "", change.text.strip() + "\n", [{"section": "(whole file)", "action": "created"}]
    if old is None:
        raise HTTPException(status_code=404, detail="No such knowledge file")
    if change.mode == "replace":
        return old, change.text.strip() + "\n", [{"section": "(whole file)", "action": "replaced"}]
    try:
        new, actions = apply_update(old, change.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return old, new, actions


@app.get("/api/admin/files")
def admin_files(request: Request) -> dict:
    who = _admin(request)
    s = store()
    last = s.last_modified()
    return {"version": current_version(force=True),
            "files": [{"name": n, "chars": len(s.read(n) or ""), "staff_only": is_internal_name(n), "team": team_of(n),
                       "last": {k: v for k, v in last[n].items() if k != "diff"} if n in last else None}
                      for n in s.names() + s.internal_names() if _can_edit(who, n)]}


@app.get("/api/admin/files/{name:path}")
def admin_file(name: str, request: Request) -> dict:
    _check_file(_admin(request), name)
    text = store().read(name) if valid_name(name) else None
    if text is None:
        raise HTTPException(status_code=404, detail="No such knowledge file")
    return {"name": name, "text": text, "history": store().history(name),
            "last_modified": store().last_modified().get(name)}


@app.post("/api/admin/preview")
def admin_preview(change: KnowledgeChange, request: Request) -> dict:
    _check_file(_admin(request), change.file)
    old, new, actions = _plan(change)
    # Internal files are never shown to partners, so internal names there are expected.
    _, hits = ([], []) if is_internal_name(change.file) else redact(change.text)
    return {
        "version": current_version(force=True),
        "actions": actions,
        "diff": unified_diff(old, new, change.file),
        "chars_before": len(old), "chars_after": len(new),
        # Internal names in the update: allowed (the filter hides them from partners), but
        # shown so the admin can rewrite them in partner terms.
        "internal_terms": sorted(set(hits)),
    }


@app.post("/api/admin/apply")
def admin_apply(change: KnowledgeChange, request: Request) -> dict:
    who = _admin(request)
    _check_file(who, change.file)
    return _apply_change(change, request, who)


def _apply_change(change: KnowledgeChange, request: Request, who: dict, **audit_extra) -> dict:
    if change.base_version and change.base_version != current_version(force=True):
        raise HTTPException(status_code=409, detail="The knowledge changed since your preview. Preview again.")
    old, new, actions = _plan(change)
    version, saved_as = store().write(change.file, new, by=who["admin"])
    current_version(force=True)          # this instance reloads now; others within the refresh interval
    diff = unified_diff(old, new, change.file)
    _audit(request, "apply", who, file=change.file, mode=change.mode, note=change.note, actions=actions,
           version=version, previous_saved_as=saved_as, sha_before=_sha(old) if old else None, sha_after=_sha(new),
           chars_before=len(old), chars_after=len(new), diff=diff[:MAX_AUDIT_DIFF_CHARS],
           diff_truncated=len(diff) > MAX_AUDIT_DIFF_CHARS, **audit_extra)
    _record_last_modified(who, change.file, "apply", mode=change.mode, note=change.note, actions=actions,
                          version=version, diff=diff[:MAX_AUDIT_DIFF_CHARS])
    log.info("Knowledge %s updated by %s (%s): %s", change.file, who["admin"], change.mode, actions)
    return {"ok": True, "version": version, "actions": actions}


@app.post("/api/admin/restore")
def admin_restore(req: Restore, request: Request) -> dict:
    who = _admin(request)
    _check_file(who, req.file)
    text = store().read_history(req.file, req.stamp) if valid_name(req.file) else None
    if text is None:
        raise HTTPException(status_code=404, detail="No such saved version")
    old = store().read(req.file) or ""
    version, saved_as = store().write(req.file, text, by=who["admin"])
    current_version(force=True)
    diff = unified_diff(old, text, req.file)
    _audit(request, "restore", who, file=req.file, note=req.note, restored=req.stamp, version=version,
           previous_saved_as=saved_as, sha_before=_sha(old), sha_after=_sha(text),
           chars_before=len(old), chars_after=len(text), diff=diff[:MAX_AUDIT_DIFF_CHARS],
           diff_truncated=len(diff) > MAX_AUDIT_DIFF_CHARS)
    _record_last_modified(who, req.file, "restore", note=req.note, restored=req.stamp, version=version,
                          diff=diff[:MAX_AUDIT_DIFF_CHARS])
    return {"ok": True, "version": version}


@app.get("/api/admin/audit")
def admin_audit(request: Request, limit: int = 100) -> dict:
    who = _admin(request)
    events = store().read_audit(max(1, min(limit, 500)) if who["super"] else 2000)
    if not who["super"]:                 # team admins: only their teams' files and sources
        events = [e for e in events if e.get("team") in who["teams"]][:limit]
    return {"events": events}


# ---------------------------------------------------------------- admin: blocked users

MANUAL_BUCKET = "_manual"       # blocks set by hand on the admin page (super admins' dashboard)


class BlockChange(BaseModel):
    subject: str = Field(pattern=r"^(ip|cid|user):[\w.:@+-]{1,120}$")
    note: str = Field(default="", max_length=500)


@app.get("/api/admin/blocks")
def admin_blocks(request: Request) -> dict:
    _super(request)
    return {
        "blocks": guard.list_blocks(),
        "events": list(guard.events)[:100],
        "settings": {"strikes_to_block": config.STRIKES_TO_BLOCK, "window_minutes": config.STRIKE_WINDOW_MINUTES,
                     "block_hours": config.BLOCK_HOURS, "block_by_ip": config.BLOCK_BY_IP,
                     "per_minute": config.RATE_LIMIT_PER_MINUTE, "per_hour": config.RATE_LIMIT_PER_HOUR},
    }


@app.post("/api/admin/unblock")
def admin_unblock(req: BlockChange, request: Request) -> dict:
    who = _super(request)
    found = guard.unblock(req.subject)
    _audit(request, "unblock", who, subject=req.subject, note=req.note, found=found)
    return {"ok": found}


@app.post("/api/admin/block")
def admin_block(req: BlockChange, request: Request) -> dict:
    who = _super(request)
    until = guard.block([req.subject], f"manual: {req.note or 'by admin'}", by=who["admin"])
    stats.bump(MANUAL_BUCKET, req.subject.split(":", 1)[1] if req.subject.startswith("user:") else req.subject,
               reason="manual", blocks=1)
    _audit(request, "block", who, subject=req.subject, note=req.note, until=until)
    return {"ok": True, "until": until}


# ---------------------------------------------------------------- admin: knowledge sources
# GitHub repos and Google Chat spaces that build internal-only docs, each on its own schedule.
# See bot/sources.py (registry, schedules), bot/jobs.py (queue, scheduler) and the builders
# bot/github_docs.py and bot/gchat_docs.py.

MAX_LINKS = 300


class SourceAdd(BaseModel):
    links: str = Field(min_length=3, max_length=50_000)
    # The team whose knowledge these sources build. None/"" = shared by all teams (super admins).
    team: str | None = Field(default=None, max_length=40)
    schedule: dict = Field(default_factory=lambda: dict(sources.DEFAULT_SCHEDULE))
    build_now: bool = True


class SourceSettings(BaseModel):
    schedule: dict | None = None
    enabled: bool | None = None


class SourceRun(BaseModel):
    mode: Literal["auto", "full"] = "auto"


class SourceRemove(BaseModel):
    delete_doc: bool = True


def _source_or_404(sid: str, who: dict) -> dict:
    src = sources.get(sid)
    if not src or not (who["super"] or src.get("team") in who["teams"]):
        raise HTTPException(status_code=404, detail="No such source")
    return src


def _source_view(src: dict) -> dict:
    return {**src, "schedule_text": sources.describe(src["schedule"]), "runs": src.get("runs", [])[:10]}


def _github_source(owner: str, repo: str, branch: str | None, schedule: dict, by: str, team: str | None) -> dict:
    # The same repo can be a source of several teams: each team gets its own doc.
    return sources.new_source("github", f"gh-{team or 'shared'}-{owner}--{repo}".lower(),
                              f"https://github.com/{owner}/{repo}", f"{owner}/{repo}",
                              github_docs.doc_name(owner, repo, team), schedule, by, team=team,
                              github={"owner": owner, "repo": repo, "branch": branch})


def _is_chat_link(line: str) -> bool:
    return bool(re.search(r"chat\.google\.com|mail\.google\.com/chat|spaces/|#chat/", line)) or \
        (bool(re.fullmatch(r"AAAA[A-Za-z0-9_-]{4,}", line)))


@app.get("/api/admin/sources")
def admin_sources(request: Request) -> dict:
    who = _admin(request)
    return {
        "sources": [_source_view(s) for s in sources.all_sources() if who["super"] or s.get("team") in who["teams"]],
        "teams": [{"slug": t["slug"], "name": t["name"]} for t in teams.all_teams() if t["slug"] in who["teams"]],
        "can_shared": who["super"],
        "jobs": jobs.status(),
        "github": {"token": bool(config.GITHUB_TOKEN), "mode": github_docs.auth_mode(), "orgs": sorted(config.GITHUB_ORGS)},
        "gchat": {"connected": gchat_docs.connected()},
        "timezone": config.SCHEDULE_TIMEZONE,
        "scheduler": config.SCHEDULER_ENABLED,
        "default_schedule": sources.DEFAULT_SCHEDULE,
    }


@app.post("/api/admin/sources")
async def admin_add_sources(body: SourceAdd, request: Request) -> dict:
    """Add GitHub repo / org links and Google Chat space links, one per line."""
    who = _admin(request)
    team = body.team or None
    if team and (not teams.get(team) or team not in who["teams"]):
        raise HTTPException(status_code=403, detail="You can only add sources to your own team")
    if not team and not who["super"]:
        raise HTTPException(status_code=403, detail="Pick your team: only super admins add shared sources")
    try:
        schedule = sources.clean_schedule(body.schedule)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    lines = [l.strip() for l in body.links.splitlines() if l.strip() and not l.strip().startswith("#")]
    if len(lines) > MAX_LINKS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_LINKS} links at a time")
    added, skipped, errors = [], [], []
    for line in lines:
        try:
            if _is_chat_link(line):
                space = gchat_docs.parse_space(line)
                new = [sources.new_source("gchat", f"gc-{team or 'shared'}-{space}".lower(), line,
                                          f"Google Chat: {space}", gchat_docs.doc_name(space, team), schedule,
                                          who["admin"], team=team, gchat={"space": space})]
            else:
                owner, repo, branch = github_docs.parse_url(line)
                if repo:
                    new = [_github_source(owner, repo, branch, schedule, who["admin"], team)]
                else:                                   # a whole organisation: every active repo
                    repos = await asyncio.to_thread(github_docs.client().owner_repos, owner)
                    new = [_github_source(owner, r["name"], None, schedule, who["admin"], team) for r in repos]
        except github_docs.SourceError as e:
            errors.append({"link": line, "error": str(e)})
            continue
        for src in new:
            if sources.add(src):
                added.append(src["id"])
            else:
                skipped.append(src["id"])
    queued = [sid for sid in added if body.build_now and jobs.enqueue(sid, "full", who["admin"])]
    _audit(request, "sources_add", who, team=team, added=added, skipped=skipped, errors=errors,
           schedule=sources.describe(schedule), queued=len(queued))
    return {"added": added, "skipped": skipped, "errors": errors, "queued": len(queued)}


@app.post("/api/admin/sources/{sid}/settings")
def admin_source_settings(sid: str, body: SourceSettings, request: Request) -> dict:
    who = _admin(request)
    _source_or_404(sid, who)
    try:
        schedule = sources.clean_schedule(body.schedule) if body.schedule is not None else None
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    def change(s: dict) -> None:
        if schedule is not None:
            s["schedule"] = schedule
        if body.enabled is not None:
            s["enabled"] = body.enabled
        last = sources.parse_iso(s["state"].get("last_run"))
        s["state"]["next_run"] = sources.iso(sources.next_run(s["schedule"], sources.now(), last)) \
            if s["enabled"] else None

    src = sources.update(sid, change)
    _audit(request, "source_settings", who, source=sid, team=src.get("team"), schedule=sources.describe(src["schedule"]),
           enabled=src["enabled"])
    return _source_view(src)


@app.post("/api/admin/sources/{sid}/run")
def admin_source_run(sid: str, body: SourceRun, request: Request) -> dict:
    who = _admin(request)
    src = _source_or_404(sid, who)
    queued = jobs.enqueue(sid, body.mode, who["admin"])
    if not queued:
        raise HTTPException(status_code=409, detail="This source is already queued or running")
    _audit(request, "source_run", who, source=sid, team=src.get("team"), mode=body.mode)
    return {"ok": True, "jobs": jobs.status()}


@app.post("/api/admin/sources/{sid}/remove")
def admin_source_remove(sid: str, body: SourceRemove, request: Request) -> dict:
    who = _admin(request)
    src = _source_or_404(sid, who)
    if jobs.status()["current"] and jobs.status()["current"]["source"] == sid:
        raise HTTPException(status_code=409, detail="This source is running. Remove it when the run ends.")
    sources.remove(sid)
    version = None
    if body.delete_doc:
        version, saved = store().remove(src["doc"], by=who["admin"])
        current_version(force=True)
    _audit(request, "source_remove", who, source=sid, file=src["doc"], doc_deleted=body.delete_doc, version=version)
    return {"ok": True}


# ---------------------------------------------------------------- admin: documentation gaps
# Questions the docs couldn't answer, grouped and drafted daily per team (bot/gaps.py).
# Team admins see their team's bucket; super admins also "shared" and "partner".

class GapSchedule(BaseModel):
    schedule: dict


class ProposalChange(BaseModel):
    # The admin may edit the draft and its target file before adding it.
    markdown: str = Field(min_length=3, max_length=MAX_KNOWLEDGE_CHARS)
    target_file: str = Field(max_length=200)
    note: str = Field(default="", max_length=500)


class ProposalReject(BaseModel):
    reason: str = Field(default="", max_length=500)


def _gap_bucket(who: dict, bucket: str) -> str:
    allowed = who["teams"] + ([gaps.SHARED, gaps.PARTNER] if who["super"] else [])
    if bucket not in allowed or bucket not in gaps.buckets():
        raise HTTPException(status_code=404, detail="No such gap list")
    return bucket


def _proposal_change(p: dict, body: ProposalChange, who: dict) -> KnowledgeChange:
    if not valid_name(body.target_file):
        raise HTTPException(status_code=400, detail="Target file must look like internal/<team>/notes/topic.md")
    if (p["bucket"] == gaps.PARTNER) == is_internal_name(body.target_file) or \
            (p["bucket"] != gaps.PARTNER and team_of(body.target_file) != (None if p["bucket"] == gaps.SHARED else p["bucket"])):
        raise HTTPException(status_code=400, detail="That file belongs to another team or audience")
    if is_internal_name(body.target_file) and "/notes/" not in body.target_file:
        raise HTTPException(status_code=400, detail="Add gap answers to a notes file: repo and Chat docs are "
                                                    "rewritten by their scheduled refresh")
    _check_file(who, body.target_file)
    exists = store().read(body.target_file) is not None
    text = body.markdown.strip()
    if not exists:
        text = "# Answers added from users' questions\n\nSections added by admins from documentation-gap drafts.\n\n" + text
    return KnowledgeChange(file=body.target_file, text=text, mode="section" if exists else "new",
                           note=body.note or f"Gap fix: {p['summary']}"[:500])


@app.get("/api/admin/gaps")
def admin_gaps(request: Request) -> dict:
    who = _admin(request)
    st = gaps.state()
    out = []
    for b in who["teams"] + ([gaps.SHARED, gaps.PARTNER] if who["super"] else []):
        if b not in gaps.buckets():
            continue
        all_items = gaps.items(b)
        days = {}
        for g in all_items:
            days[g["ts"][:10]] = days.get(g["ts"][:10], 0) + 1
        s_ = st.get(b, {})
        out.append({"bucket": b, "name": gaps.bucket_name(b), "recipients": gaps.recipients(b),
                    "schedule": s_.get("schedule"), "schedule_text": sources.describe(s_.get("schedule") or gaps.DEFAULT_SCHEDULE),
                    "state": {k: v for k, v in s_.items() if k != "schedule"},
                    "new": [g for g in all_items if g["status"] == "new"][-50:],
                    "proposals": gaps.proposals(b, "pending"),
                    "per_day": dict(sorted(days.items())[-14:])})
    return {"buckets": out, "gmail": mailer.connected(), "jobs": jobs.status()}


@app.post("/api/admin/gaps/{bucket}/run")
def admin_gaps_run(bucket: str, request: Request) -> dict:
    who = _admin(request)
    b = _gap_bucket(who, bucket)
    if not jobs.enqueue(f"gaps:{b}", "auto", who["admin"]):
        raise HTTPException(status_code=409, detail="Already queued or running")
    _audit(request, "gaps_run", who, team=b)
    return {"ok": True}


@app.post("/api/admin/gaps/{bucket}/schedule")
def admin_gaps_schedule(bucket: str, body: GapSchedule, request: Request) -> dict:
    who = _admin(request)
    b = _gap_bucket(who, bucket)
    try:
        sch = sources.clean_schedule(body.schedule)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    nxt = sources.next_run(sch, sources.now())
    gaps.update_state(b, schedule=sch, next_run=sources.iso(nxt))
    _audit(request, "gaps_schedule", who, team=b, schedule=sources.describe(sch))
    return {"ok": True, "schedule_text": sources.describe(sch), "next_run": sources.iso(nxt)}


def _pending(who: dict, bucket: str, pid: str) -> dict:
    p = gaps.get_proposal(_gap_bucket(who, bucket), pid)
    if not p or p["status"] != "pending":
        raise HTTPException(status_code=404, detail="No such pending draft")
    return p


@app.post("/api/admin/gaps/{bucket}/proposals/{pid}/preview")
def admin_proposal_preview(bucket: str, pid: str, body: ProposalChange, request: Request) -> dict:
    who = _admin(request)
    p = _pending(who, bucket, pid)
    return admin_preview(_proposal_change(p, body, who), request)


@app.post("/api/admin/gaps/{bucket}/proposals/{pid}/accept")
def admin_proposal_accept(bucket: str, pid: str, body: ProposalChange, request: Request) -> dict:
    who = _admin(request)
    p = _pending(who, bucket, pid)
    result = _apply_change(_proposal_change(p, body, who), request, who, proposal=pid, gaps=len(p["gap_ids"]))
    gaps.close_proposal(p, "accepted", who["admin"], version=result["version"])
    return result


@app.post("/api/admin/gaps/{bucket}/proposals/{pid}/reject")
def admin_proposal_reject(bucket: str, pid: str, body: ProposalReject, request: Request) -> dict:
    who = _admin(request)
    p = _pending(who, bucket, pid)
    gaps.close_proposal(p, "rejected", who["admin"], reason=body.reason)
    _audit(request, "gap_reject", who, team=bucket, proposal=pid, note=body.reason, summary=p["summary"])
    return {"ok": True}


# ---------------------------------------------------------------- admin: question counts
# Team admins see their teams; super admins every team plus "shared" and "partner".

@app.get("/api/admin/stats")
def admin_stats(request: Request, days: int = 7) -> dict:
    who = _admin(request)
    buckets = who["teams"] + ([gaps.SHARED, gaps.PARTNER] if who["super"] else [])
    out = stats.summary(buckets, days if days > 0 else None)
    out["names"] = {b: gaps.bucket_name(b) for b in buckets}
    return out


# ---------------------------------------------------------------- admin: dashboard (/dashboard)
# Team admins see their teams' numbers and users; super admins every list, plus manual blocks.

@app.get("/api/admin/dashboard")
def admin_dashboard(request: Request, days: int = 7, list: str = "all") -> dict:
    who = _admin(request)
    allowed = who["teams"] + ([gaps.SHARED, gaps.PARTNER, MANUAL_BUCKET] if who["super"] else [])
    if list != "all" and list not in allowed:
        raise HTTPException(status_code=404, detail="No such list")
    out = stats.dashboard(allowed if list == "all" else [list], days if days > 0 else None)
    emails = {u["email"] for u in out["users"]}
    blocked = [b for b in guard.list_blocks()
               if who["super"] or (b["subject"].startswith("user:") and b["subject"][5:] in emails)]
    names = {b: gaps.bucket_name(b) for b in allowed if b != MANUAL_BUCKET}
    names[MANUAL_BUCKET] = "Blocked by an admin"
    return {**out, "blocked_now": blocked, "names": names, "lists": [b for b in allowed if b != MANUAL_BUCKET],
            "super": who["super"], "admin": who["admin"]}


# ---------------------------------------------------------------- admin: insights and answer cache (super admins)
# Raw interaction records (bot/analytics.py), the weekly AI report, and the answer cache.

class InsightsSchedule(BaseModel):
    schedule: dict


@app.get("/api/admin/insights")
def admin_insights(request: Request) -> dict:
    _super(request)
    evs = analytics.events()
    return {"state": analytics.state(), "schedule_text": sources.describe(analytics.state()["schedule"]),
            "collected": analytics._numbers(evs) if evs else {"questions": 0}, "records": len(evs),
            "reports": [{k: r[k] for k in ("id", "created", "by", "numbers", "events")} for r in analytics.reports()][:30],
            "cache": answer_cache.stats(), "gmail": mailer.connected(), "enabled": config.ANALYTICS_ENABLED}


@app.get("/api/admin/insights/export")
def admin_insights_export(request: Request) -> JSONResponse:
    """Everything recorded since the last report, as one JSON file (download)."""
    who = _super(request)
    evs = analytics.events()
    _audit(request, "insights_export", who, records=len(evs))
    return JSONResponse(evs, headers={"Content-Disposition":
                                      f'attachment; filename="interactions-{datetime.now(timezone.utc):%Y%m%d-%H%M}.json"'})


@app.get("/api/admin/insights/reports/{rid}")
def admin_insights_report(rid: str, request: Request) -> dict:
    _super(request)
    r = analytics.get_report(rid)
    if not r:
        raise HTTPException(status_code=404, detail="No such report")
    return r


@app.post("/api/admin/insights/run")
def admin_insights_run(request: Request) -> dict:
    who = _super(request)
    if not jobs.enqueue("insights", "auto", who["admin"]):
        raise HTTPException(status_code=409, detail="Already queued or running")
    _audit(request, "insights_run", who)
    return {"ok": True}


@app.post("/api/admin/insights/schedule")
def admin_insights_schedule(body: InsightsSchedule, request: Request) -> dict:
    who = _super(request)
    try:
        sch = sources.clean_schedule(body.schedule)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    nxt = sources.next_run(sch, sources.now())
    analytics.update_state(schedule=sch, next_run=sources.iso(nxt))
    _audit(request, "insights_schedule", who, schedule=sources.describe(sch))
    return {"ok": True, "schedule_text": sources.describe(sch), "next_run": sources.iso(nxt)}


@app.post("/api/admin/cache/clear")
def admin_cache_clear(request: Request) -> dict:
    who = _super(request)
    n = answer_cache.clear()
    _audit(request, "cache_clear", who, removed=n)
    return {"removed": n}


# ---------------------------------------------------------------- admin: teams (super admins)

class TeamAdd(BaseModel):
    slug: str = Field(min_length=2, max_length=31)
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    admins: list[str] | None = Field(default=None, max_length=50)


@app.get("/api/admin/teams")
def admin_teams(request: Request) -> dict:
    _super(request)
    srcs = sources.all_sources()
    docs = store().internal_names()
    return {"teams": [{**t, "sources": sum(1 for x in srcs if x.get("team") == t["slug"]),
                       "docs": sum(1 for d in docs if team_of(d) == t["slug"])} for t in teams.all_teams()],
            "super_admins": sorted(config.ADMIN_EMAILS)}


@app.post("/api/admin/teams")
def admin_add_team(body: TeamAdd, request: Request) -> dict:
    who = _super(request)
    try:
        team = teams.add(body.slug.strip().lower(), body.name, body.description)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit(request, "team_add", who, team=team["slug"], name=team["name"])
    return team


@app.post("/api/admin/teams/{slug}")
def admin_update_team(slug: str, body: TeamUpdate, request: Request) -> dict:
    who = _super(request)
    before = teams.get(slug)
    if not before:
        raise HTTPException(status_code=404, detail="No such team")
    old_admins = list(before["admins"])
    try:
        team = teams.update(slug, body.name, body.description, body.admins)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit(request, "team_update", who, team=slug, name=team["name"],
           admins_added=sorted(set(team["admins"]) - set(old_admins)),
           admins_removed=sorted(set(old_admins) - set(team["admins"])))
    return team


@app.post("/api/admin/teams/{slug}/remove")
def admin_remove_team(slug: str, request: Request) -> dict:
    who = _super(request)
    if not teams.get(slug):
        raise HTTPException(status_code=404, detail="No such team")
    if any(x.get("team") == slug for x in sources.all_sources()) or \
            any(team_of(d) == slug for d in store().internal_names()):
        raise HTTPException(status_code=409, detail="Remove the team's sources and documents first")
    teams.remove(slug)
    _audit(request, "team_remove", who, team=slug)
    return {"ok": True}


@app.post("/api/cron/tick")
def cron_tick(x_cron_secret: str | None = Header(default=None)) -> dict:
    """For Cloud Scheduler: queue the sources that are due. Needs CRON_SECRET."""
    if not config.CRON_SECRET or not x_cron_secret or not hmac.compare_digest(x_cron_secret, config.CRON_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"queued": jobs.tick(), "jobs": jobs.status()}
