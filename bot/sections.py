"""Merge an updated section into an existing Markdown knowledge file.

An admin pastes one or more sections, each starting with a heading:

    ## 5. Slots
    ...new text...

Each section replaces the section with the same heading in the file (same title, ignoring
case, numbering and formatting), up to the next heading of the same or a higher level.
A section whose heading isn't found is added at the end of the file.
"""

import difflib
import re

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _norm(title: str) -> str:
    t = re.sub(r"[*_`]", "", title).lower()
    t = re.sub(r"^\s*(?:section\s+)?\d+(?:\.\d+)*[.)]?\s+", "", t)   # "3.1 ", "5) ", "Section 2 "
    return re.sub(r"\s+", " ", t).strip()


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, normalised title) for every heading outside code blocks."""
    out, in_code = [], False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_code = not in_code
        elif not in_code and (m := HEADING_RE.match(line)):
            out.append((i, len(m.group(1)), _norm(m.group(2))))
    return out


def split_sections(update: str) -> list[tuple[int, str, list[str]]]:
    """Split pasted text into (level, title, lines) at headings of the first heading's level."""
    lines = update.strip("\n").splitlines()
    heads = _headings(lines)
    if not heads or heads[0][0] != next(i for i, l in enumerate(lines) if l.strip()):
        raise ValueError("The update must start with a Markdown heading, for example '## Slots'.")
    level = heads[0][1]
    starts = [(i, t) for i, lv, t in heads if lv == level]
    sections = []
    for n, (start, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = lines[start:end]
        while body and not body[-1].strip():
            body.pop()
        sections.append((level, title, body))
    return sections


def apply_update(doc: str, update: str) -> tuple[str, list[dict]]:
    """Return the new document and what happened to each pasted section."""
    lines = doc.splitlines()
    actions = []
    for level, title, body in split_sections(update):
        heads = _headings(lines)
        match = next(((i, lv) for i, lv, t in heads if t == title and lv == level), None) \
            or next(((i, lv) for i, lv, t in heads if t == title), None)
        if match:
            start, lv = match
            end = next((i for i, l, _ in heads if i > start and l <= lv), len(lines))
            # Keep the blank line that separated this section from the next one.
            tail = [""] if end < len(lines) else []
            old_len = end - start
            lines[start:end] = body + tail
            actions.append({"section": body[0].lstrip("# ").strip(), "action": "replaced",
                            "old_lines": old_len, "new_lines": len(body)})
        else:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += [""] + body
            actions.append({"section": body[0].lstrip("# ").strip(), "action": "added",
                            "old_lines": 0, "new_lines": len(body)})
    return "\n".join(lines).rstrip("\n") + "\n", actions


def unified_diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{name} (live)", tofile=f"{name} (updated)", n=2))
