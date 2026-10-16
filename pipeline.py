#!/usr/bin/env python3
"""
NUworks daily auto-apply orchestrator.

Flow (HYBRID_PIPELINE_PLAN.md):
  1. Scrape today's jobs              (job_scraper.py -> data/jobs.csv)
  2. Classify apply method            (apply_type.py) -> keep in_portal, not applied
  3. Base ATS audit, once per run     (resume_agents.audit_bases_cached)
  4. Send ALL posting cards at once   (telegram_bot) — no LLM sees a JD before Approve
  5-11. Per approved job, in parallel: Haiku keywords → verify/score → low-match
        card → tailor → post-check → verdict → resume card (Scan → score card →
        Retailor loop)
  12-14. Cover letter (own approval) → auto-apply (one browser at a time) → external
  15. Summary, mark/unmark day, stats, supervisor report

The Telegram approvals are the human-in-the-loop gates: NOTHING is tailored without
a posting Approve and NOTHING is submitted without a resume Approve.

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
import resume_agents as ra
import telegram_bot as tg
import events

# external_autofill loads its OWN copy of telegram_bot by file path (so wellfound can
# share it). Inside this pipeline it must use the shared module instead — otherwise
# its approval wait runs a second getUpdates poller next to tg.DISPATCHER and the two
# steal each other's taps.
external_autofill.tg = tg

HERE = Path(__file__).resolve().parent
JOBS_CSV = HERE / "data" / "jobs.csv"
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Pause flag toggled by /pause nuworks and /continue nuworks (claude-bridge/
# bridge.py) — checked at the top of every sibling pipeline against its own name.
PAUSE_FLAG = HERE.parent / ".autoapply_paused.nuworks"

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
# each up to tailor.GEN_TIMEOUT (any attempt can be a fresh generation), with capped linear backoff between regenerations +
# pdflatex time. The budget must cover that real worst case or the watchdog hard-
# kills a healthy-but-slow tailor that's legitimately escalating to Opus (H2).
_TAILOR_ATTEMPTS = tailor.max_total_attempts()
_TAILOR_BACKOFF  = claude_runner.DEFAULT_BACKOFF * sum(min(i, 4) for i in range(1, _TAILOR_ATTEMPTS))
TAILOR_BUDGET   = _TAILOR_ATTEMPTS * max(tailor.GEN_TIMEOUT, claude_runner.DEFAULT_TIMEOUT) + _TAILOR_BACKOFF + 300
APPLY_BUDGET    = 300                                                  # one portal apply

# How many resumes to tailor concurrently up front (the slow claude -p calls).
# Mirrors handshake's apply.tailor_parallelism (default 6). Each job tailors in
# its own workdir via an independent claude -p, so the wave is thread-safe.
TAILOR_PARALLELISM = int(os.environ.get("NUWORKS_TAILOR_PARALLELISM", "6"))

# ── Hybrid pipeline (HYBRID_PIPELINE_PLAN.md §4–§5) ─────────────────────────
POSTING_TIMEOUT   = int(ra.agent_cfg("posting_timeout_s"))     # posting card window
LOW_MATCH_TIMEOUT = int(ra.agent_cfg("low_match_timeout_s"))   # low-match card window
KEYWORD_BUDGET    = int(ra.agent_cfg("keyword_attempts")) * 200
HYBRID_BUDGET     = KEYWORD_BUDGET + TAILOR_BUDGET             # Steps 5–8 worst case
SCAN_BUDGET       = int(ra.agent_cfg("bi_scan_timeout_s")) + 300   # scan + delete/reset
RETAILOR_BUDGET   = TAILOR_BUDGET
ATS_AUDIT_BUDGET  = int(ra.agent_cfg("ats_fix_attempts")) * claude_runner.DEFAULT_TIMEOUT * 2 + 300
BI_MIN_POLL_S     = 20   # Big Interview: never poll faster than this (invariant 9)

# Up to TAILOR_PARALLELISM jobs run claude -p at once; a job WAITING on a Telegram
# card holds no slot. Every posting card gets its own (cheap) waiter thread.
LLM_SLOTS  = threading.BoundedSemaphore(TAILOR_PARALLELISM)
APPLY_LOCK = threading.Lock()   # one browser session (NUworks / external ATS) at a time
SCAN_LOCK  = threading.Lock()   # one Big Interview scan in flight, no parallel MCP calls
_scans_lock = threading.Lock()
SCANS_FILE = HERE / "data" / "scans.json"
SCAN_RESET_STAMP = HERE / "data" / "scan_reset_date.txt"  # limit reset: at most once a day

_watchdog_deadline = None  # wall-clock time the run must not pass
_scrape_pgid = None  # process group of the live scrape() subprocess, if any (so the
                     # watchdog can kill it too instead of orphaning it — the 06-10 and
                     # 09-12 incidents: a sleep/wake stall trips the watchdog, but without
                     # this the scrape subprocess keeps running unattended and its result
                     # (session.json / jobs.csv) is silently never consumed by anyone)


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
                if _scrape_pgid is not None:
                    try:
                        os.killpg(_scrape_pgid, signal.SIGKILL)
                    except Exception:
                        pass
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


_watchdog_lock = threading.Lock()  # job workers bump concurrently


def bump_watchdog(extra: int):
    """Push the watchdog deadline out by `extra` seconds from now (only ever
    later, never earlier)."""
    global _watchdog_deadline
    with _watchdog_lock:
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
_pending_lock = threading.RLock()  # job workers update the queue concurrently


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
    with _pending_lock:
        _add_pending_locked(job, failure_class, detail)


def _add_pending_locked(job: dict, failure_class: str, detail: str):
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
                                    "salary", "posted", "deadline", "description", "apply_urls",
                                    "_apply_type", "_required_docs")}
    keep["_queued_at"] = now
    keep["_first_queued"] = now
    keep["_attempts"] = 1
    keep["_last_class"] = failure_class
    keep["_last_detail"] = (detail or "")[:300]
    items.append(keep)
    save_pending(items)


def clear_pending(job_id: str):
    with _pending_lock:
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
    with _pending_lock, open(SKIP_FILE, "a") as f:
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
    global _scrape_pgid
    scrape_log = LOG_DIR / "scrape_last.log"
    with open(scrape_log, "w") as f:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "job_scraper.py")],
            cwd=str(HERE), stdout=f, stderr=subprocess.STDOUT,
            start_new_session=True)
        _scrape_pgid = proc.pid  # start_new_session means pgid == pid; lets the
                                 # top-level watchdog kill it too if IT trips first
        try:
            rc = proc.wait(timeout=SCRAPE_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            raise
        finally:
            _scrape_pgid = None
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


def _msg(text: str):
    """Best-effort Telegram message (never raises)."""
    try:
        if tg.configured():
            tg.send_message(text)
    except Exception as e:
        log(f"  telegram message failed: {type(e).__name__}: {e}")


# ── Big Interview (plan §4A) ─────────────────────────────────────────────────

def _bi(cmd: str, **args) -> dict:
    """One Big Interview MCP call via bi_scan.py (callers hold SCAN_LOCK). Always
    returns the MCP envelope {ok, data} / {ok: False, error: {code, message, hint}}."""
    try:
        import bi_scan
    except ImportError as e:
        return {"ok": False, "error": {"code": "unavailable", "message": str(e), "hint": ""}}
    return bi_scan.call(cmd, **args)


def _bi_err(cmd: str, r: dict) -> str:
    e = (r or {}).get("error") or {}
    return (f"⚠️ Big Interview {cmd} failed ({e.get('code', '?')}): {e.get('message', '')}"
            + (f"\nHint: {e['hint']}" if e.get("hint") else ""))


def _scan_button() -> dict | None:
    """Scan button state, read once per resume card. None → no scan UI (MCP not set
    up). Never blocks on an in-flight scan: then the button shows without a count."""
    if not SCAN_LOCK.acquire(blocking=False):
        return {"available": True, "left": None}
    try:
        r = _bi("auth")
    finally:
        SCAN_LOCK.release()
    if not r.get("ok"):
        if (r.get("error") or {}).get("code") == "unavailable":
            return None
        return {"available": True, "left": None}  # MCP error surfaces when tapped
    d = r.get("data") or {}
    return {"available": bool(d.get("logged_in")), "left": d.get("scans_remaining")}


def _record_scan(job: dict, scan_id: str, version: int, medal, items=None):
    with _scans_lock:
        try:
            rows = json.loads(SCANS_FILE.read_text())
        except Exception:
            rows = []
        for r in rows:
            if r.get("scan_id") == scan_id and r.get("version") == version \
                    and r.get("job_id") == job["job_id"]:
                if items is not None:
                    r["items"] = items
                break
        else:
            rows.append({"job_id": job["job_id"], "scan_id": scan_id, "version": version,
                         "medal": medal, "date": date.today().isoformat(),
                         "items": items or []})
        SCANS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCANS_FILE.write_text(json.dumps(rows, indent=2))


def _limit_reset() -> tuple:
    """Plan §4A.4 (D17): at 0/5, delete the N oldest scans in My Scans (each delete
    backs up its feedback first — MCP built-in) and re-check the allowance. Runs at
    most once per calendar day. Returns (ok, message, scans_left)."""
    today = date.today().isoformat()
    if SCAN_RESET_STAMP.exists() and SCAN_RESET_STAMP.read_text().strip() == today:
        return False, ("Scan allowance is 0/5 and today's reset already ran — scans "
                       "resume at midnight."), 0
    SCAN_RESET_STAMP.parent.mkdir(parents=True, exist_ok=True)
    SCAN_RESET_STAMP.write_text(today)  # stamp FIRST: never twice a day, even on a crash
    n = int(ra.agent_cfg("bi_reset_delete_count"))
    scans, cursor = [], None
    for _ in range(50):  # bounded paging
        r = _bi("list", limit=50, cursor=cursor)
        if not r.get("ok"):
            return False, _bi_err("list", r), 0
        scans += (r.get("data") or {}).get("scans") or []
        cursor = (r.get("data") or {}).get("next_cursor")
        if not cursor:
            break
    # Undated rows can't be ordered by age → never deleted.
    oldest = sorted((x for x in scans if x.get("scanned_at")),
                    key=lambda x: x["scanned_at"])[:n]
    deleted = 0
    for x in oldest:
        r = _bi("delete", scan_id=x["scan_id"])
        if not r.get("ok"):
            _msg(_bi_err("delete", r))
            break
        deleted += 1
    log(f"  scan limit reset: deleted {deleted}/{len(oldest)} oldest scan(s)")
    a = _bi("auth")
    left = ((a.get("data") or {}).get("scans_remaining") if a.get("ok") else None) or 0
    if left > 0:
        _msg(f"♻️ Deleted {deleted} oldest scan(s) (feedback backed up) — allowance now {left}/5.")
        return True, "", left
    return False, (f"Deleted {deleted} old scan(s) but the allowance did not reset "
                   f"({left}/5). Scans resume at midnight."), 0


def _run_scan(job: dict, st: dict) -> tuple:
    """Plan §4A.3 steps 2–7, called under SCAN_LOCK. Returns (feedback, scans_left,
    error_text). MCP errors are surfaced with their hint and NEVER retried."""
    bump_watchdog(SCAN_BUDGET)
    a = _bi("auth")
    if not a.get("ok"):
        return None, None, _bi_err("auth", a)
    d = a.get("data") or {}
    if not d.get("logged_in"):
        return None, None, "🔐 Big Interview login expired — run scripts/login.py"
    left = d.get("scans_remaining")
    if left == 0:
        ok, why, left = _limit_reset()
        if not ok:
            return None, 0, why
    s = _bi("start", resume_path=str(Path(st["pdf"]).resolve()),
            job_title=job.get("title", ""), company=job.get("company", ""),
            job_description=job.get("description", ""),
            scoring_guide=ra.agent_cfg("bi_scoring_guide"))
    if not s.get("ok"):
        return None, left, _bi_err("start", s)
    sd = s.get("data") or {}
    sid, state = sd.get("scan_id"), sd.get("state")
    left = sd.get("scans_remaining", left)
    poll = max(BI_MIN_POLL_S, int(ra.agent_cfg("bi_poll_s")))
    deadline = time.time() + int(ra.agent_cfg("bi_scan_timeout_s"))
    hint = ""
    while state != "complete":
        if state == "failed":
            return None, left, f"❌ Big Interview reported the scan failed (scan {sid}). {hint}".strip()
        if state == "unknown":
            return None, left, (f"❓ Scan state unreadable (scan {sid}) — not retrying. "
                                f"{hint}").strip()
        if time.time() + poll > deadline:
            return None, left, f"⏱️ Scan timed out — scan_id {sid} kept, try again later."
        time.sleep(poll)
        r = _bi("status", scan_id=sid)
        if not r.get("ok"):
            return None, left, _bi_err("status", r)
        rd = r.get("data") or {}
        state, hint = rd.get("state"), rd.get("hint") or ""
        left = rd.get("scans_remaining", left)
    f = _bi("feedback", scan_id=sid)
    if not f.get("ok"):
        return None, left, _bi_err("feedback", f)
    return f.get("data") or {}, left, None


def _resume_state(job: dict, ctx: dict, res: dict, match, kw, version: int,
                  round_no: int, prev_scan: str) -> tuple:
    """Steps 9–10 on a tailored / retailored version. Returns (state | None, post)."""
    post = ra.verify_tailored_pdf(res["pdf"], res.get("tex"), kw)
    if not post["ok"]:
        return None, post
    tex_text = Path(res["tex"]).read_text() if res.get("tex") and Path(res["tex"]).exists() else ""
    base = ra.detect_base(tex_text, ctx["base_texts"]) if tex_text else None
    verdict = ra.build_verdict(
        ats_status=(ctx["audit"].get(base) or {}).get("status", "green"),
        match=match, base=base, post=post,
        xyz=tailor.xyz_bullet_stats(tex_text) if tex_text else None,
        model=res.get("model", ""), attempts=res.get("attempts"),
        escalated=res.get("escalated", False))
    log(f"  verdict v{version}: {verdict}")
    return {"pdf": res["pdf"], "tex": res.get("tex"), "version": version, "round": round_no,
            "prev_scan": prev_scan, "verdict": verdict, "match": match, "kw": kw}, post


def _scan_summary(fb: dict) -> str:
    cats = fb.get("categories") or {}
    return (f"{tg.medal_label(fb.get('medal'))} · "
            f"ATS Fit: {tg.medal_label((cats.get('ats_fit') or {}).get('badge'))}")


def _scan_and_retailor(job: dict, ctx: dict, st: dict):
    """Plan §4A: scan → score card → (Retailor | Back). Returns the state for the
    next resume card (same version on Back / any failure, vN+1 after a successful
    retailor) or "timeout" when the score card got no tap."""
    if not SCAN_LOCK.acquire(blocking=False):
        _msg("⏳ Queued — another scan is running.")
        SCAN_LOCK.acquire()
    try:
        fb, left, err = _run_scan(job, st)
    finally:
        SCAN_LOCK.release()
    if err:
        log(f"  scan: {err}")
        _msg(err)
        return st
    sid = fb.get("scan_id", "")
    _record_scan(job, sid, st["version"], fb.get("medal"))
    nonce = uuid.uuid4().hex[:8]
    card = tg.send_score_card(
        job, fb, nonce, version=st["version"], scans_left=left, round_no=st["round"],
        max_rounds=int(ra.agent_cfg("retailor_max_rounds")),
        max_toggles=int(ra.agent_cfg("bi_max_toggles")),
        categories=tuple(ra.agent_cfg("bi_correction_categories")))
    _record_scan(job, sid, st["version"], fb.get("medal"), items=card.selectable)
    bump_watchdog(APPROVAL_TIMEOUT + RETAILOR_BUDGET + APPLY_BUDGET)
    dec = tg.wait_for_decision(job["job_id"], nonce=nonce, timeout=APPROVAL_TIMEOUT,
                               refuse=card.refuse)
    log(f"  score card decision: {dec}")
    if dec == "timeout":
        return "timeout"
    if dec != "retailor":
        return st  # Back → same version's resume card
    kws, corrs = card.picked()
    n = st["version"]
    if not (st.get("tex") and Path(st["tex"]).exists()):
        _msg(f"⚠️ Retailor unavailable: v{n}'s .tex is missing — here is v{n} again.")
        return st
    _msg(f"🔁 Retailoring v{n} → v{n + 1} with {len(kws)} keyword(s) and "
         f"{len(corrs)} correction(s)…")
    bump_watchdog(RETAILOR_BUDGET + APPROVAL_TIMEOUT + APPLY_BUDGET)
    with LLM_SLOTS:
        res = tailor.retailor(job, st["tex"], kws, corrs)
    if not res.get("ok"):
        _msg(f"⚠️ Retailor failed ({res.get('fail_code')}): {res.get('fail_reason')} "
             f"— here is v{n} again.")
        return st
    new, post = _resume_state(job, ctx, res, st["match"], st["kw"], res["version"],
                              st["round"] + 1, _scan_summary(fb))
    if new is None:
        _msg(f"⚠️ Retailored v{res['version']} failed the post-check "
             f"({', '.join(post.get('failed', []))}) — here is v{n} again.")
        return st
    return new


# ── Per-job worker (Steps 5–14) ──────────────────────────────────────────────

def _run_job(job: dict, ctx: dict, dry_apply: bool) -> dict:
    """Everything after a posting Approve, for ONE job: Haiku keywords → verify +
    score → low-match card → tailor → post-check → verdict → resume card (with the
    scan / retailor loop) → cover letter → apply. Blocks only this job's worker."""
    jid = job["job_id"]
    title = job.get("title", "")[:55]
    bump_watchdog(HYBRID_BUDGET + APPROVAL_TIMEOUT + APPLY_BUDGET)

    # Step 5–6: Haiku keywords + Python verify/score (degrades to Match n/a).
    d = tailor.stage(job)
    jd = (d / "job_description.txt").read_text()
    with LLM_SLOTS:
        kw = ra.extract_keywords(job, d)
    match = None
    if "error" in kw:
        log(f"  [{title}] keywords: {kw['error']} — Match n/a, tailor extracts its own")
    else:
        match = ra.score_keywords(kw, jd, ctx["base_texts"])
        sc = match["score"]
        log(f"  [{title}] match: {'n/a' if sc is None else f'{sc:.0%}'} "
            f"({match['must_hit']}/{match['must_total']} must), dropped={match['dropped']}")

    # Step 7: low-match card.
    if match and match["score"] is not None and match["score"] < float(ra.agent_cfg("min_match_score")):
        nonce = uuid.uuid4().hex[:8]
        tg.send_low_match_card(job, match, nonce=nonce)
        bump_watchdog(LOW_MATCH_TIMEOUT + HYBRID_BUDGET + APPROVAL_TIMEOUT + APPLY_BUDGET)
        dec = tg.wait_for_decision(jid, nonce=nonce, timeout=LOW_MATCH_TIMEOUT)
        log(f"  [{title}] low-match decision: {dec}")
        if dec == "lowno":
            clear_pending(jid)
            add_to_skiplist(jid)
            return _result(job, "skipped_low_match")
        if dec != "lowok":
            add_pending(job, "transient", "no low-match tap in time")
            return _result(job, "timeout", detail="no low-match tap")

    # Step 8: tailor (reuse a 1-page PDF from an earlier run if present).
    kwpass = ({**match["keywords"], "covered": match["covered"]}
              if match and match["score"] is not None else None)
    existing = tailor.latest_pdf(job)
    if existing:
        log(f"  [{title}] using existing tailored resume: {Path(existing).name}")
        res = {"ok": True, "pdf": existing, "tex": existing[:-4] + ".tex",
               "attempts": 0, "model": "reused", "version": 1}
    else:
        bump_watchdog(HYBRID_BUDGET + APPROVAL_TIMEOUT + APPLY_BUDGET)
        with LLM_SLOTS:
            res = tailor.tailor_with_claude(job, keywords=kwpass)
    if not res["ok"]:
        fail = {"stage": "résumé tailoring", "code": res.get("fail_code"),
                "reason": res.get("fail_reason"), "log": res.get("log"),
                "attempts": res.get("attempts")}
        log(f"  [{title}] ⚠ tailoring failed [{fail['code']}]: {fail['reason']}")
        _alert_failure(job, fail)
        return _result(job, "tailor_failed", system_failure=True)

    # Steps 9–10: post-check + verdict.
    st, post = _resume_state(job, ctx, res, match, kwpass, 1, 0, "")
    if st is None:
        # Quarantine so tailor.latest_pdf() (glob Resume_*.pdf) won't reuse it next run.
        bad = Path(res["pdf"])
        if bad.exists():
            bad.rename(bad.with_name("POSTCHECK_FAILED_" + bad.name))
        _alert_failure(job, {"stage": "post-check", "code": "postcheck_failed",
                             "reason": f"tailored PDF failed critical ATS checks: "
                                       f"{', '.join(post.get('failed', []))}",
                             "attempts": res.get("attempts")})
        return _result(job, "postcheck_failed", system_failure=True)

    # Step 11 (+ §4A loop): resume card until Approve / Reject / timeout.
    max_rounds = int(ra.agent_cfg("retailor_max_rounds"))
    while True:
        nonce = uuid.uuid4().hex[:8]
        tg.send_resume_for_approval(st["pdf"], job, summary=st["verdict"], nonce=nonce,
                                    scan=_scan_button(), version=st["version"],
                                    round_no=st["round"], max_rounds=max_rounds,
                                    prev_scan=st["prev_scan"])
        bump_watchdog(APPROVAL_TIMEOUT + SCAN_BUDGET + APPLY_BUDGET + 120)
        dec = tg.wait_for_decision(jid, nonce=nonce, timeout=APPROVAL_TIMEOUT)
        log(f"  [{title}] resume decision (v{st['version']}): {dec}")
        if dec == "approve":
            break
        if dec == "scan":
            nxt = _scan_and_retailor(job, ctx, st)
            if nxt == "timeout":
                add_pending(job, "transient", "no score-card tap in time")
                return _result(job, "timeout", detail="no score-card tap")
            st = nxt
            continue
        if dec == "timeout":
            add_pending(job, "transient", "no approval tap in time")
            return _result(job, "timeout")
        clear_pending(jid)
        add_to_skiplist(jid)  # prevent re-presentation in wider scrape window
        return _result(job, dec)

    if st["version"] > 1 and st.get("tex"):
        tailor.consolidate(Path(st["pdf"]), Path(st["tex"]))
    # The PDF submitted is always the version on the card that was approved.
    return _apply(job, st["pdf"], dry_apply)


def _apply(job: dict, pdf: str, dry_apply: bool) -> dict:
    """Steps 12–14: cover letter (own approval) → NUworks apply → external links.
    The browser steps (13–14) run under APPLY_LOCK: one browser session at a time.
    The cover letter (claude -p + a human wait) stays outside the lock so a pending
    approval never blocks other jobs' applies."""
    title = job.get("title", "")[:55]
    label = tailor.expected_basename(job)

    # Cover letter — only generate one when the job actually requires it.
    cover_pdf = None
    if cover_letter.needs_cover_letter(job.get("_required_docs")):
        log(f"  [{title}] cover letter required — generating (from JD + tailored resume)...")
        bump_watchdog(TAILOR_BUDGET + APPLY_BUDGET)
        resume_src = pdf.replace(".pdf", ".tex") if Path(pdf.replace(".pdf", ".tex")).exists() else pdf
        with LLM_SLOTS:
            cl = cover_letter.generate_with_claude(job, resume_src)
        cover_pdf = cl.get("pdf")
        if cover_pdf:
            log(f"  [{title}] cover letter: generated {Path(cover_pdf).name}")
            # C6: a required cover letter is generated by claude -p; send it for its
            # OWN Approve/Reject so it's never submitted to an employer sight-unseen.
            cl_nonce = uuid.uuid4().hex[:8]
            tg.send_cover_letter_for_approval(cover_pdf, job, nonce=cl_nonce)
            bump_watchdog(APPROVAL_TIMEOUT + APPLY_BUDGET + 120)
            cl_decision = tg.wait_for_decision(job["job_id"], nonce=cl_nonce,
                                               timeout=APPROVAL_TIMEOUT)
            log(f"  [{title}] cover letter decision: {cl_decision}")
            if cl_decision == "timeout":
                add_pending(job, "transient", "no cover-letter approval tap in time")
                return _result(job, "cover_letter_timeout")
            if cl_decision != "approve":
                clear_pending(job["job_id"])
                return _result(job, "cover_letter_rejected")
        else:
            # Required cover letter could not be produced -> system failure.
            fail = {"stage": "cover letter", "code": cl.get("fail_code"),
                    "reason": cl.get("fail_reason"), "log": cl.get("log"),
                    "attempts": cl.get("attempts")}
            log(f"  [{title}] ⚠ cover letter FAILED [{cl.get('fail_code')}]: {cl.get('fail_reason')}")
            _alert_failure(job, fail)
            return _result(job, "cover_letter_failed", system_failure=True)

    mode = "dry" if dry_apply else "submit"
    transcript = str(TRANSCRIPT) if TRANSCRIPT.exists() else None
    with APPLY_LOCK:
        bump_watchdog(APPLY_BUDGET * 2)
        log(f"  [{title}] auto-applying (mode={mode}, transcript={'yes' if transcript else 'no'}, "
            f"cover_letter={'yes' if cover_pdf else 'no'})...")
        res = asyncio.run(auto_apply.apply_to_job(job["job_id"], pdf, label, mode=mode,
                                                  transcript_pdf=transcript,
                                                  cover_letter_pdf=cover_pdf))
        log(f"  [{title}] apply result: {res['status']} — {res['detail']}")
        if res["status"] == "applied":
            clear_pending(job["job_id"])  # definitively done — drop from the re-queue

        # submitted_unverified: Submit was clicked but neither the page text nor the
        # portal flag confirmed it. Re-queue as verify_gap + keep the day MARKED (not a
        # system failure): the supervisor re-checks next run and the double-apply guard
        # makes a re-apply safe.
        if res["status"] == "submitted_unverified":
            add_pending(job, "verify_gap", res["detail"])
            return _result(job, "submitted_unverified", detail=res["detail"])

        # A portal error (not a clean terminal state) is a system failure -> retry live.
        if res["status"] == "error":
            return _result(job, "error", detail=res["detail"], system_failure=True)

        # External apply links from the posting — auto-fill + Telegram submit-gate
        # (external_autofill never auto-solves captchas or creates accounts).
        ext = [u.strip() for u in (job.get("apply_urls", "") or "").split(";") if u.strip()]
        for u in ext:
            # An error in the external step must stay isolated to this link and never
            # fail the whole job (the in-portal apply already succeeded above).
            try:
                external_apply.track(job, u, external_apply.detect_ats(u), "tracked",
                                     "external apply link from NUworks posting")
                external_autofill.process_job(
                    u, resume_path=pdf, dry=dry_apply,
                    job_meta={"title": job.get("title", ""), "company": job.get("company", "")})
            except Exception as e:
                log(f"  external autofill error for {u}: {e}")
                _msg(f"⚠️ External autofill error for {title} ({u}). "
                     "Tracked — finish this one manually.")
        if ext:
            log(f"  [{title}] external apply links handled: {ext}")

    return _result(job, res["status"], detail=res["detail"], extra={"external": ext})


def _crash_result(job: dict, e: Exception) -> dict:
    """A crash in one job must NOT abort the others (H1); it's a system failure."""
    import traceback
    jt = (job.get("title", "?") or "?")[:55]
    log(f"  ✗ UNHANDLED error on {jt}: {type(e).__name__}: {e}")
    log("".join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:])
    _msg(f"🛑 Crashed while processing <b>{jt}</b>\n"
         f"<code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
         "Day left unmarked → it'll retry live next login.")
    return _result(job, "crashed", system_failure=True)


def _posting_worker(job: dict, nonce: str, ctx: dict, dry_apply: bool) -> dict:
    """Wait on ONE posting card; an Approve starts that job's pipeline right away."""
    dec = tg.wait_for_decision(job["job_id"], nonce=nonce, timeout=POSTING_TIMEOUT)
    log(f"JOB: {job.get('title', '')[:55]} ({job.get('company', '')})  posting decision: {dec}")
    if dec == "approve":
        try:
            return _run_job(job, ctx, dry_apply)
        except Exception as e:
            return _crash_result(job, e)
    if dec == "timeout":
        add_pending(job, "transient", "no posting-approval tap in time")
        return _result(job, "timeout")
    clear_pending(job["job_id"])
    add_to_skiplist(job["job_id"])
    return _result(job, dec)


def run_postings(todo: list, ctx: dict, dry_apply: bool) -> list:
    """Step 4 + per-job workers: send EVERY posting card at once, then each card's
    Approve starts Steps 5–14 for that job immediately, in parallel (plan §5)."""
    results = []
    tg.DISPATCHER.start()
    try:
        bump_watchdog(POSTING_TIMEOUT + HYBRID_BUDGET + APPROVAL_TIMEOUT
                      + APPLY_BUDGET * max(1, len(todo)) + 300)
        sent = []
        for job in todo:
            nonce = uuid.uuid4().hex[:8]
            try:
                tg.send_job_for_approval(job, nonce=nonce)
                sent.append((job, nonce))
            except Exception as e:
                log(f"  posting card send failed for {job.get('title', '')[:50]}: {e}")
                add_pending(job, "transient", "posting card send failed")
                results.append(_result(job, "timeout", detail="posting card send failed"))
        log(f"sent {len(sent)} posting card(s)")
        if sent:
            with ThreadPoolExecutor(max_workers=len(sent)) as ex:
                futs = {ex.submit(_posting_worker, j, n, ctx, dry_apply): j for j, n in sent}
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        results.append(_crash_result(futs[fut], e))
    finally:
        tg.DISPATCHER.stop()
    return results


def audit_or_stop() -> dict | None:
    """Step 3: base ATS audit once per run, before any card. Any red → ONE alert and
    the run stops (day unmarked). Returns ctx {audit, base_texts} or None."""
    bump_watchdog(ATS_AUDIT_BUDGET)
    audit = ra.audit_bases_cached()
    for name, a in audit.items():
        log(f"  base ATS audit: {name} → {a['status']}{' (cached)' if a.get('cached') else ''}")
    red = {n: a for n, a in audit.items() if a["status"] == "red"}
    if red:
        detail = "; ".join(f"{n}: {', '.join(a['report'].get('failed', []))}" for n, a in red.items())
        log(f"Base resume(s) fail ATS checks — stopping, day unmarked: {detail}")
        _msg(f"🛑 Base resume fails ATS checks: {detail}. Fix the template. "
             "Day left unmarked.")
        return None
    tailor.BASE_OVERRIDES.clear()
    tailor.BASE_OVERRIDES.update({n: a["usable_tex"] for n, a in audit.items()
                                  if a["status"] == "yellow"})
    return {"audit": audit,
            "base_texts": {n: Path(a["usable_tex"]).read_text() for n, a in audit.items()}}


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

    if PAUSE_FLAG.exists():
        log("Paused via Telegram /pause — skipping this run.")
        return

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
    events.instrument(sys.modules[__name__])  # live trace for the pipeline-live pane

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

    # Step 3–14: no LLM sees a JD before its posting Approve. Without Telegram there
    # is no human gate, so nothing is tailored at all.
    if not todo:
        results = []
    elif not tg.configured():
        log("  ⚠ Telegram not configured — cannot ask for posting approval; nothing tailored.")
        results = [_result(j, "no_telegram") for j in todo]
    else:
        ctx = audit_or_stop()
        if ctx is None:
            return  # base resume red → alert sent, day unmarked
        results = run_postings(todo, ctx, dry_apply)

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
