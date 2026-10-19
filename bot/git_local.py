"""Reads GitHub repos with the computer's own git login instead of an API token.

GITHUB_AUTH=git: the local `git` command clones over HTTPS with whatever GitHub login this
computer already has (macOS Keychain, git credential manager), so it reads every repo the
person can read, with no token and no org approval. Only for running on a person's own
machine; a server (Cloud Run) has no such login and uses GITHUB_TOKEN.

Same methods as github_docs.GitHub, so the doc builder doesn't care which one it gets. One
clone per repo is kept in DATA_DIR/git/<owner>__<repo> ("blobless": all history, file contents
fetched when needed), reused for full builds, weekly diffs and code search for gap drafts.
"""

import re
import shutil
import subprocess
from pathlib import Path

from bot import config
from bot.github_docs import SourceError

TIMEOUT = 1800                      # big repos can take a while to clone the first time
ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "GCM_INTERACTIVE": "never"}


def _url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def clone_dir(owner: str, repo: str) -> Path:
    return config.DATA_DIR / "git" / f"{owner.lower()}__{repo.lower()}"


def _git(args: list[str], cwd: Path | None = None, what: str = "", timeout: int = TIMEOUT) -> str:
    import os

    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **ENV})
    except FileNotFoundError:
        raise SourceError("git isn't installed on this computer.")
    except subprocess.TimeoutExpired:
        raise SourceError(f"git took too long for {what or 'the repo'} (over {timeout // 60} min).")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if re.search(r"not found|does not exist", err, re.I):
            raise SourceError(f"{what} not found, or your GitHub login can't read it.")
        if re.search(r"could not read Username|Authentication failed|terminal prompts disabled|403", err, re.I):
            raise SourceError("git isn't signed in to GitHub on this computer. In a terminal run "
                              f"`git ls-remote {_url('pharmeasy', 'some-repo')}` once and sign in, then Run again.")
        raise SourceError(f"git error for {what}: {err[-300:]}")
    return r.stdout


class GitLocal:
    def __init__(self, token: str | None = None):
        if not shutil.which("git"):
            raise SourceError("git isn't installed on this computer.")

    # ---- the same interface as github_docs.GitHub

    def repo(self, owner: str, repo: str) -> dict:
        out = _git(["ls-remote", "--symref", _url(owner, repo), "HEAD"], what=f"Repo {owner}/{repo}", timeout=120)
        m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
        return {"default_branch": m.group(1) if m else "master", "description": None, "language": None}

    def _ensure(self, owner: str, repo: str, branch: str) -> Path:
        """The local clone, fetched up to date for this branch."""
        d = clone_dir(owner, repo)
        if not (d / ".git").exists():
            if d.exists():
                shutil.rmtree(d)
            d.parent.mkdir(parents=True, exist_ok=True)
            _git(["clone", "--filter=blob:none", "--no-checkout", "--quiet", _url(owner, repo), str(d)],
                 what=f"Repo {owner}/{repo}")
        _git(["fetch", "--quiet", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
             d, f"Branch {branch} of {owner}/{repo}")
        return d

    def head(self, owner: str, repo: str, branch: str) -> dict:
        d = self._ensure(owner, repo, branch)
        sha, date, msg = _git(["log", "-1", "--format=%H%x00%cI%x00%s", f"origin/{branch}"], d,
                              f"{owner}/{repo}").strip().split("\x00", 2)
        return {"sha": sha, "date": date, "message": msg[:200]}

    def owner_repos(self, owner: str) -> list[dict]:
        raise SourceError("Adding a whole organisation needs the GitHub API (a token). With the git login, "
                          "paste the repo links one per line.")

    def download(self, owner: str, repo: str, sha: str, dest: Path) -> Path:
        """Check out `sha` in the cached clone and return it (dest isn't needed: nothing is copied)."""
        d = clone_dir(owner, repo)
        _git(["checkout", "--force", "--quiet", "--detach", sha], d, f"{owner}/{repo}")
        _git(["clean", "-fdq"], d, f"{owner}/{repo}")
        return d

    def compare(self, owner: str, repo: str, base: str, head: str) -> dict:
        """The same shape as GitHub's compare API: status, ahead_by, commits, files (with patch)."""
        d = clone_dir(owner, repo)
        if subprocess.run(["git", "cat-file", "-e", f"{base}^{{commit}}"], cwd=d, capture_output=True).returncode:
            raise SourceError("The last documented commit is no longer on the branch (force push).")
        ahead = subprocess.run(["git", "merge-base", "--is-ancestor", base, head], cwd=d).returncode == 0
        log = _git(["log", "--format=%H%x00%s", f"{base}..{head}"], d, f"{owner}/{repo}")
        commits = [{"sha": h, "commit": {"message": s}} for h, s in
                   (line.split("\x00", 1) for line in log.splitlines() if "\x00" in line)][::-1]
        stats = {}
        for line in _git(["diff", "--numstat", "-M", base, head], d, f"{owner}/{repo}").splitlines():
            a, r, name = line.split("\t", 2)
            stats[name.split(" => ")[-1].replace("}", "").replace("{", "")] = (a, r)
        files = []
        for line in _git(["diff", "--name-status", "-M", base, head], d, f"{owner}/{repo}").splitlines():
            parts = line.split("\t")
            code, name = parts[0], parts[-1]
            status = {"A": "added", "D": "removed", "M": "modified", "R": "renamed"}.get(code[0], "modified")
            a, r = stats.get(name, ("0", "0"))
            f = {"filename": name, "status": status, "additions": int(a) if a.isdigit() else 0,
                 "deletions": int(r) if r.isdigit() else 0}
            if code.startswith("R"):
                f["previous_filename"] = parts[1]
            if a != "-":                                   # "-" = binary file: no patch
                f["patch"] = _git(["diff", "-M", base, head, "--", name], d, f"{owner}/{repo}")[-60000:]
            files.append(f)
        return {"status": "ahead" if ahead else "diverged", "ahead_by": len(commits), "commits": commits,
                "files": files}

    # ---- code search for gap drafts (the checked-out copy of each repo)

    def grep(self, owner: str, repo: str, term: str, limit: int = 5) -> list[str]:
        d = clone_dir(owner, repo)
        if not (d / ".git").exists():
            return []
        r = subprocess.run(["git", "grep", "-l", "-I", "-i", "-F", "-e", term], cwd=d, capture_output=True,
                           text=True, timeout=120)
        return r.stdout.splitlines()[:limit]
