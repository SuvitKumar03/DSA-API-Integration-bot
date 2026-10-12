"""Keeps internal implementation details out of what partners see.

Three layers, all driven by one rules file (INTERNAL_TERMS_FILE, git-ignored because the
list itself names internal systems):

1. sanitize_doc()  - rewrites/drops lines in the knowledge files before the model sees them.
2. StreamRedactor  - scrubs any internal term from the answer while it streams.
3. probe_flags()   - labels questions that look like attempts to extract internals or
                     override instructions, so they can be reviewed in the logs.

Rules file syntax (one rule per line, # for comments):
    term                       case-insensitive whole word
    cs: TERM                   case-sensitive whole word
    re: regex                  regular expression
    drop-line: regex           remove matching lines from the docs
    rewrite: regex => text     replace in the docs (regex groups allowed, e.g. \\1)
"""

import logging
import re
from functools import lru_cache

from bot import config

log = logging.getLogger(__name__)

REPLACEMENT = "[internal detail]"

# Generic patterns that are never partner-facing, kept in code because they name nothing specific.
BUILTIN_PATTERNS = [
    r"\btbl_\w+",                                                  # database tables
    r"\busp_\w+",                                                  # stored procedures
    r"\b[\w-]+\.(?:private|internal|cluster\.local)\b[\w./:-]*",  # internal hostnames
    r"\b(?!AbortController\b)\w+(?:HttpClient|ServiceImpl|Repository|Controller)\b",  # code class names
    r"\b(?:production|staging|qa|dev)-[\w-]+-(?:service|svc|app)\b[\w./:-]*",
]

_SECRET_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{10,}"), "Bearer <token>"),
    (re.compile(r"eyJ[\w-]{8,}\.[\w-]+(?:\.[\w-]+)?"), "<token>"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,})"), "<key>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"), "<phone>"),
]


class Rules:
    def __init__(self, terms: list[re.Pattern], drops: list[re.Pattern], rewrites: list[tuple[re.Pattern, str]]):
        self.terms = terms
        self.drops = drops
        self.rewrites = rewrites


@lru_cache(maxsize=1)
def rules() -> Rules:
    terms = [re.compile(p) for p in BUILTIN_PATTERNS]
    drops, rewrites = [], []
    path = config.INTERNAL_TERMS_FILE
    if not path.exists():
        log.warning("No internal terms file at %s; only built-in patterns are filtered", path)
        return Rules(terms, drops, rewrites)
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("re:"):
                terms.append(re.compile(line[3:].strip()))
            elif line.startswith("cs:"):
                terms.append(re.compile(r"(?<![\w-])" + re.escape(line[3:].strip()) + r"(?![\w-])"))
            elif line.startswith("drop-line:"):
                drops.append(re.compile(line[10:].strip()))
            elif line.startswith("rewrite:"):
                pattern, _, repl = line[8:].partition("=>")
                rewrites.append((re.compile(pattern.strip()), repl.strip()))
            else:
                terms.append(re.compile(r"(?<![\w-])" + re.escape(line) + r"(?![\w-])", re.I))
        except re.error as e:
            log.error("Bad rule on line %d of %s: %s", n, path, e)
    return Rules(terms, drops, rewrites)


def sanitize_doc(text: str) -> str:
    """Prepare a knowledge file for the model: apply rewrites, drop internal-only lines."""
    r = rules()
    for pattern, repl in r.rewrites:
        text = pattern.sub(repl, text)
    lines = [ln for ln in text.splitlines() if not any(p.search(ln) for p in r.drops)]
    return "\n".join(lines)


def redact(text: str) -> tuple[str, list[str]]:
    """Replace internal terms; returns the new text and the terms that were hit."""
    hits = []
    for pattern in rules().terms:
        def _sub(m):
            hits.append(m.group(0))
            return REPLACEMENT
        text = pattern.sub(_sub, text)
    return text, hits


def scrub_secrets(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class StreamRedactor:
    """Redacts a streamed answer without breaking streaming.

    Holds back the last HOLD characters so a term split across two chunks is still caught
    before any part of it is sent.
    """

    HOLD = 80

    def __init__(self):
        self.raw = ""
        self.sent = 0
        self.hits: list[str] = []

    def feed(self, chunk: str) -> str:
        self.raw += chunk
        clean, _ = redact(self.raw)
        safe = max(self.sent, len(clean) - self.HOLD)
        out = clean[self.sent:safe]
        self.sent = safe
        return out

    def flush(self) -> str:
        clean, self.hits = redact(self.raw)
        out = clean[self.sent:]
        self.sent = len(clean)
        return out


_PROBES = [
    ("prompt_extraction", r"system\s*prompt|your (?:instructions|rules|prompt)|initial prompt|repeat (?:everything|the text) above"),
    ("instruction_override", r"ignore (?:all|any|the|your)?\s*(?:previous|above|prior)|disregard (?:the|your|all)|you are now|developer mode|jailbreak|act as (?!a partner)"),
    ("internal_probe", r"internal (?:api|name|service|system|architecture|endpoint|header|code|database|db|table|host|ones?|values?)|"
                       r"(?:hidden|undocumented|secret) (?:values?|headers?|endpoints?|params?|parameters?)|"
                       r"(?:which|what) (?:database|db|service|server|framework|stack|language)\b|source code|"
                       r"how (?:is|are) (?:it|this|the api|validation) implemented|backend (?:service|code|architecture)|"
                       r"stack ?trace|table name|class name|function name"),
]
_PROBE_RES = [(name, re.compile(p, re.I)) for name, p in _PROBES]


def probe_flags(question: str) -> list[str]:
    return [name for name, pattern in _PROBE_RES if pattern.search(question)]
