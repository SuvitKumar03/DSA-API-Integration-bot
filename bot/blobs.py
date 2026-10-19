"""Small key/value file storage for app data: users' chat history and memory, the source
registry (GitHub repos, Google Chat spaces) and job state.

Same two backends as the knowledge files (bot/knowledge_store.py):
    local  a folder on disk (DATA_DIR). Development and single-machine setups.
    gcs    a Cloud Storage bucket (DATA_BUCKET, under DATA_PREFIX). Cloud Run's disk is wiped
           on every restart, so per-user data must live in the bucket there.

Paths are plain relative names like "users/<uid>/memory.json".
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from bot import config

PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


def _check(path: str) -> str:
    if not PATH_RE.match(path) or ".." in path.split("/"):
        raise ValueError(f"Bad storage path: {path!r}")
    return path


class LocalBlobs:
    def __init__(self, root: Path):
        self.root = Path(root)

    def read(self, path: str) -> str | None:
        p = self.root / _check(path)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def write(self, path: str, text: str) -> None:
        p = self.root / _check(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)                     # never leave a half-written file behind

    def delete(self, path: str) -> bool:
        p = self.root / _check(path)
        if p.exists():
            p.unlink()
            return True
        return False

    def list(self, prefix: str) -> list[str]:
        """Paths of the files directly inside the folder `prefix`."""
        d = self.root / _check(prefix.strip("/"))
        return sorted(f"{prefix.strip('/')}/{p.name}" for p in d.iterdir() if p.is_file()
                      and not p.name.endswith(".tmp")) if d.is_dir() else []


class GCSBlobs:
    def __init__(self, bucket: str, prefix: str):
        from google.cloud import storage  # imported here so local setups don't need it

        self.bucket = storage.Client().bucket(bucket)
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def read(self, path: str) -> str | None:
        b = self.bucket.blob(self.prefix + _check(path))
        return b.download_as_text(encoding="utf-8") if b.exists() else None

    def write(self, path: str, text: str) -> None:
        self.bucket.blob(self.prefix + _check(path)).upload_from_string(text, content_type="application/json")

    def delete(self, path: str) -> bool:
        b = self.bucket.blob(self.prefix + _check(path))
        if b.exists():
            b.delete()
            return True
        return False

    def list(self, prefix: str) -> list[str]:
        full = self.prefix + _check(prefix.strip("/")) + "/"
        return sorted(b.name[len(self.prefix):] for b in self.bucket.list_blobs(prefix=full, delimiter="/"))


def read_json(path: str, default=None):
    text = data().read(path)
    return json.loads(text) if text else default


def write_json(path: str, value) -> None:
    data().write(path, json.dumps(value, ensure_ascii=False, indent=1))


@lru_cache(maxsize=1)
def data() -> LocalBlobs | GCSBlobs:
    if config.DATA_BACKEND == "gcs":
        if not config.DATA_BUCKET:
            raise RuntimeError("DATA_BACKEND=gcs needs DATA_BUCKET (or KNOWLEDGE_BUCKET)")
        return GCSBlobs(config.DATA_BUCKET, config.DATA_PREFIX)
    return LocalBlobs(config.DATA_DIR)
