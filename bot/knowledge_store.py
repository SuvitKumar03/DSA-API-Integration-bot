"""Where the knowledge files live, and how updates reach every running copy of the bot.

Two backends, chosen by KNOWLEDGE_BACKEND:
    local  a folder on disk (KNOWLEDGE_DIR). For development and single-machine setups.
    gcs    a Google Cloud Storage bucket (KNOWLEDGE_BUCKET). For Cloud Run: the service's own
           service account reads and writes the bucket, so no keys are handed out.

Layout (same in both):
    <name>.md                    the live knowledge files
    _version.json                bumped on every change; bot instances poll it to reload
    _history/<name>/<ts>.md      the previous content of a file, saved before each change
    _audit/...                   who changed what and when (append-only audit trail)
    _last_modified.json          per file, only the latest change: who, when, why, the diff
"""

import json
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from bot import config

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.md$")
EXCLUDED = {"README.md"}           # folder notes for people, not knowledge for the bot
VERSION_FILE = "_version.json"
HISTORY_DIR = "_history"
AUDIT_DIR = "_audit"
LAST_FILE = "_last_modified.json"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and name not in EXCLUDED


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def names(self) -> list[str]:
        return sorted(p.name for p in self.root.glob("*.md") if valid_name(p.name))

    def read(self, name: str) -> str | None:
        p = self.root / name
        return p.read_text(encoding="utf-8") if p.exists() else None

    def version(self) -> str:
        # Admin updates bump the version file; the newest change time also catches files
        # edited by hand.
        p = self.root / VERSION_FILE
        admin = json.loads(p.read_text(encoding="utf-8"))["version"] if p.exists() else "0"
        newest = max((f.stat().st_mtime_ns for f in self.root.glob("*.md")), default=0)
        return f"{admin}-{newest}"

    def write(self, name: str, text: str, by: str) -> tuple[str, str | None]:
        """Save a file; returns the new version and the history stamp of the old content."""
        old, saved = self.read(name), None
        if old is not None:
            hist = self.root / HISTORY_DIR / name
            hist.mkdir(parents=True, exist_ok=True)
            saved = _stamp()
            (hist / f"{saved}.md").write_text(old, encoding="utf-8")
        (self.root / name).write_text(text, encoding="utf-8")
        return self._bump(name, by), saved

    def append_audit(self, record: dict) -> None:
        d = self.root / AUDIT_DIR
        d.mkdir(exist_ok=True)
        with (d / f"{record['ts'][:7]}.jsonl").open("a", encoding="utf-8") as f:   # one file per month
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def last_modified(self) -> dict:
        p = self.root / LAST_FILE
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def set_last_modified(self, name: str, record: dict) -> None:
        data = self.last_modified()
        data[name] = record                      # overwrite: only the latest change is kept
        (self.root / LAST_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def read_audit(self, limit: int = 200) -> list[dict]:
        out = []
        for f in sorted((self.root / AUDIT_DIR).glob("*.jsonl"), reverse=True):
            lines = f.read_text(encoding="utf-8").splitlines()
            out += [json.loads(l) for l in reversed(lines) if l.strip()]
            if len(out) >= limit:
                break
        return out[:limit]

    def history(self, name: str) -> list[str]:
        hist = self.root / HISTORY_DIR / name
        return sorted((p.stem for p in hist.glob("*.md")), reverse=True) if hist.exists() else []

    def read_history(self, name: str, stamp: str) -> str | None:
        p = self.root / HISTORY_DIR / name / f"{stamp}.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def _bump(self, name: str, by: str) -> str:
        version = _stamp()
        (self.root / VERSION_FILE).write_text(json.dumps(
            {"version": version, "file": name, "by": by}), encoding="utf-8")
        return version


class GCSStore:
    """Same interface on a bucket. Needs google-cloud-storage; on Cloud Run the credentials
    come from the service account automatically (grant it Storage Object Admin on this
    bucket only)."""

    def __init__(self, bucket: str, prefix: str):
        from google.cloud import storage  # imported here so local setups don't need it

        self.bucket = storage.Client().bucket(bucket)
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _blob(self, path: str):
        return self.bucket.blob(self.prefix + path)

    def names(self) -> list[str]:
        names = []
        for b in self.bucket.list_blobs(prefix=self.prefix, delimiter="/"):
            name = b.name[len(self.prefix):]
            if valid_name(name):
                names.append(name)
        return sorted(names)

    def read(self, name: str) -> str | None:
        b = self._blob(name)
        return b.download_as_text(encoding="utf-8") if b.exists() else None

    def version(self) -> str:
        b = self._blob(VERSION_FILE)
        return json.loads(b.download_as_text())["version"] if b.exists() else "0"

    def write(self, name: str, text: str, by: str) -> tuple[str, str | None]:
        old, saved = self.read(name), None
        if old is not None:
            saved = _stamp()
            self._blob(f"{HISTORY_DIR}/{name}/{saved}.md").upload_from_string(old, content_type="text/markdown")
        self._blob(name).upload_from_string(text, content_type="text/markdown")
        version = _stamp()
        self._blob(VERSION_FILE).upload_from_string(
            json.dumps({"version": version, "file": name, "by": by}), content_type="application/json")
        return version, saved

    def append_audit(self, record: dict) -> None:
        # Objects can't be appended to, so each event is its own small object (sorted by name = time).
        self._blob(f"{AUDIT_DIR}/{_stamp()}-{record['event']}.json").upload_from_string(
            json.dumps(record, ensure_ascii=False), content_type="application/json")

    def last_modified(self) -> dict:
        b = self._blob(LAST_FILE)
        return json.loads(b.download_as_text()) if b.exists() else {}

    def set_last_modified(self, name: str, record: dict) -> None:
        data = self.last_modified()
        data[name] = record
        self._blob(LAST_FILE).upload_from_string(json.dumps(data, ensure_ascii=False), content_type="application/json")

    def read_audit(self, limit: int = 200) -> list[dict]:
        blobs = sorted(self.bucket.list_blobs(prefix=f"{self.prefix}{AUDIT_DIR}/"), key=lambda b: b.name, reverse=True)
        return [json.loads(b.download_as_text()) for b in blobs[:limit]]

    def history(self, name: str) -> list[str]:
        prefix = f"{self.prefix}{HISTORY_DIR}/{name}/"
        return sorted((Path(b.name).stem for b in self.bucket.list_blobs(prefix=prefix)), reverse=True)

    def read_history(self, name: str, stamp: str) -> str | None:
        b = self._blob(f"{HISTORY_DIR}/{name}/{stamp}.md")
        return b.download_as_text(encoding="utf-8") if b.exists() else None


@lru_cache(maxsize=1)
def store() -> LocalStore | GCSStore:
    if config.KNOWLEDGE_BACKEND == "gcs":
        if not config.KNOWLEDGE_BUCKET:
            raise RuntimeError("KNOWLEDGE_BACKEND=gcs needs KNOWLEDGE_BUCKET")
        return GCSStore(config.KNOWLEDGE_BUCKET, config.KNOWLEDGE_PREFIX)
    return LocalStore(config.KNOWLEDGE_DIR)


_checked = {"at": 0.0, "version": None}


def current_version(force: bool = False) -> str:
    """The knowledge version, re-read from the store at most every KNOWLEDGE_REFRESH_SECONDS.

    This is how an update reaches every running instance: each one notices the new version
    within that interval and reloads the docs on its next question.
    """
    now = time.monotonic()
    if force or _checked["version"] is None or now - _checked["at"] >= config.KNOWLEDGE_REFRESH_SECONDS:
        _checked["version"] = store().version()
        _checked["at"] = now
    return _checked["version"]
