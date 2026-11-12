"""A small local model that answers common partner questions without an AI call.

Most partner questions are the same few dozen doubts asked in different words. The curated
FAQ (knowledge/04-faq.md) already has a reviewed answer for each one, so when the model is sure a
first question means one of those FAQ entries, the bot returns that entry's answer directly.
Anything it isn't sure about goes to the AI as before.

The model is a text classifier: one class per FAQ entry, plus OTHER ("the FAQ doesn't answer
this"). Runs in plain Python in about a millisecond; only training needs scikit-learn.
    features()   word, word-pair and character 3-5-gram features (character grams catch typos,
                 Hinglish spellings and pasted error text).
    vectorize()  TF-IDF weights, L2-normalised. Training and this module use the same function,
                 so the server computes exactly what the model was trained on.
    predict()    multinomial logistic regression (weights from scripts/train_local_model.py)
                 → probability per class. The bot answers only when the top class is an FAQ
                 entry with probability ≥ `threshold`, learned by cross-validation for a target
                 precision and saved with the weights in local_model/model.json.

Training data comes from the AI once ("teacher"): paraphrases of each FAQ question, near-miss
questions the FAQ doesn't answer (OTHER), real asked questions labelled by the AI, and questions
users rejected with a 👎 here. Safety: the answer text is read from the current FAQ file on every
use, so an admin's edit shows up at once, and an entry whose question was changed or removed is
never answered from an old model. Answers pass the partner filters (sanitize_doc + redact).
"""

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter

from bot import config
from bot.blobs import data, read_json, write_json
from bot.guardrails import redact, sanitize_doc
from bot.knowledge_store import current_version, store
from bot.retrieval import STOPWORDS

log = logging.getLogger("bot.local_model")

FAQ_FILE = "04-faq.md"
MODEL_PATH = "local_model/model.json"
REJECTED_PATH = "local_model/rejected.json"
OTHER = "OTHER"
RELOAD_SECONDS = 60
GROUP_WEIGHT = {"w": 1.0, "b": 1.0, "c": 0.4}      # words, word pairs, character grams
EXTRA_STOPWORDS = set("kaise karu kare karna karte hota hoga raha rahe ho bhi ye yeh wo aur please pls hi hello".split())
FOOTER = ("\n\n_Quick answer from the FAQ. If this isn't what you meant, reply with more detail "
          "and I'll look deeper._")

ENTRY_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")


# ---------------------------------------------------------------- FAQ entries

def normalise(text: str) -> str:
    return re.sub(r"[\s?!.]+$", "", re.sub(r"\s+", " ", text.strip().lower()))


def entry_id(question: str) -> str:
    return hashlib.sha256(normalise(question).encode()).hexdigest()[:12]


def faq_entries(text: str) -> dict[str, dict]:
    """`**Question**` line followed by the answer lines, grouped under `## Section` headings."""
    entries, section, current = {}, "", None
    for line in text.splitlines():
        if line.startswith("## "):
            section, current = line[3:].strip(), None
            continue
        m = ENTRY_RE.match(line.strip())
        if m:
            current = {"question": m.group(1).strip(), "answer": [], "section": section}
            entries[entry_id(current["question"])] = current
        elif current is not None and (line.strip() or current["answer"]):
            current["answer"].append(line)
    for e in entries.values():
        e["answer"] = "\n".join(e["answer"]).strip()
    return {k: e for k, e in entries.items() if e["answer"]}


# ---------------------------------------------------------------- features

def _words(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.findall(r"[a-z0-9]+", text.lower())


def features(text: str) -> Counter:
    words = [w for w in _words(text) if w not in STOPWORDS and w not in EXTRA_STOPWORDS]
    f = Counter(f"w:{w}" for w in words)
    f.update(f"b:{a}_{b}" for a, b in zip(words, words[1:]))
    for w in words:
        s = f" {w} "
        for n in (3, 4, 5):
            f.update(f"c:{s[i:i + n]}" for i in range(len(s) - n + 1))
    return f


def idf_table(texts: list[str]) -> dict[str, float]:
    df = Counter(k for t in texts for k in features(t))
    n = len(texts)
    return {k: math.log((1 + n) / (1 + d)) + 1 for k, d in df.items()}


def vectorize(text: str, idf: dict[str, float]) -> dict[str, float]:
    v = {k: (1 + math.log(c)) * idf[k] * GROUP_WEIGHT[k[0]] for k, c in features(text).items() if k in idf}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {k: x / norm for k, x in v.items()}


def predict(model: dict, text: str) -> tuple[str, float]:
    """(class, probability) of the most likely class."""
    logits = list(model["intercept"])
    weights = model["weights"]
    for k, x in vectorize(text, model["idf"]).items():
        for ci, w in weights.get(k, ()):
            logits[ci] += x * w
    top = max(logits)
    exps = [math.exp(z - top) for z in logits]
    best = max(range(len(exps)), key=exps.__getitem__)
    return model["classes"][best], exps[best] / sum(exps)


# ---------------------------------------------------------------- runtime

_state = {"checked": 0.0, "raw": None, "model": None, "kv": None, "entries": {}, "rejected": set()}
_stats = {"answered": 0, "passed": 0}


def _refresh() -> None:
    now = time.monotonic()
    if now - _state["checked"] < RELOAD_SECONDS and _state["raw"] is not None:
        return
    _state["checked"] = now
    raw = data().read(MODEL_PATH) or ""
    if raw != _state["raw"]:
        _state["raw"] = raw
        _state["model"] = json.loads(raw) if raw else None
        if raw:
            log.info("Local model loaded: %d classes, threshold %.2f", len(_state["model"]["classes"]),
                     _state["model"]["threshold"])
    _state["rejected"] = {r["q"] for r in read_json(REJECTED_PATH, []) or []}
    kv = current_version()
    if kv != _state["kv"]:
        _state["kv"] = kv
        _state["entries"] = faq_entries(store().read(FAQ_FILE) or "")


def answer(question: str, audience: str) -> dict | None:
    """The FAQ answer for this question when the model is confident, else None."""
    if not config.LOCAL_MODEL_ENABLED or audience not in config.LOCAL_MODEL_AUDIENCES:
        return None
    try:
        _refresh()
        model = _state["model"]
        if not model or normalise(question) in _state["rejected"]:
            return None
        label, p = predict(model, question)
        entry = _state["entries"].get(label) if label != OTHER and p >= model["threshold"] else None
        if not entry:
            _stats["passed"] += 1
            return None
        text = sanitize_doc(entry["answer"])
        if audience == "partner":
            text, _ = redact(text)
        _stats["answered"] += 1
        return {"answer": text + FOOTER, "entry": label, "question": entry["question"], "score": round(p, 3)}
    except Exception:  # the model is an optimisation: never break chat because of it
        log.exception("Local model failed; using the AI")
        return None


def served(message_id: str, question: str, entry: str) -> None:
    write_json(f"local_model/served/{message_id}.json", {"q": normalise(question), "entry": entry, "ts": time.time()})


def reject(message_id: str) -> bool:
    """A 👎 on a local answer: never answer that question locally again; the next training run
    learns it as OTHER."""
    hit = read_json(f"local_model/served/{message_id}.json") if re.fullmatch(r"[0-9a-f]{32}", message_id or "") else None
    if not hit:
        return False
    rejected = [r for r in (read_json(REJECTED_PATH, []) or []) if r["q"] != hit["q"]] + [hit]
    write_json(REJECTED_PATH, rejected)
    _state["rejected"].add(hit["q"])
    return True


def stats() -> dict:
    model = _state["model"] or {}
    if not model:
        raw = data().read(MODEL_PATH)
        model = json.loads(raw) if raw else {}
    return {**_stats, "enabled": config.LOCAL_MODEL_ENABLED, "trained": model.get("trained_at"),
            "threshold": model.get("threshold"), "metrics": model.get("metrics")}
