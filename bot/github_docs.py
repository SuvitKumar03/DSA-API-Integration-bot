"""Builds and refreshes the knowledge document for one GitHub repository.

Full build (first time, or when the change is too big for a delta):
    1. Download the branch as a tarball (GitHub API, GITHUB_TOKEN, read-only).
    2. Pick the files that explain the repo: README and docs first, then build files and
       config, then controllers/routes, services and models, then the rest; skip vendored
       code, binaries, lock files and anything that looks like a secrets file. Values of
       secret-looking settings are blanked before anything is sent to the AI.
    3. The AI writes notes per batch of files (REPO_BATCH_CHARS), then one document from
       the notes, in fixed sections (see DOC_SECTIONS).

Weekly delta: compare the last documented commit with the branch head. Only the diff goes
to the AI, which rewrites just the sections the change affects; they replace the old
sections (bot/sections.py) and a line is added to the change log. No new commits = no AI call.
"""

import asyncio
import logging
import re
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from bot import config
from bot.engine import complete
from bot.sections import apply_update, split_sections

log = logging.getLogger(__name__)

DOC_SECTIONS = ["Overview", "Tech stack", "How to run and deploy", "Structure", "API endpoints",
                "Background jobs and messaging", "Data model", "Integrations", "Configuration",
                "Business rules and error messages", "Notes"]
CHANGELOG = "Change log"
CHANGELOG_KEEP = 40
MAX_FILE_BYTES = 150_000
COMPARE_FILE_LIMIT = 300       # GitHub's compare API lists at most 300 files

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "out", "target", "bin", "obj", ".next", ".nuxt",
             "coverage", "__pycache__", ".venv", "venv", ".idea", ".vscode", ".gradle", ".mvn", "Pods",
             "DerivedData", ".terraform", "bower_components", "fonts", "images", "img", ".yarn", ".cache"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".tiff", ".psd", ".woff", ".woff2",
            ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi", ".wav", ".zip", ".gz", ".tgz", ".rar", ".7z",
            ".jar", ".war", ".class", ".so", ".dll", ".dylib", ".exe", ".bin", ".pdf", ".doc", ".docx", ".xls",
            ".xlsx", ".ppt", ".pptx", ".db", ".sqlite", ".pyc", ".map", ".lock", ".apk", ".aab", ".ipa", ".keystore",
            ".jks", ".p12", ".pfx", ".pem", ".key", ".crt", ".cer", ".der", ".csr", ".parquet", ".avro", ".h5"}
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "gemfile.lock",
              "composer.lock", "go.sum", "cargo.lock", "npm-shrinkwrap.json"}
SECRET_FILE_RE = re.compile(r"^\.env(?!\.(?:example|sample|template)$)|^id_(?:rsa|dsa|ecdsa|ed25519)|"
                            r"(?:^|[._-])(?:credentials?|secrets?)\.(?:json|ya?ml|xml|properties|env|txt)$"
                            r"|\.(?:tfstate|tfvars)$|service[-_]?account.*\.json$", re.I)

BUILD_FILES = {"package.json", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "requirements.txt",
               "pyproject.toml", "setup.py", "go.mod", "cargo.toml", "composer.json", "gemfile", "dockerfile",
               "makefile", "procfile", "app.yaml", "serverless.yml", "serverless.yaml", "jenkinsfile", "tsconfig.json"}
API_HINT = re.compile(r"controller|route|router|handler|endpoint|resource|api|view|urls\.py|graphql|grpc|proto"
                      r"|openapi|swagger|^(?:main|app|server|index|application)\.", re.I)
DOMAIN_HINT = re.compile(r"service|model|entity|dto|schema|repositor|dao|client|consumer|producer|listener|job|"
                         r"cron|worker|scheduler|migration|\.sql$|enum|constant|exception|error|validator", re.I)
TEST_HINT = re.compile(r"(?:^|/)(?:tests?|spec|__tests__|e2e|testdata|fixtures?)(?:/|$)|[._-](?:test|spec)\.", re.I)

# Values of secret-looking settings are blanked before code reaches the AI.
SECRET_KV_RE = re.compile(
    r"""(?i)((?:[\w.-]*?)(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"""
    r"""client[_-]?secret|auth[_-]?key|signing[_-]?key|credential)s?[\w.-]*["']?\s*(?:[:=]|=>)\s*)(["']?)"""
    r"""(?!\$\{|\{\{|<|process\.env|os\.|System\.|env\(|getenv|config\.|settings\.|None|null|true|false)"""
    r"""([^\s"',;)}]{4,})""")
URL_CRED_RE = re.compile(r"\b([a-z][a-z0-9+.-]*://)([^:/\s@]+):([^@/\s]+)@", re.I)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
TOKEN_RES = [re.compile(p) for p in (r"AKIA[0-9A-Z]{16}", r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]+", r"gh[pousr]_[A-Za-z0-9]{30,}",
                                      r"github_pat_[A-Za-z0-9_]{40,}", r"sk-[A-Za-z0-9_-]{20,}", r"AIza[0-9A-Za-z_-]{30,}",
                                      r"xox[baprs]-[A-Za-z0-9-]{10,}")]


class SourceError(Exception):
    """A problem the admin can act on; the message is shown on the admin page."""


def scrub_code(text: str) -> str:
    text = PRIVATE_KEY_RE.sub("<redacted private key>", text)
    text = URL_CRED_RE.sub(r"\1\2:<redacted>@", text)
    text = SECRET_KV_RE.sub(r"\1\2<redacted>", text)
    for r in TOKEN_RES:
        text = r.sub("<redacted>", text)
    return text


# ---------------------------------------------------------------- GitHub API

URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com[/:]([\w.-]+)(?:/([\w.-]+?))?(?:\.git)?"
                    r"(?:/tree/([^?#\s]+))?/?(?:[?#].*)?$", re.I)
SSH_RE = re.compile(r"^git@github\.com:([\w.-]+)/([\w.-]+?)(?:\.git)?$", re.I)
SHORT_RE = re.compile(r"^([\w.-]+)/([\w.-]+)$")


def parse_url(text: str) -> tuple[str, str | None, str | None]:
    """(owner, repo or None for a whole org/user, branch or None)."""
    t = text.strip()
    m = URL_RE.match(t) or SSH_RE.match(t) or SHORT_RE.match(t)
    if not m:
        raise SourceError(f"Not a GitHub link: {t}")
    owner, repo = m.group(1), m.group(2)
    branch = m.group(3) if m.re is URL_RE else None
    if config.GITHUB_ORGS and owner.lower() not in config.GITHUB_ORGS:
        raise SourceError(f"Only repos of {', '.join(sorted(config.GITHUB_ORGS))} can be added (got {owner})")
    return owner, repo, branch


def doc_name(owner: str, repo: str, team: str | None = None) -> str:
    """internal/<team>/repos/... for a team's doc, internal/repos/... for one shared by all teams."""
    return f"internal/{team + '/' if team else ''}repos/{owner.lower()}__{repo.lower()}.md"


def auth_mode() -> str:
    if config.GITHUB_AUTH in ("token", "git"):
        return config.GITHUB_AUTH
    return "token" if config.GITHUB_TOKEN else "git"


def client():
    """The repo reader for GITHUB_AUTH: the GitHub API (token) or the local git login."""
    if auth_mode() == "git":
        from bot.git_local import GitLocal
        return GitLocal()
    return GitHub()


class GitHub:
    def __init__(self, token: str | None = None):
        token = token or config.GITHUB_TOKEN
        if not token:
            raise SourceError("No GitHub token. Add a read-only GITHUB_TOKEN to .env and restart the server.")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "integration-assistant"})

    def _check(self, r: requests.Response, what: str) -> requests.Response:
        if r.status_code == 401:
            raise SourceError("GitHub rejected the token (expired or wrong). Update GITHUB_TOKEN.")
        if r.status_code == 403 and "sso" in r.headers.get("x-github-sso", "").lower() + r.text.lower():
            raise SourceError("The GitHub token isn't authorised for the organisation's SSO. "
                              "On GitHub: Settings > Tokens > Configure SSO > Authorise.")
        if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
            raise SourceError("GitHub API rate limit reached. It resets within an hour.")
        if r.status_code == 404:
            raise SourceError(f"{what} not found, or the token can't read it.")
        if r.status_code >= 400:
            raise SourceError(f"GitHub error {r.status_code} for {what}: {r.text[:200]}")
        return r

    def get(self, path: str, what: str, **params) -> dict | list:
        return self._check(self.s.get(f"{config.GITHUB_API}{path}", params=params, timeout=60), what).json()

    def repo(self, owner: str, repo: str) -> dict:
        return self.get(f"/repos/{owner}/{repo}", f"Repo {owner}/{repo}")

    def head(self, owner: str, repo: str, branch: str) -> dict:
        c = self.get(f"/repos/{owner}/{repo}/commits/{branch}", f"Branch {branch} of {owner}/{repo}")
        return {"sha": c["sha"], "date": c["commit"]["committer"]["date"],
                "message": c["commit"]["message"].splitlines()[0][:200]}

    def owner_repos(self, owner: str) -> list[dict]:
        """All non-archived repos of an org (or user), name order."""
        out, page = [], 1
        kind = "orgs"
        while True:
            try:
                batch = self.get(f"/{kind}/{owner}/repos", f"Organisation {owner}", per_page=100, page=page, type="all")
            except SourceError:
                if kind == "orgs" and page == 1:
                    kind = "users"
                    continue
                raise
            out += [r for r in batch if not r.get("archived") and not r.get("disabled")]
            if len(batch) < 100:
                break
            page += 1
        return sorted(out, key=lambda r: r["name"].lower())

    def download(self, owner: str, repo: str, sha: str, dest: Path) -> Path:
        r = self._check(self.s.get(f"{config.GITHUB_API}/repos/{owner}/{repo}/tarball/{sha}", stream=True,
                                   timeout=300), f"Code of {owner}/{repo}")
        archive = dest / "repo.tar.gz"
        with archive.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        with tarfile.open(archive) as tar:
            tar.extractall(dest / "src", filter="data")    # "data" refuses links and paths outside dest
        archive.unlink()
        roots = [p for p in (dest / "src").iterdir() if p.is_dir()]
        return roots[0] if len(roots) == 1 else dest / "src"

    def compare(self, owner: str, repo: str, base: str, head: str) -> dict:
        return self.get(f"/repos/{owner}/{repo}/compare/{base}...{head}", f"Changes in {owner}/{repo}")


# ---------------------------------------------------------------- choosing files

def skip_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1].lower()
    if any(p in SKIP_DIRS for p in parts[:-1]) or name in LOCK_FILES or SECRET_FILE_RE.search(parts[-1]):
        return True
    suffix = Path(name).suffix
    return suffix in SKIP_EXT or name.endswith((".min.js", ".min.css", ".bundle.js", ".chunk.js"))


def priority(rel: str) -> int:
    name = rel.rsplit("/", 1)[-1].lower()
    if name.startswith("readme") or (rel.lower().startswith("docs/") and name.endswith(".md")):
        return 0
    if name in BUILD_FILES or name.endswith((".csproj", ".sln")) or rel.startswith(".github/workflows/"):
        return 1
    if re.search(r"application[\w-]*\.(?:ya?ml|properties)$|(?:^|/)config/|settings\.py$|\.env\.(?:example|sample)$",
                 rel, re.I):
        return 2
    if TEST_HINT.search(rel):
        return 6
    if API_HINT.search(rel):
        return 3
    if DOMAIN_HINT.search(rel):
        return 4
    return 5


def collect_files(root: Path) -> tuple[list[tuple[str, str]], list[str], int]:
    """(chosen [(path, text)], every path in the repo, number of readable files)."""
    every, candidates = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        every.append(rel)
        if skip_path(rel) or p.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.count("\n") + 1
        if not text.strip() or len(text) / lines > 400:          # minified or generated
            continue
        candidates.append((priority(rel), rel.count("/"), rel, text))
    candidates.sort()
    chosen, used = [], 0
    for _, _, rel, text in candidates:
        if used + len(text) > config.REPO_MAX_CHARS:
            continue
        chosen.append((rel, scrub_code(text)))
        used += len(text)
    return chosen, every, len(candidates)


def tree_outline(paths: list[str], limit: int = 400) -> str:
    """Folders with their file counts, so the AI sees the layout without every file name."""
    from collections import Counter

    dirs = Counter(p.rsplit("/", 1)[0] if "/" in p else "." for p in paths if not skip_path(p))
    lines = [f"{d}/ ({n} files)" for d, n in sorted(dirs.items())]
    return "\n".join(lines[:limit]) + (f"\n... {len(lines) - limit} more folders" if len(lines) > limit else "")


def batches(files: list[tuple[str, str]]) -> list[str]:
    out, cur, size = [], [], 0
    for rel, text in files:
        block = f"### FILE: {rel}\n```\n{text[: config.REPO_BATCH_CHARS]}\n```\n"
        if cur and size + len(block) > config.REPO_BATCH_CHARS:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(block)
        size += len(block)
    if cur:
        out.append("".join(cur))
    return out


# ---------------------------------------------------------------- AI prompts

NOTES_SYSTEM = """You document a source code repository for the internal knowledge base that the company's developers, integration team and support use to answer questions. You get one part of the repository's files.

Write dense, factual notes in Markdown bullets, grouped by area, covering only what these files show:
- What the files do and how they fit together.
- Every HTTP endpoint: method, path, purpose, request and response fields, headers, auth, error responses.
- Background jobs, schedulers, queues and topics (names), events produced or consumed.
- Data models, tables and important fields; status values and enums.
- Calls to other services and third parties (names and hosts as written in code).
- Configuration keys and environment variable names (never their secret values).
- Business rules, validations, limits, timeouts, retries and exact error messages.
Use exact names from the code. Never copy secrets (passwords, tokens, keys, connection strings); write <redacted>. No introduction, no guesses."""

DOC_SYSTEM = """You write the knowledge document for one source code repository, for the company's internal knowledge base. Readers are developers, the integration team and support; a chatbot answers their questions from this document.

Write Markdown. Start with one short paragraph (no heading) saying what the repository is, what it does and who or what uses it. Then these sections, in this order, with exactly these headings (## level). Leave a section out only when nothing applies:
{sections}

Rules:
- Facts only from the input. Exact names for endpoints, classes, fields, tables, queues, config keys, env vars and error messages.
- "API endpoints": a table with Method | Path | Purpose | Auth, then short notes on important request/response fields and errors.
- "Configuration": key and env var names with what they control; never values of secrets.
- Be complete but compact: no filler, no marketing, no speculation.
- Never output secrets (passwords, tokens, keys, connection strings).
- Do not write a title line or a change log; they are added automatically."""

DELTA_SYSTEM = """You keep the knowledge document of a source code repository up to date. You get the current document and the code changes merged to the main branch since it was written (commit messages and diff).

Output, in this exact format:
SUMMARY: <one sentence saying what changed, for the change log>
Then only the sections that must change because of this diff. Start each with its ## heading exactly as it appears in the current document, and write the whole section again with the change included (it replaces the old section). Keep everything that is still true. Use a new ## heading only if the change adds a new kind of content; use one of: {sections}.

If no section needs to change (for example only tests, formatting or comments changed), output the SUMMARY line and then a line with only NO_CHANGES.

Rules: facts only from the diff and the current document, exact names, never output secrets, never write a "Change log" section."""


async def _ai(system: str, prompt: str, max_tokens: int = 16000) -> str:
    text = await complete(system, prompt, max_tokens=max_tokens, timeout=config.DOC_AI_TIMEOUT_SECONDS)
    if not text:
        raise SourceError("The AI call failed or timed out (details in the server log). Try Run again later.")
    return text.strip()


def _strip_fences(text: str) -> str:
    m = re.match(r"^```(?:markdown|md)?\s*\n([\s\S]*?)\n```\s*$", text.strip())
    return m.group(1) if m else text


# ---------------------------------------------------------------- document assembly

def _source_line(owner: str, repo: str, branch: str, head: dict, built: str, updated: str | None = None) -> str:
    line = (f"> Source: https://github.com/{owner}/{repo} (branch `{branch}`, commit `{head['sha'][:10]}` "
            f"of {head['date'][:10]}). Generated by the knowledge bot on {built}")
    return line + (f", last updated {updated}." if updated else ".")


SOURCE_LINE_RE = re.compile(r"^> Source: https://github\.com/.*$", re.M)


def _add_changelog(doc: str, entry: str) -> str:
    """Prepend one line to the change log section (kept by code, newest first)."""
    lines = doc.rstrip("\n").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().lower() == f"## {CHANGELOG.lower()}")
    except StopIteration:
        return doc.rstrip("\n") + f"\n\n## {CHANGELOG}\n\n- {entry}\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    items = [l for l in lines[start + 1:end] if l.startswith("- ")]
    body = [f"## {CHANGELOG}", ""] + [f"- {entry}"] + items[: CHANGELOG_KEEP - 1]
    return "\n".join(lines[:start] + body + ([""] + lines[end:] if end < len(lines) else [])) + "\n"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def build(owner: str, repo: str, branch: str | None, progress=lambda msg: None) -> dict:
    """Full build. Returns {"text", "head", "branch", "summary"}."""
    gh = client()
    info = await asyncio.to_thread(gh.repo, owner, repo)
    branch = branch or info.get("default_branch") or "master"
    head = await asyncio.to_thread(gh.head, owner, repo, branch)
    progress(f"Downloading {owner}/{repo}@{branch} ({head['sha'][:10]})")
    with tempfile.TemporaryDirectory(prefix="repo-doc-") as tmp:
        root = await asyncio.to_thread(gh.download, owner, repo, head["sha"], Path(tmp))
        files, every, readable = await asyncio.to_thread(collect_files, root)
    if not files:
        raise SourceError("No readable source files found in this repository.")
    outline = tree_outline(every)
    parts = batches(files)
    coverage = f"Built from {len(files)} of {readable} readable files ({len(every)} files in total)."
    meta = (f"Repository: {owner}/{repo}\nDescription on GitHub: {info.get('description') or '-'}\n"
            f"Main language: {info.get('language') or '-'}\nBranch: {branch}\n\nFolders:\n{outline}\n\n")
    sections = "\n".join(f"## {s}" for s in DOC_SECTIONS)
    if len(parts) == 1:
        progress("Writing the document")
        body = await _ai(DOC_SYSTEM.format(sections=sections), meta + "Source files:\n\n" + parts[0])
    else:
        notes = []
        for n, part in enumerate(parts, 1):
            progress(f"Reading files: part {n} of {len(parts)}")
            notes.append(await _ai(NOTES_SYSTEM, meta + f"Files (part {n} of {len(parts)}):\n\n" + part, 8000))
        progress("Writing the document from the notes")
        body = await _ai(DOC_SYSTEM.format(sections=sections),
                         meta + "Notes on the files, written part by part:\n\n" + "\n\n---\n\n".join(notes))
    body = scrub_code(_strip_fences(body)).strip()
    body = re.sub(r"^#\s[^\n]*\n+", "", body)        # the title is added below; drop one the AI wrote
    text = f"# {owner}/{repo}\n\n{_source_line(owner, repo, branch, head, _today())}\n> {coverage}\n\n{body}\n"
    text = _add_changelog(text, f"{_today()}: full build at `{head['sha'][:10]}` ({head['message']})")
    return {"text": text, "head": head, "branch": branch, "summary": f"Full build at {head['sha'][:10]}"}


def _diff_text(cmp: dict) -> str:
    commits = [f"- {c['sha'][:10]} {c['commit']['message'].splitlines()[0][:150]}" for c in cmp.get("commits", [])][-60:]
    parts = ["Commits:\n" + "\n".join(commits), "Changed files:"]
    for f in cmp.get("files", []):
        name = f["filename"]
        if skip_path(name):
            parts.append(f"### {f['status']}: {name} (skipped: binary, lock, generated or secrets file)")
            continue
        patch = f.get("patch")
        header = f"### {f['status']}: {name} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
        if f.get("previous_filename"):
            header += f" (was {f['previous_filename']})"
        parts.append(header + ("\n```diff\n" + scrub_code(patch) + "\n```" if patch else " (no patch: large change)"))
    return "\n\n".join(parts)


async def update(owner: str, repo: str, branch: str, current_doc: str, last_sha: str,
                 progress=lambda msg: None) -> dict:
    """Delta update. Returns {"text" (None if the doc doesn't change), "head", "summary", "full"}."""
    gh = client()
    head = await asyncio.to_thread(gh.head, owner, repo, branch)
    if head["sha"] == last_sha:
        return {"text": None, "head": head, "summary": "No new commits", "full": False}
    try:
        cmp = await asyncio.to_thread(gh.compare, owner, repo, last_sha, head["sha"])
    except SourceError:
        cmp = None                                   # the old commit is gone (force push): rebuild
    diff = _diff_text(cmp) if cmp else ""
    if (not cmp or cmp.get("status") not in ("ahead", "identical") or len(cmp.get("files", [])) >= COMPARE_FILE_LIMIT
            or len(diff) > config.DELTA_MAX_CHARS):
        progress("Change too big for a delta: rebuilding the whole document")
        result = await build(owner, repo, branch, progress)
        return {**result, "full": True, "summary": f"Rebuilt at {head['sha'][:10]} (change too big for a delta)"}
    progress(f"Reading {cmp.get('ahead_by', '?')} new commit(s), {len(cmp.get('files', []))} changed file(s)")
    out = _strip_fences(await _ai(DELTA_SYSTEM.format(sections=", ".join(DOC_SECTIONS)),
                                  f"Current document:\n\n{current_doc}\n\n=== Changes on {branch} since "
                                  f"{last_sha[:10]} (now {head['sha'][:10]}) ===\n\n{diff}"))
    m = re.match(r"^\s*SUMMARY:\s*(.+)$", out, re.M)
    summary = (m.group(1).strip() if m else "Code changes")[:300]
    rest = out[m.end():].strip() if m else out.strip()
    span = f"`{last_sha[:10]}`..`{head['sha'][:10]}`"
    text = current_doc
    if rest and not rest.startswith("NO_CHANGES"):
        try:
            sections = [s for s in split_sections(scrub_code(rest)) if s[1] != CHANGELOG.lower()]
        except ValueError:
            raise SourceError("The AI's update wasn't in the expected format. Run again, or use Rebuild.")
        if sections:
            text, _ = apply_update(text, "\n\n".join("\n".join(body) for _, _, body in sections))
    built = re.search(r"Generated by the knowledge bot on (\d{4}-\d\d-\d\d)", current_doc)
    text = SOURCE_LINE_RE.sub(lambda _: _source_line(owner, repo, branch, head, built.group(1) if built else _today(),
                                                       _today()), text, count=1)
    text = _add_changelog(text, f"{_today()}: {span}: {summary}")
    return {"text": text, "head": head, "summary": summary, "full": False}
