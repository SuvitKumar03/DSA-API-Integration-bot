"""Finds the parts of the internal knowledge that matter for one question.

The partner docs are small enough to send whole with every question. The internal docs are
not: one .md per GitHub repo (the org has ~190) plus Google Chat docs. So for internal users:
    catalog()   one line per internal doc (name + what it is). Changes only when the knowledge
                changes, so it sits in the cached part of the prompt.
    search()    the most relevant sections for the question, ranked with BM25 (keyword search
                that understands camelCase and snake_case names), up to INTERNAL_CONTEXT_CHARS.

Both are per team (bot/teams.py): with a team chosen, only that team's docs and the shared
docs are listed and searched, so a name that exists in two teams' repos is answered the way the
chosen team uses it. team=None ("All teams") covers everything.

No embeddings or extra services: the index is rebuilt in memory when the knowledge version
changes (an admin edit or a scheduled refresh).
"""

import math
import re
from collections import Counter, defaultdict

from bot import config
from bot.knowledge_store import current_version, store, team_of
from bot.sections import FENCE_RE, HEADING_RE

CHUNK_CHARS = 5000
CATALOG_LINE_CHARS = 160
K1, B = 1.4, 0.75
STOPWORDS = set("""a an and are as at be by can do does for from has have how i if in is it its me my of on or
our so that the their them then there these this to us was we what when where which who why will with you
your kya hai ka ki ke se me mein ko""".split())

_index = {"version": None, "chunks": [], "df": Counter(), "avg": 1.0, "catalog": []}


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)          # createOrder -> create Order
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return [w for w in words if len(w) > 1 and w not in STOPWORDS]


def _title(name: str, text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else name.rsplit("/", 1)[-1].removesuffix(".md")


def _summary_line(text: str) -> str:
    """First real paragraph of the doc (after the title and metadata lines)."""
    for block in re.split(r"\n\s*\n", text):
        b = block.strip()
        if b and not b.startswith(("#", ">", "|", "-", "*", "```", "<!--")):
            return re.sub(r"\s+", " ", b)[:CATALOG_LINE_CHARS]
    return ""


def _chunks(name: str, text: str) -> list[dict]:
    title, out = _title(name, text), []
    section, buf, in_code = "", [], False

    def flush():
        body = "\n".join(buf).strip()
        while body:
            piece, body = body[:CHUNK_CHARS], body[CHUNK_CHARS:]
            if body:                                   # cut at a paragraph break when possible
                cut = piece.rfind("\n\n")
                if cut > CHUNK_CHARS // 2:
                    body, piece = piece[cut:] + body, piece[:cut]
            out.append({"doc": name, "team": team_of(name), "title": title, "section": section,
                        "text": piece.strip()})
        buf.clear()

    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_code = not in_code
        m = None if in_code else HEADING_RE.match(line)
        if m and len(m.group(1)) <= 3:
            flush()
            section = m.group(2).strip()
        buf.append(line)
    flush()
    return out


def _rebuild() -> None:
    s = store()
    chunks, catalog = [], []
    # Partner docs are indexed too (scope "partner"), only for researching documentation gaps:
    # they are always sent whole in the prompt, so answers never search them.
    for name in s.names():
        for c in _chunks(name, s.read(name) or ""):
            c["scope"] = "partner"
            c["tf"] = Counter(tokens(c["text"]) + tokens(f"{c['title']} {c['section']}") * 3)
            c["len"] = sum(c["tf"].values()) or 1
            chunks.append(c)
    for name in s.internal_names():
        text = s.read(name) or ""
        title = _title(name, text)
        line = f"- {name}: {title}" + (f". {_summary_line(text)}" if _summary_line(text) else "")
        catalog.append((team_of(name), line))
        for c in _chunks(name, text):
            c["scope"] = "internal"
            # Doc and section names count three times: "how does <repo> do X" should find that repo.
            head = tokens(f"{c['title']} {c['doc']} {c['section']}")
            c["tf"] = Counter(tokens(c["text"]) + head * 3)
            c["len"] = sum(c["tf"].values()) or 1
            chunks.append(c)
    df = Counter()
    for c in chunks:
        df.update(c["tf"].keys())
    _index.update(chunks=chunks, df=df, avg=(sum(c["len"] for c in chunks) / len(chunks)) if chunks else 1.0,
                  catalog=catalog)


def _ensure() -> None:
    version = current_version()
    if _index["version"] != version:
        _rebuild()
        _index["version"] = version


def _visible(doc_team: str | None, team: str | None) -> bool:
    return team is None or doc_team is None or doc_team == team


def catalog(team: str | None = None) -> str:
    """One line per doc the team can use. With "All teams", each line names its team."""
    _ensure()
    if team:
        return "\n".join(line for t, line in _index["catalog"] if _visible(t, team))
    return "\n".join(line.replace("- ", f"- [{t or 'shared'}] ", 1) for t, line in _index["catalog"])


def search(query: str, budget_chars: int | None = None, team: str | None = None,
           scope: str = "internal") -> list[dict]:
    """The best-matching sections the team can use, best first, within the character budget."""
    _ensure()
    budget = config.INTERNAL_CONTEXT_CHARS if budget_chars is None else budget_chars
    chunks, df, n = _index["chunks"], _index["df"], len(_index["chunks"])
    q = Counter(tokens(query))
    if not chunks or not q:
        return []
    scored = []
    for i, c in enumerate(chunks):
        if c["scope"] != scope or not _visible(c["team"], team):
            continue
        score = 0.0
        for term, qtf in q.items():
            tf = c["tf"].get(term)
            if not tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += qtf * idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * c["len"] / _index["avg"]))
        if score > 0:
            scored.append((score, i))
    picked, used = [], 0
    for score, i in sorted(scored, reverse=True):
        c = chunks[i]
        if used + len(c["text"]) > budget:
            continue
        picked.append({**{k: c[k] for k in ("doc", "team", "title", "section", "text")}, "score": round(score, 2)})
        used += len(c["text"])
        if used >= budget * 0.95:
            break
    return picked


def context_for(messages: list[dict], team: str | None = None) -> str:
    """The internal excerpts for a conversation, as one block for the prompt ("" if none)."""
    asked = [m["content"] for m in messages if m["role"] == "user"][-2:]
    hits = search("\n".join(asked), team=team)
    if not hits:
        return ""
    by_doc = defaultdict(list)
    for h in hits:
        by_doc[h["doc"]].append(h)
    parts = []
    for doc, items in by_doc.items():
        for h in items:
            parts.append(f'<excerpt doc="{doc}" section="{h["section"]}">\n{h["text"]}\n</excerpt>')
    return "<internal_knowledge>\n" + "\n\n".join(parts) + "\n</internal_knowledge>"
