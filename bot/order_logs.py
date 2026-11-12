"""Live order logs from the log search for internal users' questions.

When a team member asks about an order ("why is order 332481535602757632 stuck?"), the bot fetches
that order's log lines from the log search and puts a short timeline into the prompt, so the AI can say what
happened and where it failed. Partners never get this: logs show internal services, and a partner
could look up someone else's order. The lookup runs only for `user.is_internal`.

How it works:
    order_ids()   finds order / lead IDs in the latest question (needs a word like "order" too, so a
                  random number isn't looked up; 10-digit mobile numbers are skipped).
    search()      one Elasticsearch query through the log UI's console proxy (or straight to
                  Elasticsearch when ELASTICSEARCH_URL is set), with the API key or a read-only user's login.
    context_for() the block for the prompt: newest LOGS_MAX_HITS lines, oldest first, each line
                  time | level | service | message, trimmed, passed through scrub_secrets() (tokens,
                  keys, emails, phone numbers) before the AI sees it. Errors come back as a short
                  note ("log search not reachable") so the answer can say so.
Results are kept for 2 minutes, so follow-up questions don't query the log search again.
"""

import json
import logging
import re
import time
from urllib.parse import quote

import requests

from bot import config
from bot.guardrails import scrub_secrets

log = logging.getLogger("bot.order_logs")

CACHE_SECONDS = 120
MAX_IDS = 3
LOOKBACK_MESSAGES = 4          # user messages to look back for an ID in follow-ups
MESSAGE_CHARS = 600
TRIGGER = re.compile(r"\b(order|orders|lead|booking|ref|logs?|status|stuck|fail|failed|failing|error|track|kya hua|kahan|happened|why|cancel\w*|reschedul\w*|collected|report)\b", re.I)
MOBILE = re.compile(r"^(?:91)?[6-9]\d{9}$")
TIME_FIELDS = ("@timestamp", "timestamp", "time", "ts")
LEVEL_FIELDS = ("log.level", "level", "severity", "loglevel", "log_level")
SERVICE_FIELDS = ("service.name", "service", "app", "application", "kubernetes.container.name",
                  "container.name", "logger_name", "logger")
PERSONAL = re.compile(
    r'(?i)("?\b(?:(?:customer|patient|ben|user|first|last|full|contact)_?)?name"?\s*[:=]\s*'
    r'|"?\b\w*address"?\s*[:=]\s*|"?\b\w*(?:email|mailid)(?:id)?"?\s*[:=]\s*'
    r'|"?\b\w*(?:mobile|phone)(?:no|number)?"?\s*[:=]\s*|"?\bdob"?\s*[:=]\s*)'
    r'("[^"]*"|[^,}\]|\n]*)')
MESSAGE_FIELDS = ("message", "msg", "log", "error.message", "event.original")

_cache: dict[tuple, tuple[float, str]] = {}


def enabled_for(team: str | None) -> bool:
    if not ((config.LOGS_API_KEY or (config.LOGS_USERNAME and config.LOGS_PASSWORD))
            and (config.LOGS_URL or config.ELASTICSEARCH_URL)):
        return False
    return not config.ORDER_LOOKUP_TEAMS or (team or "") in config.ORDER_LOOKUP_TEAMS


def index_for(team: str | None) -> str:
    return config.LOGS_INDEX_BY_TEAM.get(team or "", config.LOGS_INDEX)


def order_ids(messages: list[dict]) -> list[str]:
    """IDs from the latest user message that has one, among the last few. An ID counts when its
    message is about an order ("order X failed?"), is just the ID ("CTLSP84519603"), or the latest
    message is an order question about it ("CTLSP84519603" … then "what's its status?")."""
    asked = [m["content"] for m in messages if m["role"] == "user"][-LOOKBACK_MESSAGES:]
    latest_asks = bool(asked) and bool(TRIGGER.search(asked[-1]))
    for text in reversed(asked):
        ids = [i for i in dict.fromkeys(config.ORDER_ID_PATTERN.findall(text)) if not MOBILE.match(i)]
        if not ids:
            continue
        only_ids = not config.ORDER_ID_PATTERN.sub("", re.sub(r"[\s,.:;#?!-]|\b(?:id|no|hai|ka|ki|ke)\b", "", text, flags=re.I))
        if TRIGGER.search(text) or only_ids or latest_asks:
            return ids[:MAX_IDS]
        return []          # newest ID isn't about an order (e.g. a pasted number): don't look up older ones
    return []


def search(order_id: str, index: str) -> dict:
    """The raw Elasticsearch response for one order ID (raises on HTTP / network errors)."""
    query = {"query_string": {"query": f'"{order_id}"', "lenient": True}}
    if config.LOGS_ORDER_FIELDS:
        query["query_string"]["fields"] = config.LOGS_ORDER_FIELDS
    body = {
        "size": config.LOGS_MAX_HITS,
        "track_total_hits": True,
        "sort": [{config.LOGS_TIME_FIELD: {"order": "desc", "unmapped_type": "date"}}],
        "query": {"bool": {"filter": [
            query,
            {"range": {config.LOGS_TIME_FIELD: {"gte": f"now-{config.LOGS_LOOKBACK_DAYS}d"}}},
        ]}},
    }
    headers = {"Content-Type": "application/json"}
    auth = None
    if config.LOGS_API_KEY:
        headers["Authorization"] = f"ApiKey {config.LOGS_API_KEY}"
    else:
        auth = (config.LOGS_USERNAME, config.LOGS_PASSWORD)
    if config.ELASTICSEARCH_URL:
        url = f"{config.ELASTICSEARCH_URL}/{quote(index, safe='*,-_.')}/_search"
    else:
        url = f"{config.LOGS_URL}/api/console/proxy?path={quote(index + '/_search', safe='')}&method=POST"
        headers["kbn-xsrf"] = "true"
    r = requests.post(url, headers=headers, auth=auth, data=json.dumps(body), timeout=config.LOGS_TIMEOUT_SECONDS)
    if r.status_code in (401, 403):
        raise PermissionError(f"The log search refused the login (HTTP {r.status_code})")
    r.raise_for_status()
    return r.json()


def _flat(doc: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in doc.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def _pick(doc: dict, names: tuple) -> str:
    for n in names:
        v = doc.get(n)
        if v not in (None, "", [], {}):
            return v if isinstance(v, str) else json.dumps(v, default=str)
    return ""


def _hide_personal(text: str) -> str:
    """Values of personal fields (names, addresses, emails, mobiles) in logged payloads."""
    return PERSONAL.sub(lambda m: m.group(1) + '"<hidden>"', text)


def _parts(hit: dict) -> tuple[str, str, str, str]:
    """(time, level, service, message) of one log line."""
    doc = _flat(hit.get("_source", {}))
    message = _pick(doc, MESSAGE_FIELDS)
    if not message:      # no message field: show the remaining fields instead
        message = json.dumps({k: v for k, v in doc.items() if k not in TIME_FIELDS}, default=str)
    message = re.sub(r"\s+", " ", scrub_secrets(_hide_personal(message))).strip()
    if len(message) > MESSAGE_CHARS:
        message = message[:MESSAGE_CHARS] + " …"
    service = _pick(doc, SERVICE_FIELDS)
    if not service:      # data stream ".ds-logs-<service>-<date>-000123" names the service
        m = re.match(r"\.?(?:ds-)?logs-(.+?)-\d{4}\.\d{2}\.\d{2}", hit.get("_index", ""))
        service = m.group(1) if m else "-"
    return _pick(doc, TIME_FIELDS), _pick(doc, LEVEL_FIELDS) or "-", service, message


def logs_block(order_id: str, index: str) -> str:
    """One <order_logs> block for the prompt, also for lookups that failed or found nothing."""
    key = (order_id, index)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    head = f'<order_logs order_id="{order_id}" lookback_days="{config.LOGS_LOOKBACK_DAYS}"'
    try:
        res = search(order_id, index)
    except PermissionError as e:
        return f"{head} status=\"error\">{e}. An admin needs to check the LOGS_* login settings.</order_logs>"
    except requests.RequestException as e:
        log.warning("Log lookup for %s failed: %s", order_id, type(e).__name__)
        reason = "timed out" if isinstance(e, requests.Timeout) else "not reachable (VPN off or log search down?)"
        return f"{head} status=\"error\">Log search {reason}. No logs were checked.</order_logs>"
    hits = res.get("hits", {}).get("hits", [])
    total = res.get("hits", {}).get("total", {})
    total = total.get("value", len(hits)) if isinstance(total, dict) else total
    # Newest first until the size cap; the same message repeated (polling) is shown once with a count.
    seen: dict[tuple, list] = {}
    size = 0
    for h in hits:
        when, level, service, message = _parts(h)
        same = (level, service, message)
        if same in seen:
            seen[same][0] = when                   # keep the earliest time, count the repeats
            seen[same][4] += 1
            continue
        if size + len(message) > config.LOGS_MAX_CHARS:
            break
        seen[same] = [when, level, service, message, 1]
        size += len(message)
    rows = sorted(seen.values(), key=lambda r: r[0])
    lines = [" | ".join(r[:4]) + (f" (×{r[4]})" if r[4] > 1 else "") for r in rows]
    if not lines:
        block = f"{head} status=\"not_found\">No log lines mention this ID in the last {config.LOGS_LOOKBACK_DAYS} days.</order_logs>"
    else:
        shown = sum(r[4] for r in rows)
        note = (f"latest {shown} of {total} lines" if total > shown else f"all {total} lines") + \
            ", oldest first, repeats counted as (×n)"
        block = (f"{head} status=\"found\" total=\"{total}\">\n({note}; columns: time | level | service | message)\n"
                 + "\n".join(lines) + "\n</order_logs>")
    _cache[key] = (time.monotonic(), block)
    if len(_cache) > 200:
        _cache.pop(next(iter(_cache)))
    return block


def context_for(messages: list[dict], team: str | None) -> tuple[str, list[str]]:
    """(prompt block, order IDs looked up). Empty when the log search isn't set up or no ID was asked about."""
    if not enabled_for(team):
        return "", []
    ids = order_ids(messages)
    if not ids:
        return "", []
    index = index_for(team)
    return "\n\n".join(logs_block(i, index) for i in ids), ids
