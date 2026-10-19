"""Scan the files staged for commit for things that must never reach the public repo.

Checks every staged file for: client / colleague names (chat_export/private_names.json),
internal terms (internal_terms.txt rules), API keys and tokens, OAuth client IDs, and real
email addresses. Run before every commit:

    git add -A && python scripts/leak_scan.py

Exit code 1 if anything is found. Some hits are expected (for example a prompt rule that
names a word the bot must never use); judge each one.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.guardrails import redact  # noqa: E402

KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{30,}|sk-ant-[\w-]{20,}|sk-[A-Za-z0-9]{32,}|GOCSPX-[\w-]+|ya29\.[\w-]+|"
                    r"eyJ[\w-]{20,}\.[\w-]{10,}|\d{8,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
SAFE_EMAILS = re.compile(r"@(example\.com|yourcompany\.com|partner\d\.com|anthropic\.com)$|^noreply@", re.I)


def main() -> None:
    files = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                           capture_output=True, text=True, cwd=ROOT).stdout.split()
    names_file = ROOT / "chat_export" / "private_names.json"
    names = json.loads(names_file.read_text()) if names_file.exists() else {}
    name_res = [re.compile(r"(?i)\b" + re.escape(n) + r"\b")
                for n in names.get("client_names", []) + names.get("askers", [])]
    found = 0
    for f in files:
        try:
            text = (ROOT / f).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits = {
            "names": sorted({p.pattern for p in name_res if p.search(text)}),
            "internal terms": sorted(set(redact(text)[1]))[:10],
            "keys": len(KEY_RE.findall(text)),
            "emails": sorted({e for e in EMAIL_RE.findall(text) if not SAFE_EMAILS.search(e)})[:5],
        }
        hits = {k: v for k, v in hits.items() if v}
        if hits:
            found += 1
            print(f"CHECK {f}: {hits}")
    print(f"{len(files)} staged files scanned, {found} with findings")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
