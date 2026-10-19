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
    strikes    misbehaviour strikes (by reason and hour of day);  blocks: 2-hour blocks

Each bucket also keeps per-user counts by email (for the admin dashboard): "_users", plus
"_reasons", "_strike_hours" and "_block_hours" (24 counts, hour of day in SCHEDULE_TIMEZONE).
Admins see emails of their own team's users only.
"""

import threading
from collections import defaultdict
from datetime import datetime, timedelta

from bot import sources
from bot.blobs import read_json, write_json

FIELDS = ("asked", "answered", "gaps", "cached", "off_topic", "spam", "errors", "up", "down", "strikes", "blocks")
_lock = threading.Lock()


def _today() -> str:
    return datetime.now(sources.tz()).strftime("%Y-%m-%d")


def bump(bucket: str, user: str | None = None, reason: str | None = None, **counts: int) -> None:
    """Add to today's counters for a bucket (and the user, by email). Never raises: counting
    must not break chat."""
    try:
        with _lock:
            now = datetime.now(sources.tz())
            path = f"stats/daily/{now:%Y-%m-%d}.json"
            day = read_json(path, {}) or {}
            b = day.setdefault(bucket, {})
            u = b.setdefault("_users", {}).setdefault(user.lower(), {}) if user else None
            for k, v in counts.items():
                if k in FIELDS and v:
                    b[k] = b.get(k, 0) + int(v)
                    if u is not None:
                        u[k] = u.get(k, 0) + int(v)
            if u is not None:
                u["last"] = now.isoformat(timespec="minutes")
            if counts.get("strikes"):
                b.setdefault("_reasons", {})[reason or "other"] = b.get("_reasons", {}).get(reason or "other", 0) + 1
                b.setdefault("_strike_hours", [0] * 24)[now.hour] += 1
            if counts.get("blocks"):
                b.setdefault("_block_hours", [0] * 24)[now.hour] += 1
            write_json(path, day)
    except Exception:
        pass


def dashboard(buckets: list[str], days: int | None) -> dict:
    """Everything the dashboard shows, for these buckets over the last `days` days (None = all)."""
    from bot.blobs import data

    names = sorted(p.rsplit("/", 1)[-1][:-5] for p in data().list("stats/daily") if p.endswith(".json"))
    if days:
        start = (datetime.now(sources.tz()) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        names = [n for n in names if n >= start]
    total = dict.fromkeys(FIELDS, 0)
    per_day, reasons = [], defaultdict(int)
    strike_hours, block_hours = [0] * 24, [0] * 24
    users: dict[str, dict] = {}
    for n in names:
        day = read_json(f"stats/daily/{n}.json", {}) or {}
        d = {"date": n, **dict.fromkeys(FIELDS, 0)}
        for b, counts in day.items():
            if b not in buckets:
                continue
            for k in FIELDS:
                d[k] += counts.get(k, 0)
            for r, c in counts.get("_reasons", {}).items():
                reasons[r] += c
            for h in range(24):
                strike_hours[h] += (counts.get("_strike_hours") or [0] * 24)[h]
                block_hours[h] += (counts.get("_block_hours") or [0] * 24)[h]
            for email, uc in counts.get("_users", {}).items():
                row = users.setdefault(email, {"email": email, **dict.fromkeys(FIELDS, 0), "lists": [], "days": 0, "last": ""})
                for k in FIELDS:
                    row[k] += uc.get(k, 0)
                if b not in row["lists"]:
                    row["lists"].append(b)
                row["last"] = max(row["last"], uc.get("last", ""))
                row["_seen"] = row.get("_seen", set()) | {n}
        for k in FIELDS:
            total[k] += d[k]
        per_day.append(d)
    for row in users.values():
        row["days"] = len(row.pop("_seen", ()))
    rows = sorted(users.values(), key=lambda r: (-r["asked"], r["email"]))
    return {"total": {**total, "users": len(rows), "blocked_users": sum(1 for r in rows if r["blocks"]),
                      "answered_pct": round(100 * total["answered"] / total["asked"]) if total["asked"] else None},
            "per_day": per_day, "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "strike_hours": strike_hours, "block_hours": block_hours, "users": rows,
            "since": names[0] if names else None}


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
