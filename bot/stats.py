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

Usage per user (activity(), on every chat request and sign-in): IP addresses and devices used,
sessions (a pause over 15 minutes starts a new one), active time (time between requests within a
session, plus one minute per session), conversations, sign-ins; and per bucket "_ips" (which
users each IP served). IPs and devices are personal data: they are removed from day files older
than USAGE_IP_DAYS (the counts stay).
"""

import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta

from bot import config, sources
from bot.blobs import read_json, write_json

FIELDS = ("asked", "answered", "gaps", "cached", "local", "ai", "off_topic", "spam", "errors", "up", "down", "strikes",
          "blocks", "order_lookups")
SESSION_GAP = 15 * 60          # a longer pause starts a new session
SESSION_START = 60             # active seconds counted for a session's first request
MAX_CONVERSATIONS = 300        # conversation ids kept per user per day (for the count)
PERSONAL = ("ips", "devices", "last_ip")
_lock = threading.Lock()
_pruned = {"day": ""}


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


def device(agent: str) -> str:
    """"Chrome on macOS" from a User-Agent header."""
    a = agent or ""
    os_name = next((n for p, n in ((r"iPhone|iPad", "iOS"), (r"Android", "Android"), (r"Windows", "Windows"),
                                   (r"CrOS", "ChromeOS"), (r"Mac OS X|Macintosh", "macOS"), (r"Linux", "Linux"))
                    if re.search(p, a)), "")
    browser = next((n for p, n in ((r"Edg/", "Edge"), (r"OPR/", "Opera"), (r"Firefox/", "Firefox"), (r"Chrome/", "Chrome"),
                                   (r"Version/.*Safari/", "Safari"), (r"curl|python|httpx|PostmanRuntime|Go-http", "Script / API tool"))
                    if re.search(p, a)), "")
    if not (os_name or browser):
        return "Unknown"
    return f"{browser or 'Browser'} on {os_name}" if os_name else browser


def activity(bucket: str, user: str, ip: str = "", agent: str = "", conversation: str | None = None,
             login: bool = False) -> None:
    """Record one request (or sign-in) by a user: IP, device, session and active time. Never raises."""
    if not config.USAGE_TRACKING or not user:
        return
    try:
        with _lock:
            now = datetime.now(sources.tz())
            path = f"stats/daily/{now:%Y-%m-%d}.json"
            day = read_json(path, {}) or {}
            b = day.setdefault(bucket, {})
            u = b.setdefault("_users", {}).setdefault(user.lower(), {})
            ts, stamp = now.timestamp(), now.isoformat(timespec="minutes")
            gap = ts - u.get("_ts", 0)
            if gap <= SESSION_GAP:
                u["active_s"] = u.get("active_s", 0) + int(gap)
            else:
                u["sessions"] = u.get("sessions", 0) + 1
                u["active_s"] = u.get("active_s", 0) + SESSION_START
            u["_ts"] = ts
            u.setdefault("first", stamp)
            u["last"] = stamp
            dev = device(agent)
            u.setdefault("devices", {})[dev] = u.get("devices", {}).get(dev, 0) + 1
            if ip:
                u.setdefault("ips", {})[ip] = u.get("ips", {}).get(ip, 0) + 1
                u["last_ip"] = ip
                row = b.setdefault("_ips", {}).setdefault(ip, {"users": {}, "devices": {}, "requests": 0})
                row["users"][user.lower()] = row["users"].get(user.lower(), 0) + 1
                row["devices"][dev] = row["devices"].get(dev, 0) + 1
                row["requests"] += 1
                row["last"] = stamp
            if conversation:
                convs = u.setdefault("_convs", [])
                if conversation not in convs and len(convs) < MAX_CONVERSATIONS:
                    convs.append(conversation)
            if login:
                u["logins"] = u.get("logins", 0) + 1
            write_json(path, day)
        _forget_old_ips(now)
    except Exception:
        pass


def _forget_old_ips(now: datetime) -> None:
    """Once a day: drop IPs and devices from day files older than USAGE_IP_DAYS."""
    today = now.strftime("%Y-%m-%d")
    if _pruned["day"] == today or config.USAGE_IP_DAYS <= 0:
        return
    _pruned["day"] = today
    from bot.blobs import data

    cutoff = (now - timedelta(days=config.USAGE_IP_DAYS)).strftime("%Y-%m-%d")
    for p in data().list("stats/daily"):
        name = p.rsplit("/", 1)[-1][:-5]
        if not p.endswith(".json") or name >= cutoff:
            continue
        day = read_json(p, {}) or {}
        changed = False
        for b in day.values():
            changed |= b.pop("_ips", None) is not None
            for u in b.get("_users", {}).values():
                for k in PERSONAL:
                    changed |= u.pop(k, None) is not None
        if changed:
            write_json(p, day)


def _add(into: dict, counts: dict) -> None:
    for k, v in (counts or {}).items():
        into[k] = into.get(k, 0) + v


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
    ips: dict[str, dict] = {}
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
                row = users.setdefault(email, {"email": email, **dict.fromkeys(FIELDS, 0), "lists": [], "days": 0, "last": "",
                                               "first": "", "active_s": 0, "sessions": 0, "logins": 0, "ips": {},
                                               "devices": {}, "last_ip": "", "_convs": set()})
                for k in FIELDS:
                    row[k] += uc.get(k, 0)
                if b not in row["lists"]:
                    row["lists"].append(b)
                if uc.get("last", "") >= row["last"]:
                    row["last"] = uc.get("last", "")
                    row["last_ip"] = uc.get("last_ip") or row["last_ip"]
                if uc.get("first") and (not row["first"] or uc["first"] < row["first"]):
                    row["first"] = uc["first"]
                for k in ("active_s", "sessions", "logins"):
                    row[k] += uc.get(k, 0)
                _add(row["ips"], uc.get("ips"))
                _add(row["devices"], uc.get("devices"))
                row["_convs"] |= set(uc.get("_convs", []))
                row["_seen"] = row.get("_seen", set()) | {n}
            for ip, ic in counts.get("_ips", {}).items():
                r = ips.setdefault(ip, {"ip": ip, "users": {}, "devices": {}, "requests": 0, "last": ""})
                _add(r["users"], ic.get("users"))
                _add(r["devices"], ic.get("devices"))
                r["requests"] += ic.get("requests", 0)
                r["last"] = max(r["last"], ic.get("last", ""))
        for k in FIELDS:
            total[k] += d[k]
        per_day.append(d)
    for row in users.values():
        row["days"] = len(row.pop("_seen", ()))
        row["conversations"] = len(row.pop("_convs"))
        row["ips"] = [{"ip": k, "requests": v} for k, v in sorted(row["ips"].items(), key=lambda kv: -kv[1])]
        row["devices"] = [{"device": k, "requests": v} for k, v in sorted(row["devices"].items(), key=lambda kv: -kv[1])]
    rows = sorted(users.values(), key=lambda r: (-r["asked"], r["email"]))
    ip_rows = sorted(({**r, "users": sorted(r["users"], key=lambda e: -r["users"][e]),
                       "devices": sorted(r["devices"], key=lambda e: -r["devices"][e])} for r in ips.values()),
                     key=lambda r: (-len(r["users"]), -r["requests"]))
    return {"total": {**total, "users": len(rows), "blocked_users": sum(1 for r in rows if r["blocks"]),
                      "active_hours": round(sum(r["active_s"] for r in rows) / 3600, 1), "ips": len(ip_rows),
                      "shared_ips": sum(1 for r in ip_rows if len(r["users"]) > 1),
                      "answered_pct": round(100 * total["answered"] / total["asked"]) if total["asked"] else None,
                      # Of the questions that got an answer: how many skipped the AI (local model or cache).
                      "saved_pct": round(100 * (total["local"] + total["cached"]) / served) if (served := total["local"]
                                   + total["cached"] + total["ai"]) else None},
            "per_day": per_day, "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "strike_hours": strike_hours, "block_hours": block_hours, "users": rows, "ips": ip_rows,
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
