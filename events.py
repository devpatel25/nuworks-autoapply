"""Live agent trace for the Claude "pipeline-live" pane.

instrument() wraps the agent entry points the pipeline calls (scraper, classifier,
keywords, tailor, Big Interview, cover letter, apply, Telegram, ...) so every call
writes start / end / error / msg lines to logs/events/<run>.jsonl. The pipeline
flow itself is untouched; emitting never raises into it.
"""
import functools
import inspect
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

EVENTS_DIR = Path(__file__).resolve().parent / "logs" / "events"
RUN = datetime.now().strftime("%Y%m%d-%H%M%S")
_lock = threading.Lock()
_local = threading.local()   # .job (last job this worker thread touched), .depth
_titles: dict = {}           # job_id -> title, so calls that only get an id still label

# (module attribute, agent, kind). kind: call = agent does work and returns to the
# pipeline; send = one-way message to Telegram; wait = blocked on a human tap.
TARGETS = [
    ("pipeline", "scrape", "scraper", "call"),
    ("apply_type", "classify_jobs", "classifier", "call"),
    ("ra", "audit_bases_cached", "ats_audit", "call"),
    ("ra", "extract_keywords", "keywords", "call"),
    ("tailor", "tailor_with_claude", "tailor", "call"),
    ("tailor", "retailor", "tailor", "call"),
    ("ra", "verify_tailored_pdf", "verifier", "call"),
    ("pipeline", "_bi", "bi_scan", "call"),
    ("cover_letter", "generate_with_claude", "cover_letter", "call"),
    ("auto_apply", "apply_to_job", "apply", "call"),
    ("external_autofill", "process_job", "external", "call"),
    ("supervisor", "run_supervisor", "supervisor", "call"),
    ("tg", "send_message", "telegram", "send"),
    ("tg", "send_job_for_approval", "telegram", "send"),
    ("tg", "send_low_match_card", "telegram", "send"),
    ("tg", "send_resume_for_approval", "telegram", "send"),
    ("tg", "send_score_card", "telegram", "send"),
    ("tg", "send_cover_letter_for_approval", "telegram", "send"),
    ("tg", "send_duo_decision", "telegram", "send"),
    ("tg", "wait_for_decision", "telegram", "wait"),
    ("tg", "wait_for_duo_decision", "telegram", "wait"),
]


def emit(agent: str, ev: str, text: str = "", job: str = "", **extra):
    try:
        job = job or getattr(_local, "job", "") or ""
        rec = {"t": round(time.time(), 3), "run": RUN, "agent": agent, "ev": ev,
               "job": job, "title": _titles.get(job, ""), "text": str(text)[:200], **extra}
        line = json.dumps(rec, default=str) + "\n"
        with _lock:
            EVENTS_DIR.mkdir(parents=True, exist_ok=True)
            with open(EVENTS_DIR / f"{RUN}.jsonl", "a") as f:
                f.write(line)
    except Exception:
        pass  # ponytail: the trace is best effort, never break a run for it


def _job_of(args, kwargs) -> str:
    for a in list(args) + list(kwargs.values()):
        if isinstance(a, dict) and a.get("job_id"):
            jid = str(a["job_id"])
            if a.get("title"):
                _titles[jid] = str(a["title"])[:60]
            return jid
    jid = kwargs.get("job_id") or (args[0] if args and isinstance(args[0], str)
                                   and args[0] in _titles else "")
    return str(jid or "")


def _summary(fn_name, args, kwargs) -> str:
    if fn_name == "_bi" and args:
        return f"{args[0]}"
    if fn_name == "send_message" and args:
        return str(args[0])[:80]
    if fn_name == "classify_jobs" and args:
        return f"{len(args[0])} jobs"
    return fn_name


def _outcome(r) -> str:
    if isinstance(r, (str, bool, int)) or r is None:
        return str(r)
    if isinstance(r, dict):
        for k in ("status", "result", "ok", "state"):
            if k in r:
                return f"{k}={r[k]}"
        return f"{len(r)} keys"
    if isinstance(r, list):
        return f"{len(r)} items"
    return type(r).__name__


def _wrap(fn, agent, kind):
    name = fn.__name__

    def before(args, kwargs):
        depth = getattr(_local, "depth", 0)
        _local.depth = depth + 1
        if depth:
            return None  # nested inside another traced call: the outer one speaks
        jid = _job_of(args, kwargs)
        if jid:
            _local.job = jid
        sid = uuid.uuid4().hex[:6]
        emit(agent, "msg" if kind == "send" else "start", _summary(name, args, kwargs),
             job=jid, span=sid, kind=kind, fn=name)
        return sid, time.time(), jid

    def after(tok, r=None, err=None):
        _local.depth -= 1
        if tok is None or kind == "send" and err is None:
            return
        sid, t0, jid = tok
        if err is not None:
            emit(agent, "error", f"{type(err).__name__}: {err}", job=jid, span=sid,
                 kind=kind, fn=name, dur=round(time.time() - t0, 1))
        else:
            emit(agent, "end", _outcome(r), job=jid, span=sid, kind=kind, fn=name,
                 dur=round(time.time() - t0, 1))

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def aw(*args, **kwargs):
            tok = before(args, kwargs)
            try:
                r = await fn(*args, **kwargs)
            except BaseException as e:
                after(tok, err=e)
                raise
            after(tok, r)
            return r
        return aw

    @functools.wraps(fn)
    def w(*args, **kwargs):
        tok = before(args, kwargs)
        try:
            r = fn(*args, **kwargs)
        except BaseException as e:
            after(tok, err=e)
            raise
        after(tok, r)
        return r
    return w


def instrument(pipeline_module):
    """Patch TARGETS in place (module attributes are looked up at call time, so the
    pipeline's own calls go through the wrappers). Idempotent."""
    if getattr(pipeline_module, "_events_on", False):
        return
    pipeline_module._events_on = True
    import supervisor  # main() imports it lazily; patch the same module object
    mods = {"pipeline": pipeline_module, "supervisor": supervisor}
    for mod_name, attr, agent, kind in TARGETS:
        mod = mods.get(mod_name) or getattr(pipeline_module, mod_name, None)
        fn = getattr(mod, attr, None) if mod else None
        if fn is None or getattr(fn, "_traced", False):
            continue
        wrapped = _wrap(fn, agent, kind)
        wrapped._traced = True
        setattr(mod, attr, wrapped)
    if not getattr(pipeline_module.log, "_traced", False):
        _log = pipeline_module.log

        def log(msg):
            _log(msg)
            if not getattr(_local, "depth", 0):
                emit("pipeline", "log", msg)
        log._traced = True
        pipeline_module.log = log
    emit("pipeline", "start", f"run {RUN}")
    import atexit
    atexit.register(lambda: emit("pipeline", "end", "run finished"))


def _demo():
    """`python3 events.py --demo`: a fake ~40s run (no portal, no Telegram, no LLM)
    so the pane can be seen working without applying anywhere."""
    import random
    jobs = [{"job_id": f"d{i}", "title": t} for i, t in
            enumerate(["ML Engineer Co-op", "Data Analytics Co-op", "SWE Co-op"])]
    sleep = lambda a, b: time.sleep(random.uniform(a, b))

    def stage(agent, jid, fn, out, a=1, b=3, kind="call"):
        _local.job = jid
        emit(agent, "start", fn, job=jid, span=(sid := uuid.uuid4().hex[:6]), kind=kind, fn=fn)
        t0 = time.time()
        sleep(a, b)
        emit(agent, "end", out, job=jid, span=sid, kind=kind, fn=fn, dur=round(time.time() - t0, 1))

    def card(jid, fn, decision, a=1, b=4):
        emit("telegram", "msg", fn, job=jid, kind="send", fn=fn)
        stage("telegram", jid, "wait_for_decision", decision, a, b, "wait")

    def worker(j):
        # each demo job takes a different path so every branch lights up
        jid, k = j["job_id"], int(j["job_id"][1:])
        _titles[jid] = j["title"]
        card(jid, "send_job_for_approval", "approve", 2, 6)
        stage("keywords", jid, "extract_keywords", "12 keys")
        if k == 1:
            card(jid, "send_low_match_card", "lowok")
        stage("tailor", jid, "tailor_with_claude", "ok=True", 4, 8)
        stage("verifier", jid, "verify_tailored_pdf", "ok=True", 0.5, 1)
        if k == 0:
            card(jid, "send_resume_for_approval", "scan")
            for cmd in ("auth", "start", "status", "feedback"):
                stage("bi_scan", jid, cmd, "ok=True", 0.5, 1.5)
            card(jid, "send_score_card", "retailor")
            stage("tailor", jid, "retailor", "ok=True", 3, 5)
            stage("verifier", jid, "verify_tailored_pdf", "ok=True", 0.5, 1)
        card(jid, "send_resume_for_approval", "approve")
        if k != 2:
            stage("cover_letter", jid, "generate_with_claude", "ok=True", 2, 4)
            card(jid, "send_cover_letter_for_approval", "approve")
        with _lock_apply:
            stage("apply", jid, "apply_to_job", "status=applied", 3, 5)
            if k == 2:
                stage("external", jid, "process_job", "None", 2, 3)

    _lock_apply = threading.Lock()
    emit("pipeline", "start", f"run {RUN} (demo)")
    stage("scraper", "", "scrape", "None", 3, 4)
    stage("classifier", "", "classify_jobs", "40 items", 1, 2)
    stage("ats_audit", "", "audit_bases_cached", "2 keys", 1, 1.5)
    ts = [threading.Thread(target=worker, args=(j,)) for j in jobs]
    for t in ts:
        t.start()
        sleep(0.5, 1.5)
    for t in ts:
        t.join()
    stage("supervisor", "", "run_supervisor", "ok=True", 1, 2)
    emit("pipeline", "end", "run finished")
    print(f"demo trace: {EVENTS_DIR / (RUN + '.jsonl')}")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        _demo()
