#!/usr/bin/env python3
"""
NUworks daily auto-apply orchestrator.

Flow:
  1. Scrape today's jobs              (job_scraper.py -> ../jobs.csv)
  2. Classify apply method            (apply_type.py) -> keep in_portal, not applied
  3. Tailor a resume per job          (tailor.py / ResumeBot2.1)
  4. Send resume to Telegram          (telegram_bot.py) with Approve/Reject
  5. Wait for the user's decision
  6. On Approve -> auto-apply         (auto_apply.py, mode="submit")
  7. Log + notify results

The Telegram approval is the human-in-the-loop gate: NOTHING is submitted to an
employer without an explicit Approve tap.

Usage:
  python3 pipeline.py [--no-scrape] [--limit N] [--dry-apply]
    --no-scrape  : use existing ../jobs.csv instead of re-scraping
    --limit N    : only process the first N eligible jobs
    --dry-apply  : run auto-apply in dry mode (stage, never Submit) even on approval
"""
import os
import sys
import csv
import json
import time
import uuid
import fcntl
import signal
import asyncio
import threading
import subprocess
from pathlib import Path
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import apply_type
import auto_apply
import tailor
import cover_letter
import external_apply
import external_autofill
import claude_runner
import stats
import telegram_bot as tg

HERE = Path(__file__).resolve().parent
JOBS_CSV = HERE / "data" / "jobs.csv"
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Single-instance lock. The LaunchAgent fires at 6 slots/day AND on login; a run
# that is still waiting on a Telegram approval (up to 1h/job) can easily still be
# alive when the next slot fires. Two overlapping runs would re-scrape, re-send
# the same approvals, and (both polling the one bot token) can double-submit or
# steal each other's taps. This exclusive lock makes the 2nd instance exit at once.
LOCK_FILE = LOG_DIR / "pipeline.lock"
_lock_fh = None  # kept at module scope so the fd stays open for the whole run


def acquire_singleton_lock() -> bool:
    """Take an exclusive, non-blocking lock. Returns False if another run holds it.
    The lock is released automatically when this process exits (fd closes)."""
    global _lock_fh
    _lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        _lock_fh.close()
        _lock_fh = None
        return False
    _lock_fh.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
    _lock_fh.flush()
    return True

# How long to wait for each Telegram Approve/Reject tap before skipping the job.
# Bounded so an unattended 10AM run can't hang for hours per job.
APPROVAL_TIMEOUT = 3600  # 1 hour

# ── Time budgets (watchdog) ─────────────────────────────────────────────────
# A hung step must never run for hours again (the 2026-06-05 failure ran ~113
# min). Each phase below gets a generous-but-finite bound; a background watchdog
# aborts the whole run if the deadline is ever passed, leaving today UNMARKED so
# it retries on next login. The deadline is *extended* as we enter each phase, so
# legitimate long waits (e.g. an hour for a Telegram approval) never trip it.
SCRAPE_TIMEOUT  = 900                                                  # hard kill for the scrape subprocess (login + DUO up to 10 min + scrape)
SCRAPE_BUDGET   = SCRAPE_TIMEOUT + 120                                  # watchdog must OUTLAST the subprocess timeout, so the graceful killpg path wins instead of os._exit (which orphans the browser — the 06-10 10:29 trip)
# Tailoring runs up to max_total_attempts() (Sonnet phase + Opus escalation phase),
# each up to DEFAULT_TIMEOUT, with capped linear backoff between regenerations +
# pdflatex time. The budget must cover that real worst case or the watchdog hard-
# kills a healthy-but-slow tailor that's legitimately escalating to Opus (H2).
_TAILOR_ATTEMPTS = tailor.max_total_attempts()
_TAILOR_BACKOFF  = claude_runner.DEFAULT_BACKOFF * sum(min(i, 4) for i in range(1, _TAILOR_ATTEMPTS))
TAILOR_BUDGET   = _TAILOR_ATTEMPTS * claude_runner.DEFAULT_TIMEOUT + _TAILOR_BACKOFF + 300
APPLY_BUDGET    = 300                                                  # one portal apply

# How many resumes to tailor concurrently up front (the slow claude -p calls).
# Mirrors handshake's apply.tailor_parallelism (default 6). Each job tailors in
# its own workdir via an independent claude -p, so the wave is thread-safe.
TAILOR_PARALLELISM = int(os.environ.get("NUWORKS_TAILOR_PARALLELISM", "6"))

_watchdog_deadline = None  # wall-clock time the run must not pass


def _start_watchdog(budget: int):
    """Background thread that hard-aborts the process if the deadline passes
    (something is hung). Aborting WITHOUT marking the day means the run retries
    on next login — which is exactly what we want for a live debug."""
    global _watchdog_deadline
    _watchdog_deadline = time.time() + budget

    def loop():
        while True:
            time.sleep(5)
            if _watchdog_deadline and time.time() > _watchdog_deadline:
                log("⏱️  WATCHDOG: run exceeded its time budget — a step is hung. "
                    "Aborting (today left UNMARKED → retries on next login).")
                try:
                    if tg.configured():
                        tg.send_message(
                            "⏱️ <b>Auto-apply watchdog tripped</b>\n\nA step hung past its "
                            "time budget and the run was aborted. Today was left <b>unmarked</b>, "
                            "so it'll re-run live next time you log in — sit at the Mac and watch "
                            "where it stalls. Check <code>logs/</code> for the latest run log.")
                except Exception:
                    pass
                os._exit(2)

    threading.Thread(target=loop, daemon=True).start()


def bump_watchdog(extra: int):
    """Push the watchdog deadline out by `extra` seconds from now (only ever
    later, never earlier)."""
    global _watchdog_deadline
    if _watchdog_deadline is not None:
        _watchdog_deadline = max(_watchdog_deadline, time.time() + extra)

# Once-per-day guard: lets the LaunchAgent fire on every login (RunAtLoad) so a
# missed 10AM run "catches up" the moment the Mac is next turned on, while only
# actually running once per calendar day.
RUN_STAMP = LOG_DIR / "last_run_date.txt"

# Job IDs to never auto-apply to (poor fit / ineligible), one per line.
SKIP_FILE = HERE / "data" / "skip_jobs.txt"

# Transcript to attach when a job requires one (drop your transcript PDF here).
TRANSCRIPT = HERE / "documents" / "transcript.pdf"


def log(msg: str):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line)
    with open(LOG_DIR / f"run_{datetime.now():%Y%m%d}.log", "a") as f:
        f.write(line + "\n")


def already_ran_today() -> bool:
    return RUN_STAMP.exists() and RUN_STAMP.read_text().strip() == date.today().isoformat()


def mark_ran_today():
    RUN_STAMP.write_text(date.today().isoformat())


# Jobs the user didn't decide on in time (approval timed out). Re-queued on the
# next run so a busy afternoon doesn't lose a posting forever. H5.
PENDING_FILE = HERE / "data" / "pending_retry.json"


def load_pending() -> list[dict]:
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return []


def save_pending(items: list[dict]):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(items, indent=2))


def add_pending(job: dict, failure_class: str = "transient", detail: str = ""):
    """Upsert a job into the retry queue. First time: record it with _attempts=1 and
    _first_queued. Subsequent times: increment _attempts and refresh the failure class,
    detail and timestamp — the supervisor uses _attempts + _last_class to cap doomed
    loops (a job that keeps failing the same structural way is evicted, not retried).
    Backward-compatible: callers that pass no class re-queue as a plain transient."""
    items = load_pending()
    now = datetime.now().isoformat()
    for p in items:
        if p.get("job_id") == job.get("job_id"):
            p["_attempts"] = int(p.get("_attempts", 0) or 0) + 1
            p["_last_class"] = failure_class
            p["_last_detail"] = (detail or "")[:300]
            p["_queued_at"] = now
            save_pending(items)
            return
    # First queue: keep the fields a later run needs to tailor + apply without re-scraping.
    keep = {k: job.get(k) for k in ("job_id", "title", "company", "location",
                                    "salary", "deadline", "description", "apply_urls",
                                    "_apply_type", "_required_docs")}
    keep["_queued_at"] = now
    keep["_first_queued"] = now
    keep["_attempts"] = 1
    keep["_last_class"] = failure_class
    keep["_last_detail"] = (detail or "")[:300]
    items.append(keep)
    save_pending(items)


def clear_pending(job_id: str):
    items = load_pending()
    kept = [p for p in items if p.get("job_id") != job_id]
    if len(kept) != len(items):
        save_pending(kept)


def load_skiplist() -> set:
    if not SKIP_FILE.exists():
        return set()
    out = set()
    for line in SKIP_FILE.read_text().splitlines():
        token = line.split("#", 1)[0].strip()
        if token:
            out.add(token)
    return out


def add_to_skiplist(job_id: str):
    SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SKIP_FILE, "a") as f:
        f.write(f"{job_id}\n")


# job_ids the supervisor has given up auto-retrying (data/wontfix.json, a dict keyed by
# job_id). Filtered out of eligible() + merge_pending() so an evicted job stops looping.
WONTFIX_FILE = HERE / "data" / "wontfix.json"


def load_wontfix() -> set:
    if not WONTFIX_FILE.exists():
        return set()
    try:
        return set(json.loads(WONTFIX_FILE.read_text()).keys())
    except Exception:
        return set()


class DuoNotApproved(Exception):
    """The scraper exited (code 3) because the DUO push wasn't approved in its
    window — distinct from other login failures so main() can ask the user, live,
    whether to resend the push and rerun now or skip for today."""


# How long to wait for the user's Rerun/Skip tap after a DUO timeout before giving
# up and falling back to the current behaviour (leave today unmarked → the next
# scheduled slot / login retries). The singleton lock keeps later slots from
# starting a competing run while we wait here.
DUO_DECISION_TIMEOUT = 3600  # 1 hour


def scrape():
    log("Scraping NUworks jobs...")
    # Run the scraper in its OWN process group with output to a file (not an
    # inherited pipe). On timeout we SIGKILL the whole group so a wedged
    # browser/login can never hang the run (today's 06-06 failure mode). Output
    # to scrape_last.log survives a kill, unlike buffered stdout.
    scrape_log = LOG_DIR / "scrape_last.log"
    with open(scrape_log, "w") as f:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "job_scraper.py")],
            cwd=str(HERE), stdout=f, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            rc = proc.wait(timeout=SCRAPE_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            raise
    if rc == 3:
        # DUO push wasn't approved in the scraper's window — a normal, expected
        # outcome that main() handles by asking the user to rerun or skip.
        raise DuoNotApproved()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, "job_scraper.py")


def scrape_with_duo_retry() -> bool:
    """Run the scrape; if the DUO push isn't approved in the scraper's window, ask
    the user (Telegram, two inline buttons) whether to resend the push and try
    again now, or skip for today — and act on the tap. Loops so the user can rerun
    as many times as they like (each rerun is a fresh login + push + scrape).

    Returns True if the scrape succeeded (proceed with the run), or False if the
    run should stop here (skip / no response / a non-DUO failure)."""
    while True:
        try:
            scrape()
            return True  # scrape succeeded — proceed to the rest of the pipeline
        except DuoNotApproved:
            log("DUO not approved in the scraper's window — asking the user "
                "(Telegram) whether to rerun or skip.")
            if not tg.configured():
                log("Telegram not configured — can't ask. Leaving today unmarked "
                    "so it retries on next login.")
                return False
            nonce = uuid.uuid4().hex[:8]
            tg.send_duo_decision(nonce=nonce)
            # This is a human wait of up to an hour — extend the watchdog so it
            # isn't mistaken for a hang, and keep enough budget for a re-scrape.
            bump_watchdog(DUO_DECISION_TIMEOUT + SCRAPE_BUDGET + 120)
            choice = tg.wait_for_duo_decision(nonce=nonce, timeout=DUO_DECISION_TIMEOUT)
            log(f"DUO decision: {choice}")
            if choice == "rerun":
                tg.send_message("🔁 Rerunning now — I'll send a fresh DUO push. "
                                "Approve it on your phone.")
                bump_watchdog(SCRAPE_BUDGET)
                continue  # loop back and re-scrape (fresh login + push)
            if choice == "skip":
                mark_ran_today()  # explicit skip → don't fire again today
                tg.send_message("⏭️ Skipped today's NUworks run as requested. "
                                "I won't prompt for DUO again until tomorrow.")
                log("User chose SKIP — marked today done so no further slots fire.")
                return False
            # No response in time — fall back to the existing behaviour: leave
            # today unmarked so the next scheduled slot / login retries.
            tg.send_message("⏳ No response to the DUO prompt — I'll leave today's "
                            "run open and try again at the next scheduled time.")
            log("No DUO decision in time — leaving today unmarked; next slot retries.")
            return False
        except subprocess.CalledProcessError:
            # A scrape/login failure that ISN'T a DUO timeout — e.g. the jobs API
            # couldn't be reached (scraper exit 4), the login form changed, or a
            # network blip. Don't consume today's run — retry on next login. Alert
            # so this never again passes silently as "0 new jobs" (the 2026-06-25
            # failure, where a missed fetch fell back to a stale jobs.csv).
            log("Scrape/login failed (non-DUO error). Will retry on next login.")
            try:
                if tg.configured():
                    tg.send_message("⚠️ NUworks scrape failed (couldn't fetch the "
                                    "jobs API / login issue — not DUO). Day left "
                                    "unmarked, it'll retry on next login. Check "
                                    "logs/scrape_last.log.")
            except Exception:
                pass
            return False
        except subprocess.TimeoutExpired:
            log(f"Scrape exceeded {SCRAPE_TIMEOUT}s (browser/login likely wedged). "
                "Not consuming the day — retry on next login.")
            try:
                if tg.configured():
                    tg.send_message("⏱️ NUworks scrape hung and was killed — run left "
                                    "unmarked, it'll retry on next login.")
            except Exception:
                pass
            return False


def load_jobs() -> list[dict]:
    return list(csv.DictReader(open(JOBS_CSV)))


def eligible(jobs: list[dict]) -> list[dict]:
    """Keep only in-portal, not-yet-applied jobs that aren't on the skip-list or the
    supervisor's wontfix list (jobs evicted from auto-retry as un-satisfiable)."""
    skiplist = load_skiplist()
    wontfix = load_wontfix()
    ids = [j["job_id"] for j in jobs]
    classified = asyncio.run(apply_type.classify_jobs(ids))
    cls = {c["job_id"]: c for c in classified}
    keep = []
    for j in jobs:
        c = cls.get(j["job_id"], {})
        j["_apply_type"] = c.get("type")
        j["_required_docs"] = c.get("required_docs", [])
        if j["job_id"] in skiplist:
            log(f"  skip [skiplist] {j['title'][:50]}")
        elif j["job_id"] in wontfix:
            log(f"  skip [wontfix] {j['title'][:50]}")
        elif c.get("type") == "in_portal":
            keep.append(j)
        else:
            log(f"  skip [{c.get('type')}] {j['title'][:50]} "
                + (f"-> {c.get('target')}" if c.get("target") else ""))
    return keep


def merge_pending(todo: list[dict]) -> list[dict]:
    """Add previously timed-out jobs back into today's queue (H5), but only if the
    portal still shows them in-portal and NOT already applied — so a job that was
    finished manually (or whose deadline passed) is dropped, never re-applied."""
    pend = load_pending()
    have = {j["job_id"] for j in todo}
    extra = [p for p in pend if p.get("job_id") and p["job_id"] not in have]
    if not extra:
        return todo
    skiplist = load_skiplist()
    wontfix = load_wontfix()
    cls = {c["job_id"]: c for c in
           asyncio.run(apply_type.classify_jobs([p["job_id"] for p in extra]))}
    out = list(todo)
    for p in extra:
        c = cls.get(p["job_id"], {})
        if p["job_id"] in skiplist or p["job_id"] in wontfix:
            clear_pending(p["job_id"])
        elif c.get("type") == "in_portal":
            p["_apply_type"] = "in_portal"
            p["_required_docs"] = c.get("required_docs", p.get("_required_docs") or [])
            out.append(p)
            log(f"  re-queued (prior approval timeout): {str(p.get('title',''))[:50]}")
        else:
            clear_pending(p["job_id"])  # applied/closed/external now — no retry needed
            log(f"  pending dropped [{c.get('type')}]: {str(p.get('title',''))[:50]}")
    return out


def _tailor_one(job: dict) -> dict:
    """Tailor ONE job's resume (reusing a 1-page PDF if already present). Pure worker
    for the thread pool — returns a result dict; does no logging (the main thread logs
    as futures complete) and mutates nothing shared (tailor uses a per-job workdir)."""
    existing = tailor.latest_pdf(job)
    if existing:
        return {"pdf": existing, "reused": True}
    res = tailor.tailor_with_claude(job)
    if res["ok"]:
        return {"pdf": res["pdf"], "reused": False, "attempts": res.get("attempts"),
                "model": res.get("model"), "escalated": res.get("escalated")}
    return {"pdf": None, "fail": {"stage": "résumé tailoring", "code": res.get("fail_code"),
            "reason": res.get("fail_reason"), "log": res.get("log"),
            "attempts": res.get("attempts")}}


def tailor_all_parallel(todo: list, workers: int):
    """Tailor every shortlisted job's resume CONCURRENTLY up front (the slow claude -p
    calls), instead of one-at-a-time inside the apply loop. Each job's result is cached
    on the job dict as job["_resume_pdf"] (None on failure) so get_resume() just reuses
    it. Tailors are independent (per-job workdirs), so threads are safe. This collapses
    serial ~N×(minutes-per-resume) into roughly one wave."""
    if not todo:
        return
    workers = max(1, min(workers, len(todo)))
    log(f"tailoring {len(todo)} resume(s) in parallel (workers={workers})...")
    bump_watchdog(TAILOR_BUDGET + 600)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_tailor_one, j): j for j in todo}
        for fut in as_completed(futs):
            job = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"pdf": None, "fail": {"stage": "résumé tailoring", "code": "crash",
                     "reason": f"{type(e).__name__}: {e}"}}
            job["_resume_pdf"] = r["pdf"]
            done += 1
            t = (job.get("title", "") or "")[:40]
            if r["pdf"]:
                how = "reused" if r.get("reused") else (
                    "tailored" + (" (Opus)" if r.get("escalated") else ""))
                log(f"  [{done}/{len(todo)}] ✓ {how}: {t} -> {Path(r['pdf']).name}")
            else:
                job["_fail"] = r.get("fail")
                log(f"  [{done}/{len(todo)}] ✗ tailor FAILED: {t} "
                    f"[{(r.get('fail') or {}).get('code')}]")
            bump_watchdog(TAILOR_BUDGET)  # keep the watchdog alive while others finish
    log(f"parallel tailoring finished in {int(time.time() - t0)}s "
        f"({sum(1 for j in todo if j.get('_resume_pdf'))}/{len(todo)} ready)")


def get_resume(job: dict) -> str | None:
    """Return a tailored PDF path: reuse if present, else tailor via claude -p.
    On failure, stash a classified diagnosis on the job for the Telegram alert.
    Prefers the result of the parallel tailoring wave (job["_resume_pdf"], possibly
    None on failure); otherwise reuses/tailors inline (the legacy serial path)."""
    if "_resume_pdf" in job:
        return job["_resume_pdf"]
    existing = tailor.latest_pdf(job)
    if existing:
        log(f"  using existing tailored resume: {Path(existing).name}")
        return existing
    log("  tailoring resume (ResumeBot2.1 via claude -p)...")
    res = tailor.tailor_with_claude(job)
    if res["ok"]:
        esc = " — escalated to Opus" if res.get("escalated") else ""
        log(f"  tailored: {Path(res['pdf']).name} (attempt {res.get('attempts')}, "
            f"{res.get('model','?')}{esc})")
        return res["pdf"]
    job["_fail"] = {"stage": "résumé tailoring", "code": res.get("fail_code"),
                    "reason": res.get("fail_reason"), "log": res.get("log"),
                    "attempts": res.get("attempts")}
    log(f"  ⚠ tailoring failed after {res.get('attempts')} attempt(s) "
        f"[{res.get('fail_code')}]: {res.get('fail_reason')}")
    return None


def _alert_failure(job: dict, fail: dict):
    """Telegram the user about a SYSTEM failure + its probable cause. The run is
    deliberately left unmarked so it re-fires live on next login for debugging."""
    title = job.get("title", "")[:60]
    msg = (
        "🛑 <b>Auto-apply failed — needs a look</b>\n\n"
        f"<b>{title}</b>\n{job.get('company','')}\n\n"
        f"<b>Stage:</b> {fail.get('stage')}  (<code>{fail.get('code')}</code>)\n"
        f"<b>Likely problem:</b> {fail.get('reason')}\n\n"
        f"Tried {fail.get('attempts','?')}×. Today was left <b>unmarked</b>, so the run "
        "will fire again <b>live</b> next time you log in — sit at the Mac and watch where "
        "it breaks."
    )
    if fail.get("log"):
        msg += f"\n\n📄 Attempt log: <code>{fail['log']}</code>"
    try:
        if tg.configured():
            tg.send_message(msg)
    except Exception:
        pass


def _result(job: dict, status: str, detail: str = "", system_failure: bool = False,
            extra: dict = None) -> dict:
    """Uniform apply result. ALWAYS carries job_id (the supervisor keys eviction / age-out
    / diagnosis by it) while keeping "job" as the title for the existing summary + the
    stats.record_run code. Pass `extra` to attach extra fields (resume path, ext links)."""
    r = {"job": (job.get("title", "") or "")[:55],
         "job_id": str(job.get("job_id", "") or ""),
         "result": status, "detail": detail or ""}
    if system_failure:
        r["system_failure"] = True
    if extra:
        for k, v in extra.items():
            if k not in r and v is not None:
                r[k] = v
    return r


def process(job: dict, dry_apply: bool) -> dict:
    title = job.get("title", "")[:55]
    log(f"JOB: {title} ({job.get('company','')})")

    # Give this job's tailoring its full budget on the watchdog clock.
    bump_watchdog(TAILOR_BUDGET + APPLY_BUDGET)

    pdf = get_resume(job)
    if not pdf:
        # SYSTEM failure: alert with the probable cause + leave today unmarked.
        _alert_failure(job, job.get("_fail", {"stage": "résumé tailoring"}))
        return _result(job, "tailor_failed", system_failure=True)

    if not tg.configured():
        log("  ⚠ Telegram not configured — cannot request approval. "
            "Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env.")
        return _result(job, "no_telegram", extra={"resume": pdf})

    label = tailor.expected_basename(job)
    log("  sending to Telegram for approval...")
    # Per-send nonce: the Approve/Reject buttons carry it, and wait_for_decision
    # only accepts a tap whose nonce matches THIS send. A stale tap on an old
    # message (e.g. from a prior run) can no longer auto-submit a newly tailored
    # resume the user never reviewed (C5).
    nonce = uuid.uuid4().hex[:8]
    tg.send_resume_for_approval(pdf, job, nonce=nonce)
    # An approval can legitimately take up to APPROVAL_TIMEOUT — extend the
    # watchdog so the human wait never counts as a "hang".
    bump_watchdog(APPROVAL_TIMEOUT + APPLY_BUDGET + 120)
    decision = tg.wait_for_decision(job["job_id"], nonce=nonce, timeout=APPROVAL_TIMEOUT)
    log(f"  decision: {decision}")

    if decision == "timeout":
        # Didn't answer in time — re-queue so it's offered again next run instead
        # of being lost forever (the scrape only looks back 24h). H5.
        add_pending(job, "transient", "no approval tap in time")  # report consolidates this
        return _result(job, "timeout")
    if decision != "approve":
        clear_pending(job["job_id"])
        add_to_skiplist(job["job_id"])  # prevent re-presentation in wider scrape window
        return _result(job, decision)

    # Cover letter — only generate one when the job actually requires it.
    cover_pdf = None
    if cover_letter.needs_cover_letter(job.get("_required_docs")):
        log("  cover letter required — generating (from JD + tailored resume)...")
        bump_watchdog(TAILOR_BUDGET + APPLY_BUDGET)
        resume_src = pdf.replace(".pdf", ".tex") if Path(pdf.replace(".pdf", ".tex")).exists() else pdf
        cl = cover_letter.generate_with_claude(job, resume_src)
        cover_pdf = cl.get("pdf")
        if cover_pdf:
            log(f"  cover letter: generated {Path(cover_pdf).name}")
            # C6: a required cover letter is generated by claude -p; send it for its
            # OWN Approve/Reject so it's never submitted to an employer sight-unseen.
            cl_nonce = uuid.uuid4().hex[:8]
            log("  sending cover letter to Telegram for approval...")
            tg.send_cover_letter_for_approval(cover_pdf, job, nonce=cl_nonce)
            bump_watchdog(APPROVAL_TIMEOUT + APPLY_BUDGET + 120)
            cl_decision = tg.wait_for_decision(job["job_id"], nonce=cl_nonce,
                                               timeout=APPROVAL_TIMEOUT)
            log(f"  cover letter decision: {cl_decision}")
            if cl_decision == "timeout":
                add_pending(job, "transient", "no cover-letter approval tap in time")
                return _result(job, "cover_letter_timeout")
            if cl_decision != "approve":
                clear_pending(job["job_id"])
                return _result(job, "cover_letter_rejected")
        else:
            # Required cover letter could not be produced -> system failure.
            job["_fail"] = {"stage": "cover letter", "code": cl.get("fail_code"),
                            "reason": cl.get("fail_reason"), "log": cl.get("log"),
                            "attempts": cl.get("attempts")}
            log(f"  ⚠ cover letter FAILED [{cl.get('fail_code')}]: {cl.get('fail_reason')}")
            _alert_failure(job, job["_fail"])
            return _result(job, "cover_letter_failed", system_failure=True)

    mode = "dry" if dry_apply else "submit"
    transcript = str(TRANSCRIPT) if TRANSCRIPT.exists() else None
    bump_watchdog(APPLY_BUDGET)
    log(f"  auto-applying (mode={mode}, transcript={'yes' if transcript else 'no'}, "
        f"cover_letter={'yes' if cover_pdf else 'no'})...")
    res = asyncio.run(auto_apply.apply_to_job(job["job_id"], pdf, label, mode=mode,
                                              transcript_pdf=transcript,
                                              cover_letter_pdf=cover_pdf))
    log(f"  apply result: {res['status']} — {res['detail']}")
    if res["status"] == "applied":
        clear_pending(job["job_id"])  # definitively done — drop from the re-queue

    # submitted_unverified: Submit was clicked but neither the page text nor the portal
    # flag confirmed it. Re-queue as verify_gap + keep the day MARKED (not a system
    # failure): the supervisor re-checks next run and the double-apply guard makes a
    # re-apply safe (it skips if the portal now shows applied). The consolidated report
    # surfaces it under "retrying"; leaving the day unmarked would re-fire forever.
    if res["status"] == "submitted_unverified":
        add_pending(job, "verify_gap", res["detail"])
        return _result(job, "submitted_unverified", detail=res["detail"])

    # (per-job outcome ping removed — the supervisor's consolidated report covers applied /
    # blocked / retrying outcomes; see the run_supervisor hook at the end of main().)
    # A portal error (not a clean terminal state) is a system failure -> retry live.
    if res["status"] == "error":
        return _result(job, "error", detail=res["detail"], system_failure=True)

    # External apply links from the posting — auto-fill + Telegram submit-gate.
    # external_autofill fills every mappable field from profile.json, sends you
    # the values + a Submit button, self-heals validation errors on your tap, and
    # stacks captcha/login-wall jobs for the laptop (never auto-solves captchas).
    ext = [u.strip() for u in (job.get("apply_urls", "") or "").split(";") if u.strip()]
    for u in ext:
        # The in-portal apply already succeeded above; an error in the external
        # step (tracking OR autofill) must stay isolated to this link and never
        # bubble up to fail the whole job. track() is inside the try too — a bad
        # record once crashed it BEFORE process_job ran, dropping the apply.
        try:
            external_apply.track(job, u, external_apply.detect_ats(u), "tracked",
                                 "external apply link from NUworks posting")
            external_autofill.process_job(
                u, resume_path=pdf, dry=dry_apply,
                job_meta={"title": job.get("title", ""), "company": job.get("company", "")})
        except Exception as e:
            log(f"  external autofill error for {u}: {e}")
            try:
                tg.send_message(f"⚠️ External autofill error for {title} ({u}). "
                                "Tracked — finish this one manually.")
            except Exception:
                pass
    if ext:
        log(f"  external apply links handled: {ext}")

    return _result(job, res["status"], detail=res["detail"], extra={"external": ext})


def main():
    args = sys.argv[1:]
    do_scrape = "--no-scrape" not in args
    dry_apply = "--dry-apply" in args
    force = "--force" in args
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    log("=" * 55)
    log("NUworks auto-apply pipeline starting")
    log(f"telegram configured: {tg.configured()} | dry_apply: {dry_apply} | force: {force}")

    # Refuse to run if another instance is already active (e.g. a previous slot
    # still waiting on a Telegram approval). Prevents overlapping runs from
    # double-submitting or stealing each other's Telegram taps.
    if not acquire_singleton_lock():
        log("Another pipeline run is already active (lock held) — exiting.")
        return

    # Once-per-day guard: the LaunchAgent fires at 10:00 AND on every login
    # (RunAtLoad), so a run missed while the Mac was off catches up at next
    # power-on — but only the first run of the day actually proceeds.
    if already_ran_today() and not force:
        log("Already ran today — skipping (use --force to override).")
        return

    # Backstop against any hung step (the 2026-06-05 run hung ~113 min). The
    # deadline is extended phase-by-phase as we go; if it's ever passed the run
    # is aborted WITHOUT marking the day, so it retries live on next login.
    _start_watchdog(SCRAPE_BUDGET)

    if do_scrape and not scrape_with_duo_retry():
        return  # skipped / no response / non-DUO failure — stop here

    bump_watchdog(180)  # classification / parsing headroom

    jobs = load_jobs()
    log(f"Loaded {len(jobs)} jobs from CSV")

    todo = eligible(jobs)  # also classifies every job (sets _apply_type) for the alert
    # Heads-up on freshly-found postings (title/company/type), deduped across slots.
    stats.announce_new_postings(jobs)
    todo = merge_pending(todo)  # re-add jobs whose approval timed out previously (H5)
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} in-portal jobs to process")

    # Tailor ALL resumes concurrently up front (the slow claude -p calls), right
    # after the jobs are scraped + classified — instead of one-at-a-time inside the
    # apply loop. Each job's PDF is cached on job["_resume_pdf"]; get_resume() reuses
    # it. Collapses serial N×(minutes-per-resume) into ~one wave (mirrors handshake).
    tailor_all_parallel(todo, TAILOR_PARALLELISM)

    # Crash-proof each job: a transient Telegram/network/Playwright error in one
    # job must NOT abort the whole day's remaining jobs (H1). A crash is treated
    # as a system failure so the day is left unmarked and retries next login.
    results = []
    for j in todo:
        try:
            results.append(process(j, dry_apply))
        except Exception as e:
            import traceback
            jt = (j.get("title", "?") or "?")[:55]
            log(f"  ✗ UNHANDLED error on {jt}: {type(e).__name__}: {e}")
            log(traceback.format_exc())
            try:
                if tg.configured():
                    tg.send_message(
                        f"🛑 Crashed while processing <b>{jt}</b>\n"
                        f"<code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
                        "Day left unmarked → it'll retry live next login.")
            except Exception:
                pass
            results.append(_result(j, "crashed", system_failure=True))

    log("=" * 55)
    log("SUMMARY:")
    for r in results:
        log(f"  {r['result']:<16} {r['job']}")

    # Only commit the day if NOTHING failed at the system level. If a tailor /
    # cover-letter / apply step broke, leave today unmarked so the run re-fires
    # live next login (the user can then watch where it breaks).
    failures = [r for r in results if r.get("system_failure")]
    if failures:
        log(f"{len(failures)} system failure(s) — leaving today UNMARKED so the run "
            "retries live on next login.")
    else:
        mark_ran_today()
        log("Run clean — marked as done for today.")

    # Record this run's outcomes for the nightly digest (best-effort).
    stats.record_run(jobs, results, marked=not failures)

    # Supervisor: classify every outcome, break doomed retry loops (evict un-satisfiable
    # jobs to data/wontfix.json so eligible()/merge_pending() stop re-offering them), and
    # send ONE consolidated report — replacing the old scattered per-result pings + this
    # run's summary blob. Runs AFTER the mark/unmark decision so it can never affect it;
    # wrapped so a supervisor error never fails the run. Brain stays OFF for this port.
    try:
        import supervisor
        supervisor.run_supervisor(results, marked=not failures, use_brain=False)
    except Exception as e:
        log(f"supervisor hook error (run still completed): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
