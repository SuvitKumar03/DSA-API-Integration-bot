"""Runs jobs one at a time: source jobs (build or refresh a repo / chat doc) and the daily
documentation-gap job per team ("gaps:<bucket>", bot/gaps.py), the weekly insights job
("insights", bot/analytics.py), plus the scheduler.

One job at a time on purpose: each job makes several long AI calls, and running them in
parallel overloads a laptop (and the AI quota). Jobs wait in a queue; the admin page shows
the current job, its progress and the queue.

The scheduler checks every minute (SCHEDULER_ENABLED) for sources whose next run has come
and queues them. On Cloud Run, which can stop idle instances, Cloud Scheduler calls
POST /api/cron/tick instead (see docs/deploy-gcp.md).

Every document change is saved like an admin edit: the old version goes to history, the
version is bumped (all instances reload), and the audit trail records it.
"""

import asyncio
import logging
import time
from datetime import timedelta

from bot import analytics, config, gaps, gchat_docs, github_docs, sources
from bot.github_docs import SourceError
from bot.knowledge_store import current_version, store
from bot.sections import unified_diff

log = logging.getLogger(__name__)

MAX_AUDIT_DIFF_CHARS = 20_000
RETRY_AFTER_ERROR = timedelta(hours=6)

_queue: asyncio.Queue | None = None
_queued: list[dict] = []
_current: dict | None = None
_tasks: list[asyncio.Task] = []
_loop: asyncio.AbstractEventLoop | None = None


def status() -> dict:
    return {"current": dict(_current) if _current else None, "queued": list(_queued)}


def enqueue(source_id: str, mode: str = "auto", by: str = "scheduler") -> bool:
    """Queue a job; False if this source is already queued or running."""
    if _queue is None:
        raise RuntimeError("The job worker isn't running")
    if any(j["source"] == source_id for j in _queued) or (_current and _current["source"] == source_id):
        return False
    job = {"source": source_id, "mode": mode, "by": by, "queued_at": sources.iso(sources.now())}
    _queued.append(job)
    if source_id == "insights":
        analytics.update_state(status="queued", message="Waiting for its turn")
    elif source_id.startswith("gaps:"):
        gaps.update_state(source_id[5:], status="queued", message="Waiting for its turn")
    else:
        sources.update(source_id, lambda s: s["state"].update(status="queued", message="Waiting for its turn"))
    _queue.put_nowait(job)
    return True


def tick() -> list[str]:
    """Queue every source that is due. Returns their ids."""
    queued = [src["id"] for src in sources.due(sources.now()) if enqueue(src["id"], "auto", "scheduler")]
    queued += [f"gaps:{b}" for b in gaps.due(sources.now()) if enqueue(f"gaps:{b}", "auto", "scheduler")]
    if analytics.due(sources.now()) and enqueue("insights", "auto", "scheduler"):
        queued.append("insights")
    if queued:
        log.info("Scheduler queued: %s", queued)
    return queued


def _save(src: dict, text: str, by: str, summary: str) -> str:
    s = store()
    old = s.read(src["doc"]) or ""
    version, saved_as = s.write(src["doc"], text, by=by)
    current_version(force=True)
    diff = unified_diff(old, text, src["doc"])
    record = {"ts": sources.iso(sources.now()), "event": "source_update", "admin": by, "method": "source",
              "file": src["doc"], "source": src["id"], "team": src.get("team"), "note": summary, "version": version,
              "previous_saved_as": saved_as, "chars_before": len(old), "chars_after": len(text),
              "actions": [{"section": "(generated)", "action": "created" if not old else "updated"}],
              "diff": diff[:MAX_AUDIT_DIFF_CHARS], "diff_truncated": len(diff) > MAX_AUDIT_DIFF_CHARS}
    try:
        s.append_audit(record)
        s.set_last_modified(src["doc"], {k: record[k] for k in ("ts", "event", "admin", "method", "note",
                                                                 "version", "actions", "diff")})
    except Exception:
        log.exception("Could not write audit record: %s", {k: v for k, v in record.items() if k != "diff"})
    return version


async def _run_gaps(job: dict) -> None:
    global _current
    bucket = job["source"][5:]
    started = time.time()
    _current = {**job, "label": f"Gaps: {gaps.bucket_name(bucket)}", "started_at": sources.iso(sources.now()),
                "progress": "Starting"}

    def progress(msg: str) -> None:
        _current["progress"] = msg
        log.info("[gaps:%s] %s", bucket, msg)

    gaps.update_state(bucket, status="running", message="Running")
    try:
        outcome, message = await gaps.run_bucket(bucket, job["by"], progress)
    except Exception as e:
        log.exception("Gap job for %s failed", bucket)
        outcome, message = "error", f"Unexpected error: {type(e).__name__} (details in the server log)"
    t = sources.now()
    st = gaps.state().get(bucket, {})
    runs = ([{"ts": sources.iso(t), "by": job["by"], "result": outcome, "message": message,
              "seconds": round(time.time() - started)}] + st.get("runs", []))[:10]
    gaps.update_state(bucket, status=outcome, message=message, last_run=sources.iso(t), runs=runs,
                      next_run=sources.iso(sources.next_run(st.get("schedule", gaps.DEFAULT_SCHEDULE), t, t)))
    log.info("Gap job %s %s: %s", bucket, outcome, message)


async def _run_insights(job: dict) -> None:
    global _current
    started = time.time()
    _current = {**job, "label": "Weekly insights", "started_at": sources.iso(sources.now()), "progress": "Starting"}

    def progress(msg: str) -> None:
        _current["progress"] = msg
        log.info("[insights] %s", msg)

    analytics.update_state(status="running", message="Running")
    try:
        outcome, message = await analytics.run(job["by"], progress)
    except Exception as e:
        log.exception("Insights job failed")
        outcome, message = "error", f"Unexpected error: {type(e).__name__} (details in the server log)"
    t = sources.now()
    st = analytics.state()
    analytics.update_state(status=outcome, message=message, last_run=sources.iso(t),
                           next_run=sources.iso(sources.next_run(st["schedule"], t, t)))
    log.info("Insights job %s: %s (%ds)", outcome, message, time.time() - started)


async def _run(job: dict) -> None:
    global _current
    if job["source"] == "insights":
        await _run_insights(job)
        return
    if job["source"].startswith("gaps:"):
        await _run_gaps(job)
        return
    src = sources.get(job["source"])
    if not src:
        return
    started = time.time()
    _current = {**job, "label": src["label"], "started_at": sources.iso(sources.now()), "progress": "Starting"}

    def progress(msg: str) -> None:
        _current["progress"] = msg
        log.info("[%s] %s", src["id"], msg)

    sources.update(src["id"], lambda s: s["state"].update(status="running", message="Running"))
    state, result = src["state"], None
    current_doc = store().read(src["doc"])
    try:
        if src["kind"] == "github":
            g = src["github"]
            if job["mode"] == "full" or not current_doc or not state.get("last_sha"):
                result = await github_docs.build(g["owner"], g["repo"], g.get("branch"), progress)
            else:
                result = await github_docs.update(g["owner"], g["repo"], g["branch"], current_doc,
                                                  state["last_sha"], progress)
        else:
            space = src["gchat"]["space"]
            full = job["mode"] == "full" or not current_doc
            result = await gchat_docs.run(space, None if full else current_doc,
                                          None if full else state.get("last_message_time"), progress)
        version = _save(src, result["text"], job["by"], result["summary"]) if result.get("text") else None
        outcome, message = ("ok" if version else "no_changes"), result["summary"]
    except SourceError as e:
        outcome, message, version = "error", str(e), None
    except Exception as e:
        log.exception("Job for %s failed", src["id"])
        outcome, message, version = "error", f"Unexpected error: {type(e).__name__} (details in the server log)", None

    t = sources.now()

    def finish(s: dict) -> None:
        st = s["state"]
        st.update(status=outcome, message=message, last_run=sources.iso(t))
        if outcome != "error":
            st["last_ok"] = sources.iso(t)
            st.pop("error_count", None)
            if result and s["kind"] == "github":
                st["last_sha"] = result["head"]["sha"]
                st["last_commit_date"] = result["head"]["date"]
                if result.get("branch"):
                    s["github"]["branch"] = result["branch"]
            if result and s["kind"] == "gchat":
                st["last_message_time"] = result["last_time"]
                if result.get("name"):
                    s["label"] = f"Google Chat: {result['name']}"
        else:
            st["error_count"] = st.get("error_count", 0) + 1
        nxt = sources.next_run(s["schedule"], t, t)
        # A failed scheduled run is retried sooner than the next regular time.
        if outcome == "error" and job["by"] == "scheduler" and s["schedule"]["type"] != "manual":
            nxt = min(filter(None, [nxt, t + RETRY_AFTER_ERROR]))
        st["next_run"] = sources.iso(nxt)
        sources.record_run(s, {"ts": sources.iso(t), "mode": job["mode"], "by": job["by"], "result": outcome,
                               "message": message, "seconds": round(time.time() - started), "version": version})

    sources.update(src["id"], finish)
    log.info("Job %s %s: %s (%ds)", src["id"], outcome, message, time.time() - started)


async def _worker() -> None:
    global _current
    while True:
        job = await _queue.get()
        if job in _queued:
            _queued.remove(job)
        try:
            await _run(job)
        finally:
            _current = None
            _queue.task_done()


async def _scheduler() -> None:
    while True:
        try:
            tick()
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(60)


def start() -> None:
    """Start the worker (and the scheduler, if enabled). Called once at app startup."""
    global _queue, _loop
    loop = asyncio.get_running_loop()
    if _queue is not None and _loop is loop:
        return
    # First start, or the app was started again on a new event loop (tests): fresh queue and worker.
    _queue, _loop = asyncio.Queue(), loop
    _queued.clear()
    _tasks.clear()
    # Jobs that were running or queued when the server stopped are queued again.
    for src in sources.all_sources():
        if src["state"].get("status") in ("queued", "running"):
            sources.update(src["id"], lambda s: s["state"].update(status="interrupted",
                                                                   message="Server restarted; queued again"))
            enqueue(src["id"], "auto", "restart")
    if analytics.state().get("status") in ("queued", "running"):
        enqueue("insights", "auto", "restart")
    for bucket, st in gaps.state().items():
        if st.get("status") in ("queued", "running"):
            enqueue(f"gaps:{bucket}", "auto", "restart")
    _tasks.append(asyncio.create_task(_worker()))
    if config.SCHEDULER_ENABLED:
        _tasks.append(asyncio.create_task(_scheduler()))
        log.info("Scheduler on (%s)", config.SCHEDULE_TIMEZONE)


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    _tasks.clear()
