#!/usr/bin/env python3
"""
Visibility helpers (added 2026-06-11):

1. announce_new_postings(jobs) — right after a scrape, Telegram the postings we
   haven't announced before (title — company — apply type). Deduped via
   announced_jobs.json so the day's retry slots don't re-spam. Covers postings
   that later get SKIPPED too (external/email/off-target), which otherwise produce
   no message at all. On a day with NOTHING new it still sends a one-line
   "📭 0 new job postings" notice (once/day) so a quiet day is confirmed, never
   silent.
2. record_run(jobs, results, marked) — append this run's outcomes to a per-day
   stats file that the nightly digest (digest.py) reads.

ALL file I/O is best-effort: these never raise into the pipeline.
"""
import json
import html
from pathlib import Path
from datetime import date, datetime

import telegram_bot as tg

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ANNOUNCED = DATA / "announced_jobs.json"      # {job_id: {date,title,company,type}}
DAILY_DIR = DATA / "daily"                    # one YYYY-MM-DD.json per day

_TYPE_LABEL = {"in_portal": "auto-apply", "email": "email", "external": "external",
               "already_applied": "already applied", "unknown": "manual"}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p: Path, obj):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2))
    except Exception:
        pass


def today_path(d: date = None) -> Path:
    return DAILY_DIR / f"{(d or date.today()).isoformat()}.json"


def load_today() -> dict:
    return _load(today_path(), {"date": date.today().isoformat(), "runs": [],
                                "scraped_ids": [], "applied": [], "skipped": [],
                                "errors": []})


def _send(msg: str):
    """Best-effort Telegram send — never raises into the pipeline."""
    try:
        if tg.configured():
            tg.send_message(msg)
    except Exception:
        pass


def announce_new_postings(jobs: list[dict]) -> int:
    """Telegram a heads-up about what the scrape turned up, and record all scraped
    ids for the digest. Returns the count of NEW (not-previously-announced) postings.

    The user wants to hear from the run EVERY day — including days with nothing new
    — so silence never looks like a missed or broken run:
      • new postings -> "🆕 N new NUworks postings found: ..."
      • nothing new  -> "📭 0 new job postings today ..."
    Either notice is sent at most once per day (postings_notice_sent in the daily
    file), so a failure-retry slot doesn't re-ping the same news."""
    jobs = jobs or []
    announced = _load(ANNOUNCED, {})
    today = date.today().isoformat()

    t = load_today()
    for j in jobs:
        jid = j.get("job_id")
        if jid and jid not in t["scraped_ids"]:
            t["scraped_ids"].append(jid)

    new = [j for j in jobs if j.get("job_id") and j["job_id"] not in announced]

    if not new:
        # Nothing fresh today. Tell the user anyway (once/day) so a quiet day is
        # confirmed rather than silent.
        first_notice = not t.get("postings_notice_sent")
        t["postings_notice_sent"] = True
        _save(today_path(), t)
        if first_notice:
            total = len(jobs)
            already = sum(1 for j in jobs if j.get("_apply_type") == "already_applied")
            if total == 0:
                _send("📭 <b>NUworks daily check</b>\n\n0 new job postings — the portal "
                      "returned nothing for your filters today. Nothing to apply to.")
            else:
                extra = f" ({already} already applied)" if already else ""
                _send("📭 <b>NUworks daily check</b>\n\n0 new job postings today. "
                      f"{total} prior listing(s) still showing{extra} — nothing new to apply to.")
        return 0

    t["postings_notice_sent"] = True
    _save(today_path(), t)
    lines = []
    for j in new:
        typ = j.get("_apply_type") or "?"
        label = _TYPE_LABEL.get(typ, typ)
        title = html.escape(str(j.get("title", ""))[:70])
        company = html.escape(str(j.get("company", "")))
        lines.append(f"• {title} — {company} [{html.escape(str(label))}]")
        announced[j["job_id"]] = {"date": today, "title": j.get("title", ""),
                                  "company": j.get("company", ""), "type": typ}
    _save(ANNOUNCED, announced)
    head = f"🆕 {len(new)} new NUworks posting{'s' if len(new) != 1 else ''} found:\n\n"
    _send(head + "\n".join(lines))
    return len(new)


def record_run(jobs: list[dict], results: list[dict], marked: bool):
    """Accumulate this run's outcomes into today's stats file (deduped by job)."""
    t = load_today()
    t["runs"].append(datetime.now().strftime("%H:%M"))

    def _add(bucket: list, item: dict, key="job"):
        if not any(x.get(key) == item.get(key) for x in bucket):
            bucket.append(item)

    for r in results:
        res = r.get("result")
        job = r.get("job", "")
        if res == "applied":
            _add(t["applied"], {"job": job})
        elif r.get("system_failure"):
            _add(t["errors"], {"job": job, "reason": res})
        elif res in ("reject", "timeout", "cover_letter_rejected",
                     "cover_letter_timeout", "no_telegram"):
            _add(t["skipped"], {"job": job, "reason": res})
    _save(today_path(), t)
