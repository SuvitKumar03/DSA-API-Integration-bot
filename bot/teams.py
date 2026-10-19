"""Teams, and who administers what.

Roles:
    super admin   ADMIN_EMAILS (and dev ADMIN_CODES). Everything: partner docs, shared internal
                  docs, every team, teams and their admins, blocked users, the full audit trail.
    team admin    listed in a team's "admins". Only that team's internal docs and sources.

Team names live in data, not in code: the first list comes from DEFAULT_TEAMS in .env.

Each team has its own internal knowledge, kept apart in the knowledge store:
    internal/<team>/{repos,gchat,notes}/<name>.md
Internal docs outside a team folder (internal/{repos,gchat,notes}/...) are shared by all teams
and managed by super admins.

Internal users pick a team on the chat page; answers then use only that team's docs plus the
shared ones, so the same name in two teams' repos is answered the way the chosen team uses it.

The registry is teams/registry.json in the data store. It is seeded from DEFAULT_TEAMS the
first time it is read. Admin rights are looked up on every request, so removing someone from a
team takes effect at once.
"""

import re
import threading
from datetime import datetime, timezone

from bot import config
from bot.blobs import read_json, write_json

REGISTRY = "teams/registry.json"
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
RESERVED = {"repos", "gchat", "notes", "shared", "all", "internal", "partner"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.RLock()


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(slug or "")) and slug not in RESERVED


def _load() -> dict:
    with _lock:
        reg = read_json(REGISTRY)
        if reg is None:
            now = datetime.now(timezone.utc).isoformat()
            reg = {"teams": {slug: {"slug": slug, "name": name, "description": "", "admins": [], "created_at": now}
                             for slug, name in config.DEFAULT_TEAMS}}
            write_json(REGISTRY, reg)
        return reg


def all_teams() -> list[dict]:
    return sorted(_load()["teams"].values(), key=lambda t: t["name"].lower())


def get(slug: str | None) -> dict | None:
    return _load()["teams"].get(slug) if slug else None


def name_of(slug: str | None) -> str:
    t = get(slug)
    return t["name"] if t else "All teams"


def add(slug: str, name: str, description: str = "") -> dict:
    if not valid_slug(slug):
        raise ValueError("Team id: 2-31 lowercase letters, digits or '-', starting with a letter "
                         "(not repos, gchat, notes, shared, all or internal)")
    with _lock:
        reg = _load()
        if slug in reg["teams"]:
            raise ValueError("A team with this id already exists")
        team = {"slug": slug, "name": name.strip()[:60] or slug, "description": description.strip()[:300],
                "admins": [], "created_at": datetime.now(timezone.utc).isoformat()}
        reg["teams"][slug] = team
        write_json(REGISTRY, reg)
        return team


def update(slug: str, name: str | None = None, description: str | None = None,
           admins: list[str] | None = None) -> dict:
    with _lock:
        reg = _load()
        team = reg["teams"].get(slug)
        if not team:
            raise KeyError(slug)
        if name is not None:
            team["name"] = name.strip()[:60] or team["name"]
        if description is not None:
            team["description"] = description.strip()[:300]
        if admins is not None:
            clean = sorted({a.strip().lower() for a in admins if a.strip()})
            bad = [a for a in clean if not EMAIL_RE.match(a)]
            if bad:
                raise ValueError(f"Not an email address: {', '.join(bad)}")
            team["admins"] = clean
        write_json(REGISTRY, reg)
        return team


def remove(slug: str) -> None:
    with _lock:
        reg = _load()
        reg["teams"].pop(slug, None)
        write_json(REGISTRY, reg)


def admin_teams(email: str | None) -> list[str]:
    """Slugs of the teams this email administers."""
    e = (email or "").strip().lower()
    return [t["slug"] for t in all_teams() if e and e in t["admins"]]


def is_super_email(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in config.ADMIN_EMAILS
