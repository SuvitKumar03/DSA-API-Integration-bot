"""Saved answers for questions that were already asked, so they don't need a new AI call.

Only the first question of a conversation is cached (later turns depend on the conversation).
The key is the question after normalising (lowercase, single spaces, no trailing ? or !), the
audience (partner / internal), the team and the knowledge version. So:
    - any knowledge change (admin edit, source refresh, gap fix) starts a fresh cache;
    - a partner never gets an internal answer, and each team gets its own team's answer;
    - an answer with a 👎 is removed, and answers that were stopped, off-topic, errors or
      documentation gaps are never saved.
Entries expire after ANSWER_CACHE_DAYS. Stored in the data store (cache/answers/), with a
small in-memory copy for speed.
"""

import hashlib
import re
import time
from collections import OrderedDict

from bot import config
from bot.blobs import data, read_json, write_json
from bot.knowledge_store import current_version

MEMORY_ITEMS = 500
_memory: OrderedDict[str, dict] = OrderedDict()
_stats = {"hits": 0, "misses": 0}


def normalise(question: str) -> str:
    q = re.sub(r"\s+", " ", question.strip().lower())
    return re.sub(r"[\s?!.]+$", "", q)


def key(question: str, audience: str, team: str | None) -> str:
    raw = f"{current_version()}|{audience}|{team or '-'}|{normalise(question)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get(question: str, audience: str, team: str | None) -> dict | None:
    if not config.ANSWER_CACHE_ENABLED:
        return None
    k = key(question, audience, team)
    entry = _memory.get(k) or read_json(f"cache/answers/{k}.json")
    if entry and time.time() - entry["ts"] < config.ANSWER_CACHE_DAYS * 86400:
        _memory[k] = entry
        _memory.move_to_end(k)
        _stats["hits"] += 1
        entry["hits"] = entry.get("hits", 0) + 1
        return entry
    _stats["misses"] += 1
    return None


def put(question: str, audience: str, team: str | None, answer: str, message_id: str) -> None:
    if not config.ANSWER_CACHE_ENABLED or not answer.strip():
        return
    k = key(question, audience, team)
    entry = {"ts": time.time(), "answer": answer, "message_id": message_id, "audience": audience, "team": team}
    write_json(f"cache/answers/{k}.json", entry)
    _memory[k] = entry
    while len(_memory) > MEMORY_ITEMS:
        _memory.popitem(last=False)


def drop(question: str, audience: str, team: str | None) -> bool:
    """Remove a saved answer (after a 👎), so the next person gets a fresh one."""
    k = key(question, audience, team)
    _memory.pop(k, None)
    return data().delete(f"cache/answers/{k}.json")


def clear() -> int:
    _memory.clear()
    n = 0
    for p in data().list("cache/answers"):
        n += data().delete(p)
    return n


def stats() -> dict:
    return {**_stats, "saved": len(data().list("cache/answers")), "enabled": config.ANSWER_CACHE_ENABLED,
            "days": config.ANSWER_CACHE_DAYS}
