"""Clean an exported Google Chat space into compact, redacted transcripts for Q&A extraction.

Reads  chat_export/output/<space_id>/messages.jsonl   (written by export_gchat.py)
Writes chat_export/output/<space_id>/clean/
  messages.jsonl   one cleaned message per line: short id, role, name, thread, text, source ids
  windows/*.txt    chronological transcript windows sized for one LLM call each
  index.json       window -> message range, size, estimated tokens
  report.md        what was dropped, merged and redacted, plus a leak check

Steps: tag roles -> redact secrets/PII -> compact curl and JSON -> drop noise ->
merge bursts -> assign short ids -> cut windows with context for late replies.

Usage:
  python chat_export/clean_chat.py --space AAAAxNg3VYI
"""

import argparse
import hashlib
import os
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- roles
# Names are kept out of the code (the repo is public): chat_export/private_names.json,
# git-ignored; see private_names.example.json.
#   askers           people who raise doubts in the group; everyone else is TEAM, except
#   unknown_senders  senders whose role the extraction model infers from context
#   client_names     partner company names, replaced with <CLIENT> so FAQ entries stay generic
# PRIVATE_NAMES_FILE overrides the location (Cloud Run mounts it from Secret Manager).
_PRIVATE = Path(os.environ.get("PRIVATE_NAMES_FILE") or HERE / "private_names.json")
if not _PRIVATE.exists():
    raise SystemExit(f"Missing {_PRIVATE}. Copy private_names.example.json and fill it in.")
_names = json.loads(_PRIVATE.read_text(encoding="utf-8"))
ASKERS = set(_names["askers"])
UNKNOWN_SENDERS = set(_names.get("unknown_senders", []))
CLIENT_NAMES = _names.get("client_names", [])

# ---------------------------------------------------------------- redaction
SECRET_KEYS = {"apikey", "api_key", "x-api-key", "password", "pwd", "token", "accesstoken",
               "access_token", "refreshtoken", "refresh_token", "authorization", "secret",
               "clientsecret", "client_secret", "otp"}
PII_KEYS = {"contactnumber", "mobile", "mobileno", "mobilenumber", "phone", "phonenumber",
            "email", "emailid", "houseno", "addressline1", "addressline2", "street", "landmark",
            "dob", "dateofbirth", "patientname", "firstname", "lastname", "externalpatientid",
            "doctorname", "referredby", "enteredby", "aadhar", "aadhaar", "pan", "address", "patientaddress", "orderby",
            "bookedby"}
# "name" is a person in patients[] but a product in items[]; product names are upper case.
NAME_KEYS = {"name", "fullname"}
PERSON_KEYS = {"patients", "patient", "beneficiaries", "beneficiary", "ben_details"}
PRODUCT_KEYS = {"items", "item", "products", "tests"}
# Same keys in any quoted key/value syntax: invalid JSON, PHP arrays ('k' => 'v'), Python dicts.
# Java toString dumps: address=..., orderBy=Some Name, (value runs to the next ", key=" or ")")
KV_EQ_PII_RE = re.compile(r"\b(%s)=(.*?)(?=,\s*\w+=|\)|\]|$)"
                          % "|".join(sorted(PII_KEYS | NAME_KEYS, key=len, reverse=True)), re.I | re.M)
ESCAPED_PII_RE = re.compile(r'(\\"(?:%s)\\"\s*:\s*\\")((?:(?!\\").)*)(\\")'
                            % "|".join(sorted(PII_KEYS | NAME_KEYS, key=len, reverse=True)), re.I)
PII_KV_RE = re.compile(r"""(['"])(%s)\1(\s*(?:=>|:)\s*)(['"])([^'"]*)\4"""
                       % "|".join(sorted(PII_KEYS | NAME_KEYS, key=len, reverse=True)), re.I)

# Payload size limits: enough to show the request shape and the error, not the whole body.
MAX_JSON_CHARS = 1200
MAX_MESSAGE_CHARS = 2500
MAX_JSON_DEPTH = 4
KEEP_JSON_KEYS = {"errors", "error", "message", "errormessage", "alertmessage", "code", "status"}

# Long random-looking strings (API keys, base64 secrets): 24+ chars mixing upper, lower and digits.
HIGH_ENTROPY_RE = re.compile(r"(?<![\w<])(?=[A-Za-z0-9+=-]*\d)(?=[A-Za-z0-9+=-]*[A-Z])(?=[A-Za-z0-9+=-]*[a-z])"
                             r"[A-Za-z0-9+=-]{24,}(?![\w])")

TEXT_RULES = [  # (pattern, replacement, counter label) applied in order
    (re.compile(r"Bearer\s+[^\s'\"]+"), "Bearer <TOKEN>", "token"),
    (re.compile(r"eyJ[\w-]{8,}\.[\w-]+(?:\.[\w-]+)?"), "<TOKEN>", "token"),
    (re.compile(r"(?i)(partner-id\s*:\s*)[^\s'\"]+"), r"\1<PARTNER_ID>", "partner_id"),
    (re.compile(r"(?i)(\"?(?:%s)\"?\s*[:=]\s*)(?!Bearer\b|\"?<)(\"[^\"]*\"|'[^']*'|[^\s,}']+)"
                % "|".join(re.escape(k) for k in sorted(SECRET_KEYS, key=len, reverse=True))),
     r'\1"<SECRET>"', "secret"),
    (re.compile(r"(?i)(header value\s*:\s*)\S+"), r"\1<SECRET>", "secret"),
    (re.compile(r"(?i)\b(password|pwd|passcode)(\s*(?:-|is|:|=)\s*)(?!\"?<)\S+"), r"\1\2<SECRET>", "secret"),
    (re.compile(r"(?i)(order\.svc/)[^/\s]+"), r"\1<SECRET>", "secret"),  # B2C API key in the URL path
    (re.compile(r"(?i)(ReportAccess\.aspx\?)\S+"), r"\1<SECRET>", "secret"),  # report access token
    (re.compile(r"(?i)(COMMON\.svc/)[^/\s]+/[^/\s]+"), r"\1<USER>/<SECRET>", "secret"),  # login user/password in path
    (HIGH_ENTROPY_RE, "<SECRET>", "secret"),
    (re.compile(r"(?i)<(name|mobile|email|address|dob)>[^<]*</\1>"), r"<\1><PII></\1>", "pii_field"),  # XML
    (re.compile(r"https?://(?:meet\.google\.com|tel\.meet)/\S+"), "<MEET_LINK>", "meet_link"),
    (re.compile(r"https?://docs\.google\.com/\S+"), "<DOC_LINK>", "doc_link"),
    (re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*(?:X-Amz|Signature|token)[^\s'\"]*"),
     r"\1?<SIGNED_QUERY>", "signed_url"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<EMAIL>", "email"),
    (re.compile(r"(?<![\w-])(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"), "<PHONE>", "phone"),
    (re.compile(r"\bVL[0-9A-F]{6}\b"), "<ORDER_ID>", "order_id"),
    (re.compile(r"\bSP\d{7,}\b"), "<BARCODE>", "barcode"),
    (re.compile(r"\bPROJ\d{5,}\b"), "<PROJ_ID>", "proj_id"),
    (re.compile(r"(?<![\w<-])\d{8,}(?![\w>])"), "<ID>", "long_number"),
    (re.compile(r"(?i)\b(?:%s)\b" % "|".join(re.escape(c) for c in CLIENT_NAMES)),
     "<CLIENT>", "client_name"),
]

# Leak check run on the final output; any hit is listed in report.md.
LEAK_PATTERNS = {
    "jwt": re.compile(r"eyJ[\w-]{8,}\.[\w-]+"),
    "bearer": re.compile(r"Bearer\s+(?!<TOKEN>)\S+"),
    "phone": re.compile(r"(?<![\w-])(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
    "high_entropy": HIGH_ENTROPY_RE,
    "secret_value": re.compile(r"(?i)\"(?:apikey|password|token)\"\s*:\s*\"(?!<SECRET>)[^\"]+\""),
}

# ---------------------------------------------------------------- noise
NOISE_PHRASES = {
    "ok", "okay", "okk", "ack", "ackd", "acked", "sure", "thanks", "thank you", "thanks a lot",
    "thank you so much", "thanks team", "noted", "checking", "will check", "let me check",
    "hi", "hello", "hi team", "hello team", "great", "cool", "fine", "got it", "on it",
    "1",  # "+1" after punctuation is stripped
}
NUDGE_PHRASES = {
    "any update", "any update on this", "any updates", "any update here", "update",
    "can anyone check this", "can we check this", "is anyone checking this", "please check",
    "pls check", "plz check", "please check this", "pls check this", "gentle reminder",
    "reminder", "can someone check", "can someone check this", "please help", "pls help",
    "can you help", "can you please help", "please look into this", "any update team",
}
MENTION_RE = re.compile(r"@[⁨⁩]*([A-Za-z]+)(?:[ .][A-Z][a-z]+)*[⁨⁩]*")
MERGE_GAP_SECONDS = 180


def first_name(sender):
    name = sender.split("<")[0].strip()
    if name.startswith("users/"):
        return "Unknown-" + name[-4:]
    if "@" in name:
        name = name.split("@")[0].split(".")[0]
    return name.split()[0].capitalize() if name else "Unknown"


def role_of(sender):
    name = sender.split("<")[0].strip()
    if name in ASKERS:
        return "ASK"
    if name in UNKNOWN_SENDERS or name.startswith("users/"):
        return "UNK"
    return "TEAM"


# ---------------------------------------------------------------- text cleanup
def redact_json(value, counts, parent_key="", in_person=False):
    """in_person: inside a patients/beneficiaries object, where every "name" is a person,
    even upper case. Product names live under "items", which resets it."""
    if isinstance(value, dict):
        return {k: redact_json(v, counts, k, in_person and k.lower() not in PRODUCT_KEYS
                               or k.lower() in PERSON_KEYS)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v, counts, parent_key, in_person) for v in value]
    key = parent_key.lower()
    if isinstance(value, (str, int)) and value not in ("", None):
        if key in SECRET_KEYS:
            counts["secret"] += 1
            return "<SECRET>"
        if key in PII_KEYS:
            counts["pii_field"] += 1
            return "<PII>"
        if key in NAME_KEYS and isinstance(value, str) and (in_person or not value.isupper()):
            counts["person_name"] += 1
            return "<NAME>"
    return value


def json_spans(text):
    """Yield (start, end) of top-level balanced {...} or [...] blocks."""
    i, n = 0, len(text)
    while i < n:
        if text[i] in "{[":
            stack, j, in_str = [text[i]], i + 1, False
            while j < n and stack:
                c = text[j]
                if in_str:
                    if c == "\\":
                        j += 1
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c in "{[":
                    stack.append(c)
                elif c in "}]":
                    stack.pop()
                j += 1
            if not stack or j >= n:  # j >= n: payload cut off mid-paste, take the rest
                yield i, j
                i = j
                continue
        i += 1


def prune_json(value, depth=0):
    """Shrink a payload for the reader: first item of long lists, short strings, shallow nesting.
    Error fields are never pruned, because they usually are the question."""
    if isinstance(value, dict):
        if depth >= MAX_JSON_DEPTH:
            return "{…}"
        return {k: (v if k.lower() in KEEP_JSON_KEYS else prune_json(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return value
        head = [prune_json(value[0], depth + 1)]
        return head + ([f"…+{len(value) - 1} more"] if len(value) > 1 else [])
    if isinstance(value, str) and len(value) > 160:
        return value[:160] + "…"
    return value


def minify(block):
    """Remove whitespace outside string literals; works on invalid JSON too."""
    out, in_str, esc = [], False, False
    for c in block:
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif not c.isspace():
            out.append(c)
    return "".join(out)


def truncate_block(block, counts, limit=MAX_JSON_CHARS, label="json_truncated"):
    """Keep the head of a long block, plus the first error/message field after the cut."""
    if len(block) <= limit:
        return block
    counts[label] += 1
    keep = block[:limit]
    m = re.search(r"""["']?(?:errors?|message|errorMessage|alertMessage|exception)["']?\s*(?::|=>)""",
                  block[limit:], re.I)
    if m:
        s = limit + m.start()
        keep += f"…[{s - limit} chars cut]…" + block[s:s + 400]
        cut = len(block) - s - 400
    else:
        cut = len(block) - limit
    return keep + (f"…[{cut} chars cut]" if cut > 0 else "")


def redact_kv(match, counts):
    q1, key, sep, q2, value = match.groups()
    if not value or value.startswith("<") or (key.lower() in NAME_KEYS and value.isupper()):
        return match.group(0)
    counts["pii_field"] += 1
    return f"{q1}{key}{q1}{sep}{q2}<PII>{q2}"


def compact_json(text, counts):
    out, last = [], 0
    for start, end in json_spans(text):
        block = text[start:end]
        if len(block) < 60:
            continue
        out.append(text[last:start])
        try:
            parsed = prune_json(redact_json(json.loads(block), counts))
            block = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        except ValueError:
            block = minify(block)
        counts["json_compacted"] += 1
        out.append(truncate_block(block, counts))
        last = end
    out.append(text[last:])
    return "".join(out)


def compact_curl(text):
    if "curl" not in text and "postman request" not in text:
        return text
    text = re.sub(r"\\\s*\n\s*", " ", text)
    text = re.sub(r"--location\s+", "", text)
    text = re.sub(r"--request\s+", "-X ", text)
    text = re.sub(r"--header\s+", "-H ", text)
    text = re.sub(r"--data(?:-raw)?\s+", "-d ", text)
    # Headers that never matter for the question.
    text = re.sub(r"-H\s+'(?:User-Agent|Request-Id|Content-Type|Accept|Cookie|Postman-Token)[^']*'\s*",
                  "", text, flags=re.I)
    return text


def compact_php(text):
    """PHP print_r/var_export dumps: one line, no array() noise."""
    if "=>" not in text:
        return text
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\barray\s*\(", "(", text)
    return re.sub(r"\s*=>\s*", "=>", text)


def clean_text(raw, counts):
    text = raw.replace("\u2068", "").replace("\u2069", "")
    text = compact_curl(text)
    text = compact_php(text)
    text = compact_json(text, counts)
    text = PII_KV_RE.sub(lambda m: redact_kv(m, counts), text)
    text = KV_EQ_PII_RE.sub(lambda m: m.group(1) + "=<PII>" if not m.group(2).startswith("<") else m.group(0), text)
    text = ESCAPED_PII_RE.sub(lambda m: m.group(0) if m.group(2).startswith("<") or (
                              m.group(2).isupper() and m.group(0).split('"')[1].lower() in NAME_KEYS)
                              else m.group(1) + "<PII>" + m.group(3), text)
    for pattern, repl, label in TEXT_RULES:
        text, n = pattern.subn(repl, text)
        counts[label] += n
    text = MENTION_RE.sub(lambda m: "@" + m.group(1).capitalize(), text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return truncate_block(text.strip(), counts, MAX_MESSAGE_CHARS, "message_truncated")


def classify_short(text):
    """Return 'noise', 'nudge' or None for a cleaned message."""
    bare = MENTION_RE.sub("", text)
    bare = re.sub(r"[^\w\s]", "", bare).strip().lower()
    bare = re.sub(r"\s+", " ", bare)
    if not bare:
        return "noise"  # mention-only or emoji-only
    if bare in NUDGE_PHRASES or re.fullmatch(r"(any )?updates?( on this| here| team)*", bare):
        return "nudge"
    if bare in NOISE_PHRASES:
        return "noise"
    return None


def meeting_invite(text):
    if "<MEET_LINK>" not in text:
        return None
    lines = [l for l in text.splitlines() if l.strip() and "<MEET_LINK>" not in l
             and not re.search(r"(?i)time zone|joining info|video call|join by phone|more phone|pin:", l)]
    return "[meeting invite] " + " / ".join(lines[:3])


def attachment_note(names):
    names = [n for n in names if n]
    if not names:
        return ""
    images = sum(1 for n in names if re.search(r"(?i)\.(png|jpe?g|gif|webp|heic)$|^image", n))
    others = [n for n in names if not re.search(r"(?i)\.(png|jpe?g|gif|webp|heic)$|^image", n)]
    parts = (["image"] * images) + others
    return " [attachment: " + ", ".join(parts) + "]"


# ---------------------------------------------------------------- pipeline
def load(space_dir):
    with (space_dir / "messages.jsonl").open() as f:
        return [json.loads(line) for line in f]


def clean(messages, counts):
    by_id = {}
    kept = []
    nudges = Counter()
    last_ask_by_sender = {}
    seen_blocks = {}

    for m in messages:
        counts["input_messages"] += 1
        if m.get("deleted"):
            counts["dropped_deleted"] += 1
            continue
        root = m["thread"] or m["id"]
        role = role_of(m["sender"])
        text = clean_text(m["text"] or "", counts)
        invite = meeting_invite(text)
        if invite:
            text = invite
            counts["meeting_invites"] += 1
        kind = classify_short(text) if len(text) < 60 and not m["attachments"] else None
        if kind == "nudge":
            counts["dropped_nudge"] += 1
            target = root if m["is_reply"] else last_ask_by_sender.get(m["sender"], root)
            nudges[target] += 1
            continue
        if kind == "noise":
            counts["dropped_noise"] += 1
            continue
        if not text and not m["attachments"]:
            counts["dropped_empty"] += 1
            continue

        # The same long payload pasted again is replaced with a pointer.
        for start, end in list(json_spans(text))[::-1]:
            block = text[start:end]
            if len(block) < 300:
                continue
            digest = hashlib.sha1(block.encode()).hexdigest()
            if digest in seen_blocks:
                text = text[:start] + f"[same payload as {seen_blocks[digest]}]" + text[end:]
                counts["repeated_payloads"] += 1
            else:
                seen_blocks[digest] = m["id"]  # resolved to a short id below

        rec = {
            "src_ids": [m["id"]],
            "time": m["create_time"],
            "role": role,
            "name": first_name(m["sender"]),
            "sender": m["sender"],
            "root": root,
            "is_reply": m["is_reply"],
            "quoted": m.get("quoted_message"),
            "text": text + clean_text(attachment_note(m["attachments"]), counts),
        }

        prev = kept[-1] if kept else None
        if (prev and prev["sender"] == rec["sender"] and prev["root"] == rec["root"]
                and prev["is_reply"] == rec["is_reply"] and not rec["quoted"]
                and (ts(rec) - ts(prev)).total_seconds() <= MERGE_GAP_SECONDS):
            prev["text"] += "\n" + rec["text"]
            prev["src_ids"] += rec["src_ids"]
            counts["merged_bursts"] += 1
            by_id[m["id"]] = prev
            continue

        kept.append(rec)
        by_id[m["id"]] = rec
        if role == "ASK" and not m["is_reply"]:
            last_ask_by_sender[m["sender"]] = root

    # Short ids in time order, then resolve thread roots, quotes and payload pointers.
    width = len(str(len(kept)))
    for i, rec in enumerate(kept, 1):
        rec["id"] = f"m{i:0{width}d}"
    short = {src: by_id[src]["id"] for src in by_id}
    for rec in kept:
        root_rec = by_id.get(rec["root"])
        rec["thread"] = root_rec["id"] if root_rec else None
        rec["reply_to_thread"] = rec["thread"] if rec["is_reply"] and rec["thread"] != rec["id"] else None
        rec["quoted"] = short.get(rec["quoted"]) if rec["quoted"] else None
        rec["nudges"] = nudges.get(rec["root"], 0) if rec["thread"] == rec["id"] else 0
        rec["text"] = re.sub(r"\[same payload as (spaces/[^\]]+)\]",
                             lambda mm: f"[same payload as {short.get(mm.group(1), 'earlier message')}]",
                             rec["text"])
    counts["output_messages"] = len(kept)
    return kept


def ts(rec):
    return datetime.fromisoformat(rec["time"].replace("Z", "+00:00"))


def render(rec, context=False):
    stamp = ts(rec).astimezone(IST).strftime("%Y-%m-%d %H:%M")
    links = []
    if rec["reply_to_thread"]:
        links.append(f"↳{rec['reply_to_thread']}")
    if rec["quoted"]:
        links.append(f"quoting {rec['quoted']}")
    link = f" ({', '.join(links)})" if links else ""
    nudge = f" [nudged {rec['nudges']}x]" if rec["nudges"] else ""
    body = rec["text"].replace("\n", "\n    ")
    prefix = "~ " if context else ""
    return f"{prefix}{rec['id']} {stamp} {rec['role']} {rec['name']}{link}{nudge}: {body}"


def cut_windows(recs, target_chars):
    """Greedy windows of ~target_chars, cut at the biggest time gap in the last third."""
    windows, start = [], 0
    while start < len(recs):
        size, end = 0, start
        while end < len(recs) and (size < target_chars or end == start):
            size += len(render(recs[end])) + 1
            end += 1
        if end < len(recs):
            lo = start + max(1, (end - start) * 2 // 3)
            gaps = [((ts(recs[i]) - ts(recs[i - 1])).total_seconds(), i) for i in range(lo, end)]
            if gaps:
                end = max(gaps)[1]
        windows.append((start, end))
        start = end
    return windows


def window_context(recs, start, end, by_short, context_hours, context_chars):
    """Earlier messages the window needs to make sense: the last few hours before it,
    plus thread roots and quoted messages that late replies point back to."""
    needed = {}
    first_time = ts(recs[start])
    size = 0
    for i in range(start - 1, -1, -1):
        if first_time - ts(recs[i]) > timedelta(hours=context_hours) or size > context_chars:
            break
        needed[recs[i]["id"]] = recs[i]
        size += len(recs[i]["text"])
    in_window = {r["id"] for r in recs[start:end]}
    for r in recs[start:end]:
        for ref in (r["reply_to_thread"], r["quoted"]):
            if ref and ref not in in_window and ref in by_short:
                needed[ref] = by_short[ref]
    return sorted(needed.values(), key=lambda r: r["id"])


def leak_check(text):
    hits = {}
    for label, pattern in LEAK_PATTERNS.items():
        found = pattern.findall(text)
        if found:
            hits[label] = found[:5]
    pii = [m.group(0) for m in PII_KV_RE.finditer(text)
           if m.group(5) and not m.group(5).startswith("<")
           and not (m.group(2).lower() in NAME_KEYS and m.group(5).isupper())]
    if pii:
        hits["pii_field"] = pii[:5]
    return hits


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--space", required=True, help="space id, e.g. AAAAxNg3VYI")
    p.add_argument("--window-chars", type=int, default=24000, help="target size per window (~6-7k tokens)")
    p.add_argument("--context-hours", type=int, default=12, help="hours of earlier chat shown as context")
    p.add_argument("--context-chars", type=int, default=4000, help="cap on that earlier context")
    args = p.parse_args()

    space_dir = HERE / "output" / args.space.removeprefix("spaces/")
    out_dir = space_dir / "clean"
    win_dir = out_dir / "windows"
    win_dir.mkdir(parents=True, exist_ok=True)
    for old in win_dir.glob("*.txt"):
        old.unlink()

    counts = Counter()
    messages = load(space_dir)
    raw_chars = sum(len(m["text"] or "") for m in messages)
    recs = clean(messages, counts)
    by_short = {r["id"]: r for r in recs}

    with (out_dir / "messages.jsonl").open("w") as f:
        for r in recs:
            f.write(json.dumps({k: r[k] for k in ("id", "time", "role", "name", "thread", "reply_to_thread",
                                                  "quoted", "nudges", "text", "src_ids")},
                               ensure_ascii=False) + "\n")

    index, all_text = [], []
    windows = cut_windows(recs, args.window_chars)
    for n, (start, end) in enumerate(windows, 1):
        ctx = window_context(recs, start, end, by_short, args.context_hours, args.context_chars)
        first, last = recs[start], recs[end - 1]
        day = lambda r: ts(r).astimezone(IST).strftime("%Y-%m-%d")
        header = (f"# Window {n}/{len(windows)} | {day(first)} to {day(last)} | "
                  f"messages {first['id']}-{last['id']}\n")
        body = []
        if ctx:
            body.append("## CONTEXT (earlier messages, for understanding only; do not extract Q&A that starts here)")
            body += [render(r, context=True) for r in ctx]
            body.append("")
        body.append("## MESSAGES")
        body += [render(r) for r in recs[start:end]]
        text = header + "\n".join(body) + "\n"
        name = f"{n:03d}_{day(first)}_to_{day(last)}.txt"
        (win_dir / name).write_text(text, encoding="utf-8")
        all_text.append(text)
        index.append({"file": name, "first": first["id"], "last": last["id"],
                      "messages": end - start, "context_messages": len(ctx),
                      "chars": len(text), "est_tokens": len(text) // 3})
    (out_dir / "index.json").write_text(json.dumps(index, indent=2))

    joined = "\n".join(all_text)
    leaks = leak_check(joined)
    roles = Counter(r["role"] for r in recs)
    total_tokens = sum(w["est_tokens"] for w in index)
    report = [
        f"# Cleaning report: {args.space}",
        "",
        f"- Input: {counts['input_messages']} messages, {raw_chars:,} chars",
        f"- Output: {len(recs)} messages ({roles['ASK']} ASK, {roles['TEAM']} TEAM, {roles['UNK']} UNK), "
        f"{len(windows)} windows, {len(joined):,} chars, ~{total_tokens:,} tokens",
        "",
        "## Dropped and merged",
        f"- Noise (ack/ok/thanks/mention-only): {counts['dropped_noise']}",
        f"- Nudges (any update?/please check), counted on the question as [nudged Nx]: {counts['dropped_nudge']}",
        f"- Empty: {counts['dropped_empty']}",
        f"- Bursts merged (same person, same thread, within {MERGE_GAP_SECONDS}s): {counts['merged_bursts']}",
        f"- Meeting invites shortened: {counts['meeting_invites']}",
        f"- Repeated payloads replaced with a pointer: {counts['repeated_payloads']}",
        f"- JSON blocks compacted: {counts['json_compacted']}",
        f"- Long payloads cut (error fields kept): {counts['json_truncated']}",
        f"- Long messages cut (over {MAX_MESSAGE_CHARS} chars): {counts['message_truncated']}",
        "",
        "## Redacted",
        *[f"- {k}: {counts[k]}" for k in ("token", "secret", "partner_id", "pii_field", "person_name", "email",
                                          "phone", "order_id", "barcode", "proj_id", "long_number",
                                          "client_name", "meet_link", "doc_link", "signed_url")],
        "",
        "## Leak check",
        "No leaks found." if not leaks else "\n".join(f"- {k}: {v}" for k, v in leaks.items()),
        "",
        "## Windows",
        "| File | Messages | Context | ~Tokens |",
        "|---|---|---|---|",
        *[f"| {w['file']} | {w['messages']} | {w['context_messages']} | {w['est_tokens']:,} |" for w in index],
    ]
    (out_dir / "report.md").write_text("\n".join(report) + "\n")
    print("\n".join(report[:22]))
    print(f"\nWrote {out_dir}")


if __name__ == "__main__":
    main()
