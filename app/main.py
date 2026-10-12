"""FastAPI app: serves the chat UI and the chat API."""

import json
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot import config
from bot.engine import complete, stream_answer
from bot.guardrails import StreamRedactor, probe_flags, redact, scrub_secrets
from bot.prompt import SUMMARY_INSTRUCTIONS, load_documentation

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
