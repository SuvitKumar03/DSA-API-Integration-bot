"""Running question counts for the admin page: asked, answered, gaps, feedback.

Unlike the insights records (deleted after each weekly report), these counters are kept: one
small file per day (stats/daily/<YYYY-MM-DD>.json, dates in SCHEDULE_TIMEZONE), with counts
per bucket (a team, "shared" for internal users across all teams, "partner").

    asked      every question (including cached and spam replies)
    answered   got a real answer from the docs: not a gap, off-topic, spam or an error
    gaps       the docs couldn't answer it (the hidden gap marker)
    cached     answered from the answer cache (no AI call); also counted as answered
    off_topic  unrelated questions;  spam: gibberish / repeats / manipulation
    errors     the AI failed;  up / down: 👍 / 👎 feedback
"""

import threading
from collections import defaultdict
from datetime import datetime, timedelta

from bot import sources
from bot.blobs import read_json, write_json

FIELDS = ("asked", "answered", "gaps", "cached", "off_topic", "spam", "errors", "up", "down")
_lock = threading.Lock()


def _today() -> str:
    return datetime.now(sources.tz()).strftime("%Y-%m-%d")


def bump(bucket: str, **counts: int) -> None:
    """Add to today's counters for a bucket. Never raises: counting must not break chat."""
    try:
        with _lock:
            path = f"stats/daily/{_today()}.json"
            day = read_json(path, {}) or {}
            b = day.setdefault(bucket, {})
            for k, v in counts.items():
                if k in FIELDS and v:
                    b[k] = b.get(k, 0) + int(v)
            write_json(path, day)
    except Exception:
        pass


def summary(buckets: list[str], days: int | None) -> dict:
    """Totals, per bucket and per day for these buckets over the last `days` days (None = all)."""
    from bot.blobs import data

    names = sorted(p.rsplit("/", 1)[-1][:-5] for p in data().list("stats/daily") if p.endswith(".json"))
    if days:
        start = (datetime.now(sources.tz()) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        names = [n for n in names if n >= start]
    total = dict.fromkeys(FIELDS, 0)
    per_bucket = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    per_day = {}
    for n in names:
        day = read_json(f"stats/daily/{n}.json", {}) or {}
        d = dict.fromkeys(FIELDS, 0)
        for b, counts in day.items():
            if b not in buckets:
                continue
            for k in FIELDS:
                v = counts.get(k, 0)
                total[k] += v
                per_bucket[b][k] += v
                d[k] += v
        per_day[n] = d
    rate = lambda c: round(100 * c["answered"] / c["asked"]) if c["asked"] else None
    return {"total": {**total, "answered_pct": rate(total)},
            "per_bucket": {b: {**c, "answered_pct": rate(c)} for b, c in per_bucket.items()},
            "per_day": per_day, "since": names[0] if names else None}
