"""FastAPI app: serves the chat UI and the chat API."""

import hashlib
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot import admin_auth, config
from bot.engine import complete, stream_answer
from bot.guardrails import StreamRedactor, probe_flags, redact, scrub_secrets
from bot.knowledge_store import current_version, store, valid_name
from bot.prompt import SUMMARY_INSTRUCTIONS, load_documentation
from bot.sections import apply_update, unified_diff

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
    messages: list[Message] = Field(min_length=1)
    # Session memory kept in the partner's browser; never stored on the server.
    summary: str = Field(default="", max_length=config.SUMMARY_MAX_CHARS * 2)


class SummaryRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=60)
    previous: str = Field(default="", max_length=config.SUMMARY_MAX_CHARS * 2)


class Feedback(BaseModel):
    message_id: str
    rating: Literal["up", "down"]
    comment: str = Field(default="", max_length=2000)


def _partner_for(code: str | None) -> str:
    """Return the partner name for an access code, or raise 401."""
    if not config.ACCESS_CODES:
        return "local"
    if code and code in config.ACCESS_CODES:
        return config.ACCESS_CODES[code]
    raise HTTPException(status_code=401, detail="Invalid access code")


_requests: dict[str, deque] = defaultdict(deque)


def _local_only(request: Request) -> None:
    """The claude_code provider runs on the developer's own subscription: localhost only."""
    if config.PROVIDER == "claude_code" and request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="This test setup only answers on the local machine.")


def _clean_summary(text: str) -> str:
    text, _ = redact(scrub_secrets(text or ""))
    return text.strip()[: config.SUMMARY_MAX_CHARS]


def _check_rate_limit(partner: str) -> None:
    now = time.time()
    window = _requests[partner]
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= config.RATE_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many questions. Please try again later.")
    window.append(now)


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


@app.on_event("startup")
def _warm_up() -> None:
    if config.PROVIDER == "claude_code" and config.ACCESS_CODES:
        raise RuntimeError("PROVIDER=claude_code is for personal local testing only and cannot be used "
                           "with partner ACCESS_CODES. Use PROVIDER=anthropic with an API key.")
    production = bool(config.ACCESS_CODES)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
def ui_config() -> dict:
    return {
        "botName": config.BOT_NAME,
        "requiresCode": bool(config.ACCESS_CODES),
        "maxChars": config.MAX_MESSAGE_CHARS,
        "supportContact": config.SUPPORT_CONTACT,
        "summaryIdleMinutes": config.SUMMARY_IDLE_MINUTES,
        "summaryEveryMessages": config.SUMMARY_EVERY_MESSAGES,
        "summaryTtlDays": config.SUMMARY_TTL_DAYS,
    }


@app.post("/api/login")
def login(x_access_code: str | None = Header(default=None)) -> dict:
    return {"partner": _partner_for(x_access_code)}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request, x_access_code: str | None = Header(default=None)):
    _local_only(request)
    partner = _partner_for(x_access_code)
    _check_rate_limit(partner)

    history = [m.model_dump() for m in req.messages][-config.MAX_HISTORY_MESSAGES:]
    if history[0]["role"] != "user":
        history = history[1:]
    question = history[-1]
    if question["role"] != "user":
        raise HTTPException(status_code=400, detail="Last message must be from the user")
    if len(question["content"]) > config.MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail="Message is too long")

    message_id = uuid.uuid4().hex
    summary = _clean_summary(req.summary)
    flags = probe_flags(question["content"])
    if flags:
        log.warning("Probe-like question from %s: %s", partner, flags)

    async def generate():
        answer_parts: list[str] = []
        usage: dict = {}
        started = time.time()
        # Every answer passes the internal-name filter before it reaches the partner.
        redactor = StreamRedactor()
        async for chunk in stream_answer(history, usage_out=usage, summary=summary):
            out = redactor.feed(chunk)
            if out:
                answer_parts.append(out)
                yield out
        tail = redactor.flush()
        if tail:
            answer_parts.append(tail)
            yield tail
        if redactor.hits:
            log.warning("Redacted internal terms from answer %s: %s", message_id, sorted(set(redactor.hits)))
        _append_jsonl(CHAT_LOG, {
            "id": message_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "partner": partner,
            # Tokens, keys, emails and phone numbers are never written to the server log.
            "question": scrub_secrets(question["content"]),
            "answer": scrub_secrets("".join(answer_parts)),
            "turns": len(history),
            "latency_s": round(time.time() - started, 2),
            "usage": usage,
            "flags": flags,
            "redacted": sorted(set(redactor.hits)),
            "had_summary": bool(summary),
        })

    return StreamingResponse(
        generate(),
        media_type="text/plain; charset=utf-8",
        headers={"X-Message-Id": message_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/feedback")
def feedback(fb: Feedback, x_access_code: str | None = Header(default=None)) -> dict:
    partner = _partner_for(x_access_code)
    _append_jsonl(FEEDBACK_LOG, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "partner": partner,
        **fb.model_dump(),
        "comment": scrub_secrets(fb.comment),
    })
    return {"ok": True}


@app.post("/api/summarize")
async def summarize(req: SummaryRequest, request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    """Update the partner's session memory. The result is returned to the browser and not stored here."""
    _local_only(request)
    partner = _partner_for(x_access_code)
    _check_rate_limit(partner + ":summary")
    transcript = "\n\n".join(f"[{m.role.upper()}]\n{m.content[:3000]}" for m in req.messages)
    prompt = (f"Previous memory:\n{_clean_summary(req.previous) or '(none)'}\n\n"
              f"New conversation:\n{scrub_secrets(transcript)}")
    text = await complete(SUMMARY_INSTRUCTIONS, prompt)
    if not text:
        raise HTTPException(status_code=503, detail="Could not update the summary right now.")
    return {"summary": _clean_summary(text)}


# ---------------------------------------------------------------- admin: knowledge updates
# Two roles: partners (ACCESS_CODES) can only chat; admins can also change the knowledge.
# Admins sign in with Google (ADMIN_EMAILS); see bot/admin_auth.py. Every admin action is
# written to the audit trail in the knowledge store (_audit/), next to the files it describes.

MAX_KNOWLEDGE_CHARS = 400_000
MAX_AUDIT_DIFF_CHARS = 20_000


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


def _admin(request: Request) -> dict:
    """The signed-in admin as {"admin": email or name, "method": ...}, or 401/404."""
    if not admin_auth.enabled():
        raise HTTPException(status_code=404, detail="Admin is not enabled on this server")
    email = admin_auth.read_session(request.cookies.get(admin_auth.SESSION_COOKIE))
    if admin_auth.is_admin_email(email):
        return {"admin": email, "method": "google"}
    email = admin_auth.iap_email(request.headers.get("x-goog-authenticated-user-email"))
    if admin_auth.is_admin_email(email):
        return {"admin": email, "method": "iap"}
    code = request.headers.get("x-admin-code")
    if code and code in config.ADMIN_CODES:
        return {"admin": config.ADMIN_CODES[code], "method": "code"}
    raise HTTPException(status_code=401, detail="Please sign in as an admin")


def _audit(request: Request, event: str, who: dict | None = None, **details) -> None:
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


class GoogleLogin(BaseModel):
    credential: str = Field(min_length=10, max_length=5000)


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
    if not admin_auth.is_admin_email(email):
        _audit(request, "login_denied", {"admin": email, "method": "google"})
        raise HTTPException(status_code=403, detail=f"{email} is not an admin")
    who = {"admin": email, "method": "google"}
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
    return _admin(request)


def _plan(change: KnowledgeChange) -> tuple[str, str, list[dict]]:
    """Work out the old text, the new text and what changes, without saving anything."""
    if not valid_name(change.file):
        raise HTTPException(status_code=400, detail="File name must look like 05-topic.md")
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
    _admin(request)
    s = store()
    last = s.last_modified()
    return {"version": current_version(force=True),
            "files": [{"name": n, "chars": len(s.read(n) or ""),
                       "last": {k: v for k, v in last[n].items() if k != "diff"} if n in last else None}
                      for n in s.names()]}


@app.get("/api/admin/files/{name}")
def admin_file(name: str, request: Request) -> dict:
    _admin(request)
    text = store().read(name) if valid_name(name) else None
    if text is None:
        raise HTTPException(status_code=404, detail="No such knowledge file")
    return {"name": name, "text": text, "history": store().history(name),
            "last_modified": store().last_modified().get(name)}


@app.post("/api/admin/preview")
def admin_preview(change: KnowledgeChange, request: Request) -> dict:
    _admin(request)
    old, new, actions = _plan(change)
    _, hits = redact(change.text)
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
    if change.base_version and change.base_version != current_version(force=True):
        raise HTTPException(status_code=409, detail="The knowledge changed since your preview. Preview again.")
    old, new, actions = _plan(change)
    version, saved_as = store().write(change.file, new, by=who["admin"])
    current_version(force=True)          # this instance reloads now; others within the refresh interval
    diff = unified_diff(old, new, change.file)
    _audit(request, "apply", who, file=change.file, mode=change.mode, note=change.note, actions=actions,
           version=version, previous_saved_as=saved_as, sha_before=_sha(old) if old else None, sha_after=_sha(new),
           chars_before=len(old), chars_after=len(new), diff=diff[:MAX_AUDIT_DIFF_CHARS],
           diff_truncated=len(diff) > MAX_AUDIT_DIFF_CHARS)
    _record_last_modified(who, change.file, "apply", mode=change.mode, note=change.note, actions=actions,
                          version=version, diff=diff[:MAX_AUDIT_DIFF_CHARS])
    log.info("Knowledge %s updated by %s (%s): %s", change.file, who["admin"], change.mode, actions)
    return {"ok": True, "version": version, "actions": actions}


@app.post("/api/admin/restore")
def admin_restore(req: Restore, request: Request) -> dict:
    who = _admin(request)
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
    _admin(request)
    return {"events": store().read_audit(max(1, min(limit, 500)))}
