"""Who is asking, and their own saved chats and memory.

Everyone signs in with Google (USER_LOGIN=google). A user is internal when Google says the
account is a verified Workspace account on one of INTERNAL_DOMAINS (the `hd` claim, which a
personal Gmail account using a work address can't have). Internal users also get the internal
knowledge (repo docs, Google Chat docs); partners only get the public partner docs.

Each user's data is kept apart under users/<uid>/ in the data store (bot/blobs.py):
    profile.json               email, name, role, first and last seen
    (each conversation also records the team it was started in, for internal users)
    memory.json                the short summary of past conversations (session memory)
    index.json                 list of conversations: id, title, last update, message count
    conversations/<id>.json    the messages of one conversation
uid is a hash of the email, so storage paths don't show addresses.
"""

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from bot import config
from bot.blobs import data, read_json, write_json

CONV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TITLE_CHARS = 80

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class User:
    email: str
    name: str = ""
    is_internal: bool = False
    method: str = "google"            # google | code (partner access code) | local (no sign-in)

    @property
    def uid(self) -> str:
        return hashlib.sha256(self.email.lower().encode()).hexdigest()[:24]

    @property
    def role(self) -> str:
        return "internal" if self.is_internal else "partner"


def is_internal_account(claims: dict) -> bool:
    email = (claims.get("email") or "").lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    hosted = (claims.get("hd") or "").lower()
    return bool(claims.get("email_verified")) and domain in config.INTERNAL_DOMAINS and hosted == domain


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock(user: User) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(user.uid, threading.Lock())


def _path(user: User, name: str) -> str:
    return f"users/{user.uid}/{name}"


def touch_profile(user: User, picture: str = "") -> None:
    with _lock(user):
        p = read_json(_path(user, "profile.json"), {}) or {}
        p.update(email=user.email, name=user.name or p.get("name", ""), role=user.role, method=user.method,
                 picture=picture or p.get("picture", ""), last_seen=_now())
        p.setdefault("first_seen", p["last_seen"])
        write_json(_path(user, "profile.json"), p)


# ---------------------------------------------------------------- memory

def get_memory(user: User) -> dict | None:
    m = read_json(_path(user, "memory.json"))
    if not m or not m.get("summary"):
        return None
    if time.time() - m.get("updated_at", 0) > config.SUMMARY_TTL_DAYS * 86400:
        return None
    return m


def set_memory(user: User, summary: str) -> dict:
    m = {"summary": summary, "updated_at": time.time()}
    write_json(_path(user, "memory.json"), m)
    return m


def forget_memory(user: User) -> None:
    data().delete(_path(user, "memory.json"))


# ---------------------------------------------------------------- conversations

def valid_conversation_id(cid: str | None) -> bool:
    return bool(cid) and bool(CONV_ID_RE.match(cid))


def list_conversations(user: User) -> list[dict]:
    return read_json(_path(user, "index.json"), []) or []


def get_conversation(user: User, cid: str) -> dict | None:
    if not valid_conversation_id(cid):
        return None
    return read_json(_path(user, f"conversations/{cid}.json"))


def history_for(user: User, cid: str) -> list[dict]:
    """The saved messages of a conversation as [{role, content}] (for the AI)."""
    conv = get_conversation(user, cid) or {}
    return [{"role": m["role"], "content": m["content"]} for m in conv.get("messages", [])]


def save_turn(user: User, cid: str, question: str, answer: str, message_id: str, stopped: bool = False,
              team: str | None = None) -> None:
    """Append one question and its answer to a conversation (created on first use, with the
    team it was started in; a conversation stays in its team)."""
    if not valid_conversation_id(cid) or not answer.strip():
        return
    with _lock(user):
        now = _now()
        conv = get_conversation(user, cid) or {"id": cid, "created": now, "messages": [], "team": team,
                                                "title": question.strip().splitlines()[0][:TITLE_CHARS]}
        conv["messages"] += [{"role": "user", "content": question, "ts": now},
                             {"role": "assistant", "content": answer, "ts": now, "id": message_id,
                              **({"stopped": True} if stopped else {})}]
        conv["updated"] = now
        write_json(_path(user, f"conversations/{cid}.json"), conv)

        index = [c for c in list_conversations(user) if c["id"] != cid]
        index.insert(0, {"id": cid, "title": conv["title"], "updated": now, "messages": len(conv["messages"]),
                         "team": conv.get("team")})
        for old in index[config.MAX_CONVERSATIONS:]:
            data().delete(_path(user, f"conversations/{old['id']}.json"))
        write_json(_path(user, "index.json"), index[: config.MAX_CONVERSATIONS])


def delete_conversation(user: User, cid: str) -> bool:
    if not valid_conversation_id(cid):
        return False
    with _lock(user):
        found = data().delete(_path(user, f"conversations/{cid}.json"))
        index = list_conversations(user)
        write_json(_path(user, "index.json"), [c for c in index if c["id"] != cid])
        return found
