"""The admin's list of knowledge sources (GitHub repos and Google Chat spaces) and when each
one is refreshed.

Each source builds one internal knowledge file:
    github  internal/repos/<owner>__<repo>.md   from the repo's master (default) branch
    gchat   internal/gchat/<space id>.md         from the space's messages

Schedules (per source, set by an admin; times in SCHEDULE_TIMEZONE):
    {"type": "weekly", "days": [4], "time": "17:00"}   every Friday at 5 pm (0 = Monday)
    {"type": "every", "days": 3, "time": "17:00"}      every 3 days at 5 pm
    {"type": "manual"}                                  only when an admin presses Run

The registry is one JSON file in the data store (sources/registry.json). Changes go through
update() under a lock so the scheduler and admin requests never overwrite each other.
"""

import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot import config
from bot.blobs import read_json, write_json

REGISTRY = "sources/registry.json"
MAX_RUNS_KEPT = 20
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DEFAULT_SCHEDULE = {"type": "weekly", "days": [4], "time": "17:00"}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_lock = threading.RLock()


def tz() -> ZoneInfo:
    return ZoneInfo(config.SCHEDULE_TIMEZONE)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_iso(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


# ---------------------------------------------------------------- schedules

def clean_schedule(s: dict | None) -> dict:
    """Validate an admin-supplied schedule; raises ValueError with a readable message."""
    s = dict(s or DEFAULT_SCHEDULE)
    kind = s.get("type")
    if kind == "manual":
        return {"type": "manual"}
    if not TIME_RE.match(str(s.get("time", ""))):
        raise ValueError("Time must look like 17:00 (24-hour)")
    if kind == "weekly":
        days = sorted({int(d) for d in s.get("days") or []})
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("Pick at least one weekday")
        return {"type": "weekly", "days": days, "time": s["time"]}
    if kind == "every":
        n = int(s.get("days") or 0)
        if not 1 <= n <= 90:
            raise ValueError("Every N days: N must be between 1 and 90")
        return {"type": "every", "days": n, "time": s["time"]}
    raise ValueError("Schedule type must be weekly, every or manual")


def describe(s: dict) -> str:
    if s["type"] == "manual":
        return "Manual only"
    at = f"{s['time']} ({config.SCHEDULE_TIMEZONE})"
    if s["type"] == "weekly":
        if len(s["days"]) == 7:
            return f"Every day at {at}"
        return f"Every {', '.join(DAY_NAMES[d] for d in s['days'])} at {at}"
    return f"Every {s['days']} day{'s' if s['days'] != 1 else ''} at {at}"


def next_run(s: dict, after: datetime, last_run: datetime | None = None) -> datetime | None:
    """The first scheduled time after `after` (for "every", counted from the last run)."""
    if s["type"] == "manual":
        return None
    hh, mm = map(int, s["time"].split(":"))
    local = after.astimezone(tz())
    if s["type"] == "weekly":
        for d in range(8):
            day = local.date() + timedelta(days=d)
            at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz())
            if at > after and day.weekday() in s["days"]:
                return at.astimezone(timezone.utc)
        return None
    # A time already past (the server was off) is simply due at once.
    day = (last_run or after).astimezone(tz()).date() + timedelta(days=s["days"])
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz()).astimezone(timezone.utc)


# ---------------------------------------------------------------- registry

def _load() -> dict:
    return read_json(REGISTRY, {"sources": {}}) or {"sources": {}}


def all_sources() -> list[dict]:
    with _lock:
        return sorted(_load()["sources"].values(), key=lambda x: x["added_at"])


def get(source_id: str) -> dict | None:
    with _lock:
        return _load()["sources"].get(source_id)


def add(source: dict) -> bool:
    """Add a new source; False if one with the same id exists."""
    with _lock:
        reg = _load()
        if source["id"] in reg["sources"]:
            return False
        reg["sources"][source["id"]] = source
        write_json(REGISTRY, reg)
        return True


def update(source_id: str, fn) -> dict | None:
    """Apply fn(source) and save; returns the updated source (None if it doesn't exist)."""
    with _lock:
        reg = _load()
        src = reg["sources"].get(source_id)
        if src is None:
            return None
        fn(src)
        write_json(REGISTRY, reg)
        return src


def remove(source_id: str) -> dict | None:
    with _lock:
        reg = _load()
        src = reg["sources"].pop(source_id, None)
        if src:
            write_json(REGISTRY, reg)
        return src


def new_source(kind: str, source_id: str, url: str, label: str, doc: str, schedule: dict, by: str,
               **details) -> dict:
    t = now()
    return {"id": source_id, "kind": kind, "url": url, "label": label, "doc": doc, "schedule": schedule,
            "enabled": True, "added_by": by, "added_at": iso(t), **details,
            "state": {"status": "new", "next_run": iso(next_run(schedule, t))}, "runs": []}


def record_run(src: dict, run: dict) -> None:
    src["runs"] = ([run] + src.get("runs", []))[:MAX_RUNS_KEPT]


def due(at: datetime) -> list[dict]:
    """Enabled sources whose scheduled time has come."""
    out = []
    for src in all_sources():
        nxt = parse_iso(src["state"].get("next_run"))
        if src["enabled"] and nxt and nxt <= at and src["state"].get("status") not in ("queued", "running"):
            out.append(src)
    return out
