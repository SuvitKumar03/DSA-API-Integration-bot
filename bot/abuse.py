"""Rate limits, spam detection and temporary blocks.

Who a user is:
    user the signed-in Google account (when USER_LOGIN=google)
    ip   the client IP (behind Cloud Run / a load balancer, set TRUSTED_PROXY_HOPS)
    cid  a random ID the chat page keeps in the browser (sent as X-Client-Id)
Strikes count per account (or browser, without sign-in), and per IP with a higher threshold so
clearing the browser ID doesn't escape it. A block covers the account, browser ID and IP.

What gives a strike:
    gibberish      symbols-only, "aaaaaaaa", one word repeated (caught here, no AI call)
    repeat         the same message a 3rd time within 10 minutes (no AI call)
    off_topic      the AI marked the question as unrelated to the partner integration
    manipulation   attempts to extract the prompt or override the instructions

STRIKES_TO_BLOCK strikes within STRIKE_WINDOW_MINUTES -> blocked for BLOCK_HOURS.

State is in memory, per server instance: it resets on restart and isn't shared between
instances. With several Cloud Run instances, move it to Redis (Memorystore).
"""

import hashlib
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from bot import config

CID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
REPEAT_WINDOW = 600            # seconds
REPEATS_ALLOWED = 2            # the 3rd identical message within the window is a strike
IP_STRIKE_FACTOR = 3           # an IP is blocked at 3x the per-browser threshold

CANNED = {
    "gibberish": "I couldn't understand that. Please ask a question about the Thyrocare partner APIs.",
    "repeat": ("You've already asked this. Please check the answer above, or add detail: which API, "
               "the request you sent and the error you got."),
}


class Limited(Exception):
    """Raised when a request must be refused; carries the HTTP status and a friendly message."""

    def __init__(self, status: int, message: str, retry_after: int):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


def _now() -> float:
    return time.time()


def _until_text(ts: float) -> str:
    ist = datetime.fromtimestamp(ts, timezone(timedelta(hours=5, minutes=30)))
    mins = max(1, round((ts - _now()) / 60))
    return f"in about {mins} min (at {ist:%H:%M} IST)" if mins < 90 else f"at {ist:%H:%M} IST"


def looks_like_gibberish(text: str) -> bool:
    """Cheap, conservative checks: real questions, pasted JSON, curl and logs must pass."""
    t = text.strip()
    chars = [c for c in t if not c.isspace()]
    if not chars:
        return True
    alnum = sum(c.isalnum() for c in chars)
    if len(chars) >= 6 and alnum / len(chars) < 0.25:                 # "!!!???###@@@"
        return True
    if re.search(r"([A-Za-z])\1{14,}", t):                           # "aaaaaaaaaaaaaaa"
        return True
    words = [w.lower() for w in re.findall(r"[A-Za-z]{2,}", t)]
    if len(words) >= 8 and len(set(words)) <= 2:                     # "spam spam spam ..."
        return True
    return False


class Guard:
    def __init__(self):
        self.minute: dict[str, deque] = defaultdict(deque)
        self.hour: dict[str, deque] = defaultdict(deque)
        self.strikes: dict[str, deque] = defaultdict(deque)     # subject -> (ts, reason)
        self.blocks: dict[str, dict] = {}                        # subject -> block info
        self.recent: dict[str, deque] = defaultdict(deque)      # subject -> (ts, message hash)
        self.active: dict[str, int] = defaultdict(int)
        self.active_total = 0
        self.events: deque = deque(maxlen=300)                   # recent strikes/blocks for admins

    # ---------------------------------------------------------------- identity
    @staticmethod
    def subjects(ip: str, cid: str | None, user: str | None = None) -> list[str]:
        subs = [f"user:{user.lower()}"] if user else []
        if cid and CID_RE.match(cid):
            subs.append(f"cid:{cid}")
        if ip:
            subs.append(f"ip:{ip}")
        return subs or ["ip:unknown"]

    # ---------------------------------------------------------------- blocks
    def check_blocked(self, subjects: list[str]) -> None:
        now = _now()
        for s in subjects:
            b = self.blocks.get(s)
            if b and b["until"] <= now:
                del self.blocks[s]
            elif b and (s.startswith(("cid:", "user:")) or config.BLOCK_BY_IP):
                raise Limited(429, "Access to the assistant is paused because of repeated unrelated or "
                                   f"spam messages. You can try again {_until_text(b['until'])}. "
                                   f"If this is a mistake, contact {config.SUPPORT_CONTACT}.",
                              int(b["until"] - now))

    def block(self, subjects: list[str], reason: str, by: str = "auto") -> float:
        until = _now() + config.BLOCK_HOURS * 3600
        for s in subjects:
            if s.startswith("ip:") and not config.BLOCK_BY_IP:
                continue
            self.blocks[s] = {"subject": s, "since": _now(), "until": until, "reason": reason, "by": by}
        self._event("blocked", subjects, reason)
        return until

    def unblock(self, subject: str) -> bool:
        found = self.blocks.pop(subject, None) is not None
        for key in [k for k in self.strikes if k == subject]:
            self.strikes[key].clear()
        if found:
            self._event("unblocked", [subject], "by admin")
        return found

    def list_blocks(self) -> list[dict]:
        now = _now()
        for s in [s for s, b in self.blocks.items() if b["until"] <= now]:
            del self.blocks[s]
        return sorted(self.blocks.values(), key=lambda b: b["until"], reverse=True)

    # ---------------------------------------------------------------- rate limits
    def check_rate(self, key: str, per_minute: int, per_hour: int) -> None:
        now = _now()
        m, h = self.minute[key], self.hour[key]
        while m and now - m[0] > 60:
            m.popleft()
        while h and now - h[0] > 3600:
            h.popleft()
        if len(m) >= per_minute:
            raise Limited(429, "You're sending messages too quickly. Please wait a minute and try again.",
                          int(60 - (now - m[0])) + 1)
        if len(h) >= per_hour:
            raise Limited(429, f"You've reached the limit of {per_hour} questions per hour. "
                               f"Please try again {_until_text(h[0] + 3600)}.", int(3600 - (now - h[0])) + 1)
        m.append(now)
        h.append(now)

    # ---------------------------------------------------------------- spam
    def precheck(self, subjects: list[str], text: str) -> str | None:
        """Reason to refuse without calling the AI ("gibberish" / "repeat"), or None."""
        if looks_like_gibberish(text):
            return "gibberish"
        digest = hashlib.sha256(re.sub(r"\s+", " ", text.strip().lower()).encode()).hexdigest()
        now = _now()
        key = subjects[0]
        recent = self.recent[key]
        while recent and now - recent[0][0] > REPEAT_WINDOW:
            recent.popleft()
        seen = sum(1 for _, d in recent if d == digest)
        recent.append((now, digest))
        if len(recent) > 50:
            recent.popleft()
        return "repeat" if seen >= REPEATS_ALLOWED else None

    def strike(self, subjects: list[str], reason: str) -> tuple[int, float | None]:
        """Record a strike; returns (strikes in window for this browser, block-until or None)."""
        now = _now()
        window = config.STRIKE_WINDOW_MINUTES * 60
        counts = {}
        for s in subjects:
            q = self.strikes[s]
            q.append((now, reason))
            while q and now - q[0][0] > window:
                q.popleft()
            counts[s] = len(q)
        self._event("strike", subjects, reason)
        # The first subject is the account, else the browser ID (or the IP for clients without
        # one, e.g. scripts).
        primary = counts[subjects[0]]
        over = primary >= config.STRIKES_TO_BLOCK
        ip_over = any(s.startswith("ip:") and n >= config.STRIKES_TO_BLOCK * IP_STRIKE_FACTOR
                      for s, n in counts.items())
        if over or ip_over:
            return primary, self.block(subjects, f"{config.STRIKES_TO_BLOCK}+ strikes, last: {reason}")
        return primary, None

    def strike_count(self, subjects: list[str]) -> int:
        now = _now()
        q = self.strikes.get(subjects[0], ())
        return sum(1 for t, _ in q if now - t <= config.STRIKE_WINDOW_MINUTES * 60)

    # ---------------------------------------------------------------- load
    def acquire(self, key: str) -> None:
        if self.active[key] >= 1:
            raise Limited(429, "Please wait for the current answer to finish.", 5)
        if self.active_total >= config.MAX_CONCURRENT_ANSWERS:
            raise Limited(503, "The assistant is busy right now. Please try again in a few seconds.", 10)
        self.active[key] += 1
        self.active_total += 1

    def release(self, key: str) -> None:
        if self.active.get(key, 0) > 0:
            self.active[key] -= 1
            self.active_total -= 1
            if not self.active[key]:
                del self.active[key]

    # ---------------------------------------------------------------- admin view
    def _event(self, kind: str, subjects: list[str], reason: str) -> None:
        self.events.appendleft({"ts": datetime.now(timezone.utc).isoformat(), "event": kind,
                                "subjects": subjects, "reason": reason})


guard = Guard()
