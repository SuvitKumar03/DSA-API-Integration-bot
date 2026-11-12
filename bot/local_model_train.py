"""Keeps the local FAQ model (bot/local_model.py) trained, with no redeploy.

A scheduled job in the normal job queue (bot/jobs.py, "local_model"), default every day at
02:00, plus 30 minutes after the FAQ's questions change. Each run:
 1. Collects new real questions: partners' first questions from the interaction records
    (bot/analytics.py also hands them over just before its weekly clean-up), the chat log and
    the Google Chat extraction, into a lasting pool (local_model/real_questions.json; question
    text only, secrets scrubbed, no user).
 2. Skips the rest when nothing changed since the last training (same FAQ questions, no new
    labelled questions, no new 👎): no AI call, no CPU.
 3. Teacher data from the AI, only for what is new: paraphrases + near misses per new FAQ entry
    (local_model/teacher/), labels for new real questions (local_model/real_labels.json). When
    the FAQ questions change, questions labelled "not in FAQ" are labelled again.
 4. Trains TF-IDF + logistic regression; 5-fold cross-validation picks the regularisation and
    the probability threshold for LOCAL_MODEL_TARGET_PRECISION.
 5. Quality gate: the new model replaces the old one only if cross-validation meets the target
    precision (overall and on real questions). Otherwise the old model stays.
 6. Saves local_model/model.json; every running server loads it within a minute
    (bot/local_model.py), then emails super admins a short summary.

CPU-heavy steps run in a worker thread so chat isn't blocked. scripts/train_local_model.py runs
the same pipeline by hand.
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from bot import config, sources
from bot.blobs import read_json, write_json
from bot.engine import complete
from bot.guardrails import scrub_secrets
from bot.knowledge_store import current_version, store
from bot.local_model import (FAQ_FILE, MODEL_PATH, OTHER, REJECTED_PATH, faq_entries, idf_table, normalise, predict,
                             vectorize)
from bot.mailer import MailError, send

log = logging.getLogger(__name__)

STATE = "local_model/state.json"
POOL = "local_model/real_questions.json"
LABELS = "local_model/real_labels.json"
DEFAULT_SCHEDULE = {"type": "weekly", "days": [0, 1, 2, 3, 4, 5, 6], "time": config.LOCAL_MODEL_RETRAIN_TIME}
FAQ_SETTLE_MINUTES = 30       # wait this long after the FAQ questions change (admins edit in bursts)
POOL_MAX = 5000
NEAR_DUPLICATE = 0.8          # a near miss this similar to an FAQ question is really that question
C_VALUES = (10.0, 30.0)       # regularisation strengths tried (higher = fits the examples harder)
MIN_WEIGHT = 0.02             # smaller weights are dropped from the saved model
TEACHER_BATCH = 6
LABEL_BATCH = 25
_lock = threading.RLock()

TEACHER_SYSTEM = "You create training data for a question-matching model. Reply with JSON only, no prose, no code fences."
TEACHER_PROMPT = """These are all the entries of a partner API FAQ (id: question):
{all_questions}

For each entry below, write:
- "paraphrases": 12 different ways a partner's developer might ask a question whose right answer is exactly this entry's answer. Mix: short keyword style, full sentences, informal with typos, Hinglish in Roman script, pasted error message style, different word order. Use only names that appear in the entry.
- "near_misses": 6 questions on the same topic that this answer does NOT answer and that none of the FAQ entries above answers either (so a bot must look deeper).

Entries:
{entries}

Reply as {{"<id>": {{"paraphrases": [...], "near_misses": [...]}}, ...}} with every id above."""
LABEL_PROMPT = """These are all the entries of a partner API FAQ (id: question, then answer):
{faq}

For each numbered question below, give the id of the FAQ entry whose answer fully and correctly answers it, or "NONE" if no entry does (partly related is NONE).

Questions:
{questions}

Reply as {{"1": "<id or NONE>", ...}} for every number."""


# ---------------------------------------------------------------- schedule state

def state() -> dict:
    with _lock:
        st = read_json(STATE)
        if st is None:
            st = {"schedule": dict(DEFAULT_SCHEDULE), "status": "new",
                  "next_run": sources.iso(sources.next_run(DEFAULT_SCHEDULE, sources.now()))}
            write_json(STATE, st)
        return st


def update_state(**changes) -> dict:
    with _lock:
        st = state()
        st.update(changes)
        write_json(STATE, st)
        return st


_faq_cache = {"version": None, "hash": None}


def faq_hash() -> str:
    """Changes when FAQ questions are added, removed or reworded (answer edits don't need training)."""
    v = current_version()
    if v != _faq_cache["version"]:
        ids = sorted(faq_entries(store().read(FAQ_FILE) or ""))
        _faq_cache.update(version=v, hash=hashlib.sha256(" ".join(ids).encode()).hexdigest()[:12])
    return _faq_cache["hash"]


def due(at: datetime) -> bool:
    if not (config.LOCAL_MODEL_ENABLED and config.LOCAL_MODEL_RETRAIN):
        return False
    st = state()
    if st.get("status") in ("queued", "running"):
        return False
    if st.get("next_run") and sources.parse_iso(st["next_run"]) <= at:
        return True
    # The FAQ questions changed: retrain once they have been stable for a while.
    h = faq_hash()
    if h in (st.get("trained_on", {}).get("faq"), st.get("faq_tried")):
        return False
    if st.get("faq_seen") != h:
        update_state(faq_seen=h, faq_seen_at=sources.iso(at))
        return False
    return (at - sources.parse_iso(st["faq_seen_at"])).total_seconds() >= FAQ_SETTLE_MINUTES * 60


# ---------------------------------------------------------------- real questions

def collect(events: list[dict]) -> int:
    """Add partners' first questions from interaction records to the lasting pool. Returns how
    many were new. Called before bot/analytics.py deletes the records, and by every run."""
    with _lock:
        pool = read_json(POOL, {}) or {}
        added = 0
        for e in events:
            q = (e.get("question") or "").strip()
            if (e.get("kind") != "chat" or e.get("audience") != "partner" or e.get("turn") != 1 or e.get("off_topic")
                    or not q or len(q) > 1500):
                continue
            n = normalise(q)
            if n not in pool:
                pool[n] = {"q": scrub_secrets(q), "ts": e.get("ts", "")}
                added += 1
        if added:
            if len(pool) > POOL_MAX:
                pool = dict(sorted(pool.items(), key=lambda kv: kv[1]["ts"])[-POOL_MAX:])
            write_json(POOL, pool)
        return added


def real_questions() -> list[str]:
    qs = [v["q"] for v in (read_json(POOL, {}) or {}).values()]
    log_file = config.LOG_DIR / "chats.jsonl"
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (rec.get("turns") == 1 and rec.get("question") and rec.get("role", "partner") == "partner"
                    and not rec.get("spam")):
                qs.append(rec["question"])
    for f in (Path(config.ROOT_DIR) / "chat_export/output").glob("*/extracted/all_entries.json"):
        qs += [e["question"] for e in json.loads(f.read_text(encoding="utf-8")) if e.get("question")]
    seen, out = set(), []
    for q in qs:
        n = normalise(q)
        if n and n not in seen and len(q) <= 1500:
            seen.add(n)
            out.append(scrub_secrets(q))
    return out


# ---------------------------------------------------------------- teacher (AI)

def _json(text: str | None) -> dict:
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except ValueError:
        return {}


async def teacher_data(entries: dict[str, dict], progress) -> tuple[dict[str, dict], int]:
    """Paraphrases and near misses per FAQ entry; AI calls only for entries without them."""
    out = {eid: t for eid in entries if (t := read_json(f"local_model/teacher/{eid}.json"))}
    missing = [eid for eid in entries if eid not in out]
    all_q = "\n".join(f"{eid}: {e['question']}" for eid, e in entries.items())
    for i in range(0, len(missing), TEACHER_BATCH):
        ids = missing[i:i + TEACHER_BATCH]
        progress(f"Practice questions for new FAQ entries {i + 1}-{i + len(ids)} of {len(missing)}")
        block = "\n\n".join(f"id {eid}\nQuestion: {entries[eid]['question']}\nAnswer: {entries[eid]['answer']}" for eid in ids)
        reply = _json(await complete(TEACHER_SYSTEM, TEACHER_PROMPT.format(all_questions=all_q, entries=block),
                                     max_tokens=8000, timeout=config.DOC_AI_TIMEOUT_SECONDS))
        for eid in ids:
            item = reply.get(eid) or {}
            if item.get("paraphrases"):
                item = {"question": entries[eid]["question"], "paraphrases": [str(p) for p in item["paraphrases"]],
                        "near_misses": [str(p) for p in item.get("near_misses", [])]}
                write_json(f"local_model/teacher/{eid}.json", item)
                out[eid] = item
    return out, len(missing) - sum(1 for eid in missing if eid in out)


async def real_labels(entries: dict[str, dict], use_teacher: bool, progress) -> dict[str, dict]:
    """Which FAQ entry answers each real question (or OTHER). New questions are labelled; when the
    FAQ questions changed, OTHER and removed-entry labels are labelled again."""
    saved = read_json(LABELS, {}) or {}
    labels, h = saved.get("labels", {}), saved.get("faq")
    if "labels" not in saved and saved:                 # older format: {normalised: {...}}
        labels, h = saved, None
    stale = h != faq_hash()
    todo = [q for q in real_questions()
            if normalise(q) not in labels or (stale and labels[normalise(q)]["label"] not in entries)]
    if todo and use_teacher:
        faq = "\n\n".join(f"{eid}: {e['question']}\n{e['answer']}" for eid, e in entries.items())
        for i in range(0, len(todo), LABEL_BATCH):
            part = todo[i:i + LABEL_BATCH]
            progress(f"Labelling real questions {i + 1}-{i + len(part)} of {len(todo)}")
            reply = _json(await complete(TEACHER_SYSTEM, LABEL_PROMPT.format(
                faq=faq, questions="\n".join(f"{n}. {q}" for n, q in enumerate(part, 1))),
                max_tokens=2000, timeout=config.DOC_AI_TIMEOUT_SECONDS))
            for n, q in enumerate(part, 1):
                label = str(reply.get(str(n), "")).strip()
                if label in entries or label == "NONE":
                    labels[normalise(q)] = {"q": q, "label": label if label in entries else OTHER}
        write_json(LABELS, {"faq": faq_hash(), "labels": labels})
    return labels


# ---------------------------------------------------------------- training (CPU, in a thread)

def _near_duplicates(examples, fixed) -> set[int]:
    """Near misses that are really FAQ questions (or their paraphrases) contradict the FAQ
    labels, so they are dropped."""
    faq_texts = [t for t, _ in fixed] + [t for t, l, _ in examples if l != OTHER]
    idf = idf_table(faq_texts)
    faq_vecs = [vectorize(t, idf) for t in faq_texts]
    out = set()
    for i, (text, _, source) in enumerate(examples):
        if source == "near_miss":
            v = vectorize(text, idf)
            if any(sum(x * f.get(k, 0.0) for k, x in v.items()) >= NEAR_DUPLICATE for f in faq_vecs):
                out.add(i)
    return out


def _fit(texts: list[str], labels: list[str], c: float):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression

    idf = idf_table(texts)
    dv = DictVectorizer()
    x = dv.fit_transform([vectorize(t, idf) for t in texts])
    return idf, dv, LogisticRegression(C=c, max_iter=5000).fit(x, labels)


def _evaluate(examples, fixed, c: float, folds: int = 5, seed: int = 7) -> list[tuple[str, str, str, float]]:
    """Cross-validated (true label, source, predicted label, probability) for every example."""
    order = list(range(len(examples)))
    random.Random(seed).shuffle(order)
    out = []
    for k in range(folds):
        test = order[k::folds]
        skip = set(test)
        train = fixed + [(t, l) for i, (t, l, _) in enumerate(examples) if i not in skip]
        idf, dv, model = _fit([t for t, _ in train], [l for _, l in train], c)
        probs = model.predict_proba(dv.transform([vectorize(examples[i][0], idf) for i in test]))
        for i, p in zip(test, probs):
            j = p.argmax()
            out.append((examples[i][1], examples[i][2], str(model.classes_[j]), float(p[j])))
    return out


def metrics(results, threshold: float) -> dict:
    answered = right = in_faq = 0
    for label, _, pick, p in results:
        in_faq += label != OTHER
        if pick != OTHER and p >= threshold:
            answered += 1
            right += pick == label
    return {"examples": len(results), "in_faq": in_faq, "answered": answered, "right": right,
            "precision": round(right / answered, 4) if answered else 1.0,
            "coverage": round(right / in_faq, 4) if in_faq else 0.0, "wrong_answers": answered - right}


def _tune(results, target: float) -> tuple[float | None, dict]:
    best: tuple[float | None, dict] = (None, metrics(results, 1.01))
    for t in [x / 100 for x in range(30, 100)]:
        m = metrics(results, t)
        if m["precision"] >= target and m["right"] > best[1]["right"]:
            best = (t, m)
    return best


def train(examples, fixed, entries, target: float, progress=lambda msg: None) -> dict:
    """Cross-validate, pick C and the threshold, train on everything. Returns the model (or None
    when no threshold meets the target) and the report."""
    drop = _near_duplicates(examples, fixed)
    examples = [e for i, e in enumerate(examples) if i not in drop]
    best = None
    for c in C_VALUES:
        progress(f"Testing the model (C={c:g})")
        results = _evaluate(examples, fixed, c)
        threshold, overall = _tune(results, target)
        if best is None or overall["right"] > best[2]["right"]:
            best = (c, threshold, overall, results)
    c, threshold, overall, results = best
    report = {"overall": overall, "dropped_near_misses": len(drop),
              "examples": {"faq": len(fixed), "labelled": sum(1 for e in examples if e[1] != OTHER),
                           "other": sum(1 for e in examples if e[1] == OTHER)}}
    for source in ("real", "paraphrase", "near_miss"):
        part = [r for r in results if r[1] == source]
        if part and threshold is not None:
            report[source] = metrics(part, threshold)
    real = [r for r in results if r[1] == "real"]
    report["curve"] = [{"threshold": t, "all": metrics(results, t), "real": metrics(real, t) if real else None}
                       for t in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9)]
    report["wrong"] = [{"true": entries[l]["question"][:80] if l in entries else "OTHER (not in FAQ)",
                        "picked": entries[p]["question"][:80], "p": round(pr, 2)}
                       for l, _, p, pr in results if threshold is not None and p != OTHER and pr >= threshold
                       and p != l and p in entries][:10]
    if threshold is None:
        return {"model": None, "report": report}

    progress("Training the final model")
    texts = [t for t, _ in fixed] + [t for t, _, _ in examples]
    idf, dv, model = _fit(texts, [l for _, l in fixed] + [l for _, l, _ in examples], c)
    classes = [str(x) for x in model.classes_]
    weights = {}
    for name, col in dv.vocabulary_.items():
        ws = [[ci, round(float(model.coef_[ci, col]), 4)] for ci in range(len(classes))
              if abs(model.coef_[ci, col]) >= MIN_WEIGHT]
        if ws:
            weights[name] = ws
    saved = {"trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "kind": "logistic_regression", "C": c, "threshold": threshold, "target_precision": target,
             "faq": faq_hash(), "classes": classes, "intercept": [round(float(b), 4) for b in model.intercept_],
             "idf": {k: round(v, 4) for k, v in idf.items() if k in weights}, "weights": weights,
             "entries": {eid: e["question"] for eid, e in entries.items()}, "metrics": report}
    sk = model.predict(dv.transform([vectorize(t, idf) for t in texts]))
    report["export_check"] = round(sum(predict(saved, t)[0] == s for t, s in zip(texts, sk)) / len(texts), 4)
    return {"model": saved, "report": report}


# ---------------------------------------------------------------- the job

def _summary(report: dict, threshold: float | None) -> str:
    r = report.get("real") or report["overall"]
    which = "real questions" if report.get("real") else "test questions"
    return (f"answers {r['coverage']:.0%} of FAQ-type {which} at {r['precision']:.0%} precision "
            f"({r['wrong_answers']} wrong) in testing; threshold {threshold}")


async def run(by: str, progress=lambda msg: None, use_teacher: bool = True, dry_run: bool = False,
              target: float | None = None, force: bool = False) -> tuple[str, str, dict | None]:
    """One retraining. Returns (outcome, message, report)."""
    from bot import analytics

    target = target or config.LOCAL_MODEL_TARGET_PRECISION
    progress("Collecting new questions")
    added = collect(analytics.events())
    entries = faq_entries(store().read(FAQ_FILE) or "")
    if not entries:
        return "error", f"No FAQ entries found in {FAQ_FILE}", None
    h = faq_hash()
    update_state(faq_tried=h)

    teacher, failed = (await teacher_data(entries, progress)) if use_teacher else (
        {eid: t for eid in entries if (t := read_json(f"local_model/teacher/{eid}.json"))}, 0)
    labels = await real_labels(entries, use_teacher, progress)
    rejected = read_json(REJECTED_PATH, []) or []
    fingerprint = {"faq": h, "teacher": len(teacher), "labels": len(labels), "rejected": len(rejected),
                   "target": target}
    if not force and not dry_run and state().get("trained_on") == fingerprint and read_json(MODEL_PATH):
        return "no_changes", f"Nothing new since the last training ({added} new questions collected, all known)", None

    fixed = [(e["question"], eid) for eid, e in entries.items()]        # always in training
    examples: list[tuple[str, str, str]] = []
    for eid, t in teacher.items():
        if eid in entries:
            examples += [(p, eid, "paraphrase") for p in t["paraphrases"]]
            examples += [(p, OTHER, "near_miss") for p in t["near_misses"]]
    for item in labels.values():
        if item["label"] == OTHER or item["label"] in entries:
            examples.append((item["q"], item["label"], "real"))
    examples += [(r["q"], OTHER, "rejected") for r in rejected]
    if len(examples) < 20:
        return "error", "Too little training data (the AI teacher may have failed; details in the server log)", None

    result = await asyncio.to_thread(train, examples, fixed, entries, target, progress)
    report, model = result["report"], result["model"]
    real = report.get("real")
    if model is None or (real and real["answered"] and real["precision"] < target):
        msg = (f"New model kept back: it did not reach {target:.0%} precision in testing; the current model "
               f"stays in use")
        update_state(trained_on=fingerprint)
        _email("Local model not updated", msg, report)
        return "kept_old", msg, report
    msg = f"New model: {_summary(report, model['threshold'])}"
    if failed:
        msg += f"; {failed} new FAQ entries had no practice questions (retried next run)"
    if dry_run:
        return "ok", msg + " (dry run, not saved)", report
    write_json(MODEL_PATH, model)
    update_state(trained_on=fingerprint, last_model=model["trained_at"])
    _email("Local model retrained", msg + ". Servers use it within a minute.", report)
    return "ok", msg, report


def _email(subject: str, message: str, report: dict) -> None:
    try:
        o = report["overall"]
        send(sorted(config.ADMIN_EMAILS), f"[{config.BOT_NAME}] {subject}",
             f"{message}\n\nTest examples: {o['examples']} ({report['examples']['faq']} FAQ entries). "
             f"Overall: {o['precision']:.1%} precision, {o['coverage']:.1%} coverage.\n\n"
             f"Details: {config.PUBLIC_URL}/admin#insights")
    except MailError as e:
        log.info("Local model email not sent: %s", e)
    except Exception:
        log.exception("Local model email failed")
