#!/usr/bin/env python3
"""Supervisor: post-run classification, loop-breaking, and a consolidated report.

Phase 1 (deterministic floor, NO brain — what this NUworks port ships): classify every
non-success outcome, break the doomed retry loop by evicting jobs that can't be satisfied
into data/wontfix.json (which eligible()/merge_pending() then filter), and send ONE
consolidated report (applied / retrying / needs-you) instead of the old scattered
per-result Telegram pings.

Brain (optional, wired OFF here): for the costly "needs you" set, a hard-timed-out
claude_runner call (diagnose) reads the evidence (status, detail, a run-log excerpt) and
refines the class + explains the root cause. The brain may only RESCUE a job from eviction
(confident transient/verify_gap) or enrich the escalation — it never creates a new eviction
and it never edits code. With use_brain=False (the default + how pipeline.py calls it) the
deterministic floor stands alone; enabling it later is a one-line change at the hook.
Perception and the autonomous auto-fix engine are intentionally NOT ported here.

Two entry points:
  • run_supervisor(results, ...) — called IN-PROCESS by pipeline.py after a run; writes a
    per-day sentinel (logs/supervisor_done_<date>.json).
  • reconstruct_and_run() — the launchd BACKSTOP (`supervisor.py --backstop`): if the
    in-process run didn't leave a sentinel (e.g. the pipeline crashed), it prunes the
    retry queue from disk state and reports anyway — a dead-man's switch for the
    supervisor itself.

Decoupled from pipeline.py (no import cycle): reads/writes the shared JSON state files
directly. All I/O is best-effort; run_supervisor never raises into the pipeline.
"""
import json
import html
import argparse
from datetime import datetime
from pathlib import Path

import claude_runner
import telegram_bot as tg

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOGS = HERE / "logs"
PENDING_FILE = DATA / "pending_retry.json"
WONTFIX_FILE = DATA / "wontfix.json"
PROPOSED_FIXES_FILE = DATA / "proposed_fixes.json"

AGE_OUT_DAYS = 7


def _cfg():
    try:
        c = json.loads((HERE / "config.json").read_text())
    except Exception:
        c = {}
    return (c.get("apply", {}).get("max_retry_attempts", 4),
            c.get("supervisor", {}).get("max_diagnoses_per_run", 8))


MAX_RETRY_ATTEMPTS, MAX_DIAGNOSES_PER_RUN = _cfg()

# NUworks apply taxonomy (see auto_apply.py + pipeline.py process()).
SUCCESS = {"applied", "already_applied", "staged"}
BENIGN = {"reject", "cover_letter_rejected", "no_inportal_apply", "external", "no_telegram"}
DEFERRALS = {"timeout", "cover_letter_timeout", "cap_reached"}   # pure deferrals; never count toward the cap
AUTO_RETRY_CLASSES = {"transient", "verify_gap"}                 # reversible: stay queued, retry next run
_VALID_CLASSES = {"transient", "structural", "data_gap", "code_break", "auth_challenge", "verify_gap"}


# ── small IO/util ───────────────────────────────────────────────────────────
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


def _age_days(iso, now):
    if not iso:
        return 0
    try:
        return (now - datetime.fromisoformat(iso)).days
    except Exception:
        return 0


def _num(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _extract_json(text: str):
    """Tolerant: bare JSON, ```json fenced, or JSON embedded in prose -> dict or None."""
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None


# ── deterministic classification (the floor; unit-tested without the brain) ───
def pre_classify(status: str, attempts: int = 0) -> str:
    """Map a NUworks apply status -> a failure class. SUCCESS/BENIGN are handled by the
    caller before this is reached; this only sees outcomes that need attention."""
    if status == "submitted_unverified":
        return "verify_gap" if attempts < 2 else "code_break"
    if status == "blocked_missing_documents":
        return "data_gap"
    if status in ("error", "crashed", "tailor_failed", "cover_letter_failed"):
        return "transient" if attempts < 1 else "code_break"
    if status in DEFERRALS:
        return "transient"
    return "code_break"


def route(status: str, cls: str, attempts: int) -> str:
    if status in SUCCESS:
        return "success"
    if status in BENIGN:
        return "info"
    if cls == "auth_challenge":
        return "session"
    if status in DEFERRALS:
        return "keep_retry"
    if cls in AUTO_RETRY_CLASSES and attempts < MAX_RETRY_ATTEMPTS:
        return "keep_retry"
    return "evict"


def _reason(status: str, cls: str, detail: str = "") -> str:
    detail = (detail or "").strip()
    if cls == "verify_gap":
        return "submit not confirmed; re-queued (the double-apply guard no-ops it next run if it actually went through)"
    if cls == "transient":
        if status == "cap_reached":
            return "daily cap reached; re-queued for the next run"
        if status in ("timeout", "cover_letter_timeout"):
            return "no approval tap in time; re-queued for the next run"
        return "transient failure; will retry"
    if cls == "data_gap":
        tail = f" ({detail[:90]})" if detail else ""
        return f"needs a document/answer I can't auto-provide — finish it manually{tail}"
    if cls == "structural":
        return "this posting can't be auto-applied (unmet requirement / not in-portal) — removed from auto-retry"
    if cls == "code_break":
        if status == "submitted_unverified":
            return "submitted but never confirmed across two runs — needs a look (verify/selector may be off)"
        return f"the automation looks off here — needs a look ({status})"
    if cls == "auth_challenge":
        return "NUworks session / login needs re-auth"
    return f"needs attention ({status})"


# ── brain diagnosis (optional; hard-timed-out, read-only, degrades to None) ────
DIAGNOSE_SYSTEM = (
    "You are a release engineer diagnosing why ONE automated NUworks job application "
    "did not succeed. You only READ the provided evidence and reply with strict JSON — "
    "you never edit code or take any action. Be concrete and concise."
)


def _parse_diag(text: str):
    obj = _extract_json(text)
    if isinstance(obj, dict) and obj.get("failure_class") in _VALID_CLASSES:
        return obj
    return None


def diagnose(incident: dict, add_dir=None):
    """Brain call: refine the class + explain the root cause for one failed application.
    Returns a diagnosis dict or None (caller falls back to the deterministic floor).
    Read-only; hard-timed-out via claude_runner (one attempt — diagnosis is best-effort).
    Dormant while use_brain=False; kept so enabling the brain is a one-line change."""
    prompt = (
        "Diagnose this failed NUworks job application. Classify the ROOT CAUSE as one of: "
        "transient | structural | data_gap | code_break | auth_challenge | verify_gap. "
        "Give a one-line root_cause, your confidence (0-1), a recommended_action "
        "(re_queue | re_tailor | stop_and_escalate | pause_session | propose_code_fix | "
        "verify_only), the exact missing_item (if data_gap, else null), a concrete "
        "proposed_fix (ONLY if code_break, else null), and one plain-English user_message.\n\n"
        f"OBSERVED_STATUS: {incident.get('result')}\n"
        f"DETAIL: {incident.get('detail','')}\n"
        f"JOB: {incident.get('job','')} (id {incident.get('job_id','')})\n"
        f"ATTEMPTS: {incident.get('_attempts', 0)} (last_class {incident.get('_last_class')})\n"
        f"PIPELINE_LOG_EXCERPT:\n{(incident.get('log_excerpt') or '')[:1500]}\n\n"
        'Output ONLY JSON: {"failure_class": str, "root_cause": str, "confidence": number, '
        '"recommended_action": str, "missing_item": str|null, "proposed_fix": str|null, '
        '"user_message": str}'
    )

    def ok(run):
        return _parse_diag(run.get("stdout") or "")

    try:
        res = claude_runner.run_with_retries(
            prompt, HERE, ok, timeout=120, retries=1, max_turns=6,
            append_system_prompt=DIAGNOSE_SYSTEM, add_dir=add_dir, model="claude-opus-4-8")
    except Exception:
        return None
    return res["value"] if res.get("ok") else None


def _log_excerpt(title: str, max_lines: int = 20) -> str:
    """Lines from the most recent run log mentioning this job's title (the log keys by it)."""
    try:
        logs = sorted(LOGS.glob("run_*.log"))
        if not logs:
            return ""
        lines = logs[-1].read_text().splitlines()
    except Exception:
        return ""
    key = (title or "")[:25]
    hits = [ln for ln in lines if key and key in ln]
    return "\n".join(hits[-max_lines:])


def _record_proposed_fix(jid, title, diag, now):
    """Record a brain-proposed code fix to data/proposed_fixes.json for a later human/dev
    session. (The autonomous auto-fix engine is out of scope for this NUworks port — this
    just leaves a durable trail; the report surfaces the proposed fix inline.)"""
    items = _load(PROPOSED_FIXES_FILE, [])
    if not isinstance(items, list):
        items = []
    items.append({"job_id": jid, "title": title, "at": now.isoformat(timespec="seconds"),
                  "root_cause": diag.get("root_cause"), "proposed_fix": diag.get("proposed_fix"),
                  "confidence": diag.get("confidence")})
    _save(PROPOSED_FIXES_FILE, items)


# ── sentinel (dedup the in-process run vs the launchd backstop) ───────────────
def _sentinel_path(now):
    return LOGS / f"supervisor_done_{now:%Y-%m-%d}.json"


def _write_sentinel(now, summary):
    _save(_sentinel_path(now), {"at": now.isoformat(timespec="seconds"), **summary})


def _sentinel_fresh(now):
    return _sentinel_path(now).exists()


# ── report ─────────────────────────────────────────────────────────────────
def build_report(applied, auto_fixed, needs_you, session_action, now) -> str:
    """Pure: render the consolidated report. needs_you items are (title, reason[, fix])."""
    esc = html.escape
    out = [f"🤖 <b>NUworks run report — {now:%Y-%m-%d %H:%M}</b>", ""]
    out.append(f"✅ Applied: {len(applied)}")
    for t in applied[:15]:
        out.append(f"   • {esc(str(t))}")
    if auto_fixed:
        out.append(f"\n🔁 Retrying next run: {len(auto_fixed)}")
        for t, why in auto_fixed[:15]:
            out.append(f"   • {esc(str(t))} — {esc(why)}")
    if needs_you:
        out.append(f"\n🙋 Needs you: {len(needs_you)}")
        for item in needs_you[:15]:
            t, why = item[0], item[1]
            fix = item[2] if len(item) > 2 else None
            out.append(f"   • {esc(str(t))} — {esc(why)}")
            if fix:
                out.append(f"       ↳ proposed fix: {esc(str(fix)[:300])}")
    if session_action:
        out.append(f"\n🔑 Action: NUworks login/session needs attention — "
                   f"{len(session_action)} job(s) blocked.")
    return "\n".join(out)


def _tally(results) -> str:
    """Compact one-line outcome count for a run, e.g. '3 processed · 2 applied — 1 reject'.
    Lets every non-empty run send at least a brief acknowledgment, so an approve tap that
    ends in a benign outcome (e.g. no_inportal_apply) isn't met with silence."""
    from collections import Counter
    c = Counter((r.get("result") or "?") for r in results)
    applied = c.get("applied", 0)
    others = ", ".join(f"{v} {k}" for k, v in sorted(c.items()) if k != "applied")
    return f"{len(results)} processed · {applied} applied" + (f" — {others}" if others else "")


def _commit_evictions(pending: list, evict: dict, now) -> list:
    """Remove evicted job_ids from pending_retry.json and append them to wontfix.json
    (which eligible()/merge_pending() filter, so an evicted job stops being re-offered)."""
    kept = [p for p in pending if str(p.get("job_id")) not in evict]
    _save(PENDING_FILE, kept)
    wf = _load(WONTFIX_FILE, {})
    if not isinstance(wf, dict):
        wf = {}
    pend_by_id = {str(p.get("job_id")): p for p in pending}
    stamp = now.isoformat(timespec="seconds")
    for jid, info in evict.items():
        if jid in wf:
            continue
        src = pend_by_id.get(jid, {})
        wf[jid] = {"title": info.get("title") or src.get("title", ""),
                   "company": src.get("company", ""),
                   "reason": info.get("reason", ""), "evicted_at": stamp}
    _save(WONTFIX_FILE, wf)
    return kept


def _send(msg):
    if tg.configured():
        tg.send_message(msg)
    else:
        print(msg)


# ── in-process entry point ────────────────────────────────────────────────────
def run_supervisor(results, shortlist=None, marked=True, now=None, use_brain=False):
    """Classify results, optionally diagnose the escalations with the brain, prune the
    retry queue, write the day's sentinel, and send ONE consolidated report. Best-effort;
    never raises into the pipeline. use_brain defaults OFF for this NUworks port."""
    try:
        _run(results or [], now or datetime.now(), use_brain=use_brain)
    except Exception as e:
        try:
            print(f"[supervisor] error: {type(e).__name__}: {e}", flush=True)
            if tg.configured():
                tg.send_message("⚠️ Supervisor hit an error (the run still completed): "
                                f"<code>{html.escape(str(e)[:200])}</code>")
        except Exception:
            pass


def _run(results, now, use_brain=False):
    pending = _load(PENDING_FILE, [])
    pend_by_id = {str(p.get("job_id")): p for p in pending}
    result_ids = {str(r.get("job_id") or "") for r in results}

    applied, auto_fixed, needs_you, session_action = [], [], [], []
    evict = {}
    diag_budget = MAX_DIAGNOSES_PER_RUN

    for r in results:
        status = r.get("result")
        jid = str(r.get("job_id") or "")
        title = r.get("job") or jid or "—"
        if status in SUCCESS:
            if status == "applied":
                applied.append(title)
            continue
        if status in BENIGN:
            continue
        attempts = int((pend_by_id.get(jid) or {}).get("_attempts", 0) or 0)
        cls = pre_classify(status, attempts)
        action = route(status, cls, attempts)
        reason = _reason(status, cls, r.get("detail") or "")
        fix = None

        # Brain diagnosis — only on the costly "needs you" set, capped. The brain can
        # RESCUE a job from eviction (confident transient/verify_gap) or enrich/confirm
        # it; it never creates a new eviction. Dormant while use_brain=False.
        if use_brain and action == "evict" and diag_budget > 0:
            incident = dict(r)
            incident["_attempts"] = attempts
            incident["_last_class"] = (pend_by_id.get(jid) or {}).get("_last_class")
            incident["log_excerpt"] = _log_excerpt(title)
            diag = diagnose(incident, add_dir=str(LOGS))
            diag_budget -= 1
            if diag:
                refined = diag.get("failure_class")
                conf = _num(diag.get("confidence"))
                if refined in AUTO_RETRY_CLASSES and conf >= 0.6:
                    action, cls = "keep_retry", refined        # rescued from eviction
                reason = diag.get("user_message") or reason
                if diag.get("failure_class") == "code_break" and diag.get("proposed_fix"):
                    fix = diag.get("proposed_fix")
                    _record_proposed_fix(jid, title, diag, now)

        if action == "session":
            session_action.append(title)
        elif action == "keep_retry":
            auto_fixed.append((title, reason))
        elif action == "evict":
            evict[jid] = {"title": title, "reason": reason}
            needs_you.append((title, reason, fix))

    # age-out: stragglers still queued but not seen this run
    for p in pending:
        jid = str(p.get("job_id") or "")
        if jid in result_ids or jid in evict:
            continue
        if _age_days(p.get("_first_queued") or p.get("_queued_at"), now) > AGE_OUT_DAYS:
            title = p.get("title") or jid
            reason = f"open & unresolved for over {AGE_OUT_DAYS} days — removed from auto-retry"
            evict[jid] = {"title": title, "reason": reason}
            needs_you.append((title, reason, None))

    if evict:
        _commit_evictions(pending, evict, now)
    _write_sentinel(now, {"applied": len(applied), "auto_fixed": len(auto_fixed),
                          "needs_you": len(needs_you)})
    # Always acknowledge a non-empty run. Rich outcomes (applied / retrying / needs-you /
    # session) get the full report + a tally footer; a run whose only outcomes were benign
    # (e.g. no_inportal_apply, reject) still gets a one-line summary instead of silence; a
    # truly empty run (no results, nothing evicted) stays quiet.
    if applied or auto_fixed or needs_you or session_action:
        msg = build_report(applied, auto_fixed, needs_you, session_action, now)
        if results:
            msg += f"\n\n📊 {_tally(results)}"
        _send(msg)
    elif results:
        _send(f"🤖 <b>NUworks run — {now:%Y-%m-%d %H:%M}</b>\n📊 {_tally(results)}")


# ── launchd backstop (dead-man's switch for the supervisor) ───────────────────
def _daily_applied(now):
    d = _load(DATA / "daily" / f"{now:%Y-%m-%d}.json", {})
    return [a.get("job", "") for a in d.get("applied", [])] if isinstance(d, dict) else []


def reconstruct_and_run(now=None):
    """Backstop: if the in-process supervisor didn't run today (no sentinel — e.g. the
    pipeline crashed), prune the retry queue from disk state and report anyway. Uses the
    deterministic floor only (the brain is optional and off by default here)."""
    now = now or datetime.now()
    if _sentinel_fresh(now):
        print("[supervisor] in-process run already reported today; backstop is a no-op.")
        return
    pending = _load(PENDING_FILE, [])
    needs_you, evict = [], {}
    for p in pending:
        jid = str(p.get("job_id") or "")
        cls = p.get("_last_class") or "transient"
        attempts = int(p.get("_attempts", 0) or 0)
        aged = _age_days(p.get("_first_queued") or p.get("_queued_at"), now) > AGE_OUT_DAYS
        if cls in ("structural", "data_gap") or attempts >= MAX_RETRY_ATTEMPTS or aged:
            title = p.get("title") or jid
            reason = (_reason("", cls, p.get("_last_detail", "")) if cls in ("structural", "data_gap")
                      else f"unresolved after {attempts} attempt(s) — removed from auto-retry")
            evict[jid] = {"title": title, "reason": reason}
            needs_you.append((title, reason, None))
    if evict:
        _commit_evictions(pending, evict, now)
    applied = _daily_applied(now)
    _write_sentinel(now, {"applied": len(applied), "auto_fixed": 0,
                          "needs_you": len(needs_you), "backstop": True})
    if applied or needs_you:
        _send(build_report(applied, [], needs_you, [], now))


def main():
    ap = argparse.ArgumentParser(description="NUworks run supervisor")
    ap.add_argument("--backstop", action="store_true",
                    help="disk-based dead-man's-switch report (launchd); no-op if the "
                         "in-process supervisor already ran today")
    args = ap.parse_args()
    if args.backstop:
        reconstruct_and_run()
    else:
        print("supervisor.py runs in-process from pipeline.py after each run. "
              "Use --backstop for the dead-man's-switch report.")


if __name__ == "__main__":
    main()
