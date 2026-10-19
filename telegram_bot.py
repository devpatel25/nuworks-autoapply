#!/usr/bin/env python3
"""
Telegram approval channel.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from ../.env (saved once, like the
NUworks creds). Sends the tailored resume PDF with inline Approve / Reject
buttons, then polls getUpdates until the user taps one.

Setup (one time):
  1. Telegram -> @BotFather -> /newbot -> copy the token
  2. Telegram -> @userinfobot -> Start -> copy your numeric Chat ID
  3. Open your new bot, tap Start
  4. Save both into ../.env as TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
"""
import io
import os
import re
import json
import time
import html
import queue
import threading
import fcntl
import contextlib
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"


def configured() -> bool:
    return bool(TOKEN and CHAT_ID)


def _require():
    if not configured():
        raise RuntimeError(
            "Telegram not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env"
        )


def _post_with_retry(fn, attempts: int = 4):
    """Retry a Telegram POST callable on transient network failures and honor
    429 rate-limiting. fn() performs the request and returns the parsed JSON.

    Telegram's API occasionally drops the connection mid-request
    (ConnectionResetError / ConnectionError). A single un-retried blip during
    the approval-card send crashed 4 jobs in one run (2026-09-10, all four hit
    "Connection reset by peer" on send_resume_for_approval and were left
    unmarked to retry the whole day). Wellfound got this same retry treatment
    after an identical incident (2026-06-25); NUworks never did — this ports it.
    """
    last_err = None
    for i in range(attempts):
        try:
            j = fn()
            if j.get("ok") is False and j.get("error_code") == 429:
                retry_after = (j.get("parameters") or {}).get("retry_after", 2)
                last_err = RuntimeError(f"429 rate-limited: {j}")
                time.sleep(min(retry_after, 30))
                continue
            return j
        except requests.RequestException as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(2 ** i)  # 1s, 2s, 4s
    raise RuntimeError(f"Telegram POST failed after {attempts} attempts: {last_err}")


def send_message(text: str, parse_mode: str = "HTML") -> dict:
    _require()
    return _post_with_retry(lambda: requests.post(
        f"{API}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
        timeout=30).json())


def _fmt_pay(salary: str) -> str:
    """Tidy the scraped salary string (e.g. '500–500 per week' -> '$500 per week')."""
    s = (salary or "").strip()
    if not s:
        return "Not listed"
    m = re.match(r'^\$?([\d,.]+)\s*[–-]\s*\$?([\d,.]+)\s*(.*)$', s)
    if m:
        lo, hi, rest = m.group(1), m.group(2), m.group(3).strip()
        return (f"${lo} {rest}".strip() if lo == hi else f"${lo}–${hi} {rest}".strip())
    return s


def _ext_links(job: dict) -> list:
    return [u.strip() for u in (job.get("apply_urls", "") or "").split(";") if u.strip()]


CAPTION_MAX = 1024  # Telegram sendDocument caption limit


def send_resume_for_approval(pdf_path: str, job: dict, summary: str = "",
                             nonce: str = "", scan: dict | None = None,
                             version: int = 1, round_no: int = 0, max_rounds: int = 3,
                             prev_scan: str = "") -> int:
    """Send the tailored resume PDF with Approve/Reject (+ optional Scan) buttons.
    Returns the sent message_id. callback_data encodes decision:job_id:nonce — the
    nonce lets wait_for_decision reject a stale tap from an earlier send so it can't
    auto-approve a freshly tailored resume the user never reviewed.

    scan=None → no scan UI (legacy). scan={"available": bool, "left": int|None}:
    available=False (Big Interview login invalid) hides the button and says why."""
    _require()
    jid = job["job_id"]
    head = "📄 <b>Tailored resume ready</b>"
    if version > 1:
        head += f"  (v{version} · retailor {round_no}/{max_rounds})"
    caption = (
        f"{head}\n\n"
        f"<b>{html.escape(job.get('title','')[:80])}</b>\n"
        f"{html.escape(job.get('company','')[:80])} — {html.escape(job.get('location','')[:60])}\n"
        f"💵 Pay: {html.escape(_fmt_pay(job.get('salary',''))[:60])}\n"
        f"🗓 Deadline: {html.escape(str(job.get('deadline') or '—')[:40])}\n"
    )
    # External apply link(s) — so you can apply yourself (e.g. from your phone).
    ext = _ext_links(job)
    ext_line = ("🌐 Apply externally too (do this yourself): "
                + html.escape("  ".join(ext)) + "\n") if ext else ""
    summary, prev_scan = (summary or "")[:400], (prev_scan or "")[:120]

    def tail_for(summ: str) -> str:
        t = f"\n{html.escape(summ)}" if summ else ""
        if prev_scan:
            t += f"\nPrev scan: {html.escape(prev_scan)}"
        if scan is not None and not scan.get("available"):
            t += "\nScan unavailable: log in via scripts/login.py"
        return t + "\n\nApprove to auto-apply on NUworks, or Reject to skip."

    if len(caption + ext_line + tail_for(summary)) <= CAPTION_MAX:
        caption += ext_line  # links also appear on the posting card; dropped if too long
    # Shrink the PLAIN summary until the escaped caption fits — never slice HTML.
    while summary and len(caption + tail_for(summary)) > CAPTION_MAX:
        summary = summary[:-20]
    caption += tail_for(summary)

    row = [
        {"text": "✅ Approve & Apply", "callback_data": f"approve:{jid}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"reject:{jid}:{nonce}"},
    ]
    if scan is not None and scan.get("available"):
        left = scan.get("left")
        label = f"🔍 Scan ({left}/5 left)" if left is not None else "🔍 Scan"
        row.append({"text": label, "callback_data": f"scan:{jid}:{nonce}"})
    keyboard = {"inline_keyboard": [row]}

    _expect(jid, nonce)
    pdf_bytes = Path(pdf_path).read_bytes()  # read once; retries need a fresh stream each attempt
    pdf_name = Path(pdf_path).name
    res = _post_with_retry(lambda: requests.post(
        f"{API}/sendDocument",
        data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
              "reply_markup": json.dumps(keyboard)},
        files={"document": (pdf_name, io.BytesIO(pdf_bytes), "application/pdf")},
        timeout=60).json())
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {res}")
    return res["result"]["message_id"]


PORTAL_BASE = "https://northeastern-csm.symplicity.com"


def job_posting_url(job_id: str) -> str:
    """The posting's own detail page. Verified against the live portal 2026-10-11: with
    a NUworks session it opens exactly that job (the search?currentJobId= link that
    auto_apply uses lands on the full job list instead). Without a session NUworks
    sends you to its sign-in page first."""
    return f"{PORTAL_BASE}/students/app/jobs/detail/{job_id}"


def posting_card_text(job: dict) -> str:
    """Plan §4 Step 4 card: title, company, pay, posted, apply-by, external, link."""
    ext = _ext_links(job)
    ext_txt = ("Yes — " + "  ".join(html.escape(u) for u in ext)) if ext else "No"
    url = job_posting_url(job["job_id"])
    return (
        "🆕 New posting\n\n"
        f"<b>{html.escape(job.get('title','')[:120])}</b>\n"
        f"{html.escape(job.get('company',''))}\n"
        f"💵 Pay: {html.escape(_fmt_pay(job.get('salary','')))}\n"
        f"📅 Posted: {html.escape(str(job.get('posted') or '—'))}\n"
        f"🗓 Apply by: {html.escape(str(job.get('deadline') or '—'))}\n"
        f"🌐 External apply: {ext_txt}\n"
        f'🔗 <a href="{html.escape(url, quote=True)}">Job posting</a>'
    )


def send_job_for_approval(job: dict, nonce: str = "") -> int:
    """Send the job POSTING ONLY (no resume yet) with Approve/Reject buttons — gates
    whether any LLM sees the JD at all, so a reject here costs zero tokens. Same
    callback_data scheme as send_resume_for_approval."""
    _require()
    jid = job["job_id"]
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{jid}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"reject:{jid}:{nonce}"},
    ]]}
    return _send_text(posting_card_text(job), keyboard, jid, nonce, "job approval")


def send_low_match_card(job: dict, match: dict, nonce: str = "") -> int:
    """Plan §4 Step 7: second chance to bail before the (expensive) tailor."""
    _require()
    jid = job["job_id"]
    missing = match.get("missing_must") or []
    miss = ", ".join(missing[:8]) + (", …" if len(missing) > 8 else "")
    text = (
        f"⚠️ Low match: {round((match.get('score') or 0) * 100)}% "
        f"({match.get('must_hit', 0)}/{match.get('must_total', 0)} must-have)\n\n"
        f"<b>{html.escape(job.get('title','')[:100])}</b> — {html.escape(job.get('company',''))}\n"
        f"Missing: {html.escape(miss or '—')}\n\n"
        "Tailor anyway?"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Tailor anyway", "callback_data": f"lowok:{jid}:{nonce}"},
        {"text": "❌ Skip", "callback_data": f"lowno:{jid}:{nonce}"},
    ]]}
    return _send_text(text, keyboard, jid, nonce, "low match")


def _send_text(text: str, keyboard: dict, jid: str, nonce: str, what: str) -> int:
    _expect(jid, nonce)
    res = _post_with_retry(lambda: requests.post(
        f"{API}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True, "reply_markup": json.dumps(keyboard)},
        timeout=30).json())
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendMessage ({what}) failed: {res}")
    return res["result"]["message_id"]


def answer_callback(cq_id: str, text: str = ""):
    try:
        requests.post(f"{API}/answerCallbackQuery",
                      data={"callback_query_id": cq_id, "text": text}, timeout=20)
    except requests.RequestException:
        pass


def edit_reply_markup(message_id: int, keyboard: dict):
    return _post_with_retry(lambda: requests.post(
        f"{API}/editMessageReplyMarkup",
        data={"chat_id": CHAT_ID, "message_id": message_id,
              "reply_markup": json.dumps(keyboard)}, timeout=30).json())


# ── Score card (plan §4A.5) ──────────────────────────────────────────────────

TEXT_BUDGET = 3600  # Telegram sendMessage limit is 4096; leave room for footer lines
MEDAL = {"gold": "🥇 Gold", "silver": "🥈 Silver", "bronze": "🥉 Bronze"}
CORRECTION_STATUSES = ("needs_work", "warning")


def medal_label(m) -> str:
    return MEDAL.get((m or "").lower(), "— n/a")


def score_card_items(fb: dict, categories=("credibility", "ats_fit")) -> list:
    """Toggleable items: every UNMATCHED ATS keyword, then corrections (criteria with
    needs_work / warning + action items) from the allowed categories ONLY (D18)."""
    cats = fb.get("categories") or {}
    items, seen = [], set()

    def add(kind, text):
        text = " ".join(str(text or "").split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            items.append({"kind": kind, "text": text})

    for k in (cats.get("ats_fit") or {}).get("keywords_unmatched") or []:
        add("keyword", k)
    for c in categories:
        for cr in (cats.get(c) or {}).get("criteria") or []:
            if cr.get("status") in CORRECTION_STATUSES:
                add("correction", cr.get("suggestion") or f"{cr.get('name')}: {cr.get('detail')}")
    for a in fb.get("action_items") or []:
        if a.get("category") in categories:
            add("correction", a.get("item"))
    return items


class ScoreCard:
    """In-memory state of one score card: items, ticked set, message id. Toggle taps
    are handled by the dispatcher thread directly (message edited in place) without
    waking the job worker until Retailor / Back."""

    def __init__(self, job: dict, nonce: str, fb: dict, *, version: int = 1,
                 scans_left=None, round_no: int = 0, max_rounds: int = 3,
                 max_toggles: int = 20, categories=("credibility", "ats_fit")):
        self.job, self.jid, self.nonce = job, job["job_id"], nonce
        self.fb, self.version, self.scans_left = fb, version, scans_left
        self.round_no, self.max_rounds = round_no, max_rounds
        self.partial = bool(fb.get("partial"))
        self.items = [] if self.partial else score_card_items(fb, tuple(categories))
        self.selectable = self.items[:max_toggles]
        self.overflow = self.items[max_toggles:]
        self.selected: set = set()
        self.message_id = None
        self._lock = threading.Lock()

    @property
    def can_retailor(self) -> bool:
        return not self.partial and bool(self.selectable) and self.round_no < self.max_rounds

    def picked(self) -> tuple:
        sel = [self.selectable[i] for i in sorted(self.selected)]
        return ([i["text"] for i in sel if i["kind"] == "keyword"],
                [i["text"] for i in sel if i["kind"] == "correction"])

    def text(self) -> str:
        cats = self.fb.get("categories") or {}
        ats = cats.get("ats_fit") or {}
        km, ku = ats.get("keywords_matched") or [], ats.get("keywords_unmatched") or []
        lines = [f"🔍 Big Interview scan — <b>{html.escape(self.job.get('title','')[:60])}</b> "
                 f"@ {html.escape(self.job.get('company',''))}  (v{self.version})",
                 f"Overall: {medal_label(self.fb.get('medal'))}", ""]
        for key, name in (("readability", "Readability"), ("credibility", "Credibility"),
                          ("ats_fit", "ATS Fit"), ("format", "Format")):
            lines.append(f"{name:<12} {medal_label((cats.get(key) or {}).get('badge'))}")
        lines += ["", f"Keywords matched: {len(km)}/{len(km) + len(ku)}"]
        if self.partial:
            lines += ["", "Feedback incomplete — retailor unavailable for this scan"]
        elif self.selectable and self.round_no < self.max_rounds:
            lines += ["", "Pick what to fix, then Retailor:"]
            # Full text here — buttons truncate. Capped per item only if 20 long ones
            # would blow the message limit.
            cap = max(48, (TEXT_BUDGET - len("\n".join(lines)) - 600) // len(self.selectable))
            for n, it in enumerate(self.selectable, 1):
                tag = "" if it["kind"] == "keyword" else "✎ "
                t = it["text"] if len(it["text"]) <= cap else it["text"][:cap - 1] + "…"
                lines.append(f"{n}. {tag}{html.escape(t)}")
        if self.overflow:
            lines += ["", "(not selectable — too many):"]
            budget = TEXT_BUDGET - len("\n".join(lines))
            for k, i in enumerate(self.overflow):
                line = f"• {html.escape(i['text'][:100])}"
                if len(line) + 1 > budget - 40:      # keep room for the "and N more" line
                    lines.append(f"… and {len(self.overflow) - k} more")
                    break
                lines.append(line)
                budget -= len(line) + 1
        if self.round_no >= self.max_rounds:
            lines += ["", f"Retailor limit reached ({self.max_rounds}/{self.max_rounds})"]
        if self.scans_left is not None:
            lines += [f"Scans left: {self.scans_left}/5"]
        return "\n".join(lines)

    def keyboard(self) -> dict:
        rows = []
        if self.can_retailor:
            for i, it in enumerate(self.selectable):
                box = "☑" if i in self.selected else "☐"
                tag = "" if it["kind"] == "keyword" else "✎ "
                rows.append([{"text": f"{box} {i + 1}. {tag}{it['text'][:48]}",
                              "callback_data": f"tg:{self.jid}:{self.nonce}:{i}"}])
        last = []
        if self.can_retailor:
            last.append({"text": f"🔁 Retailor ({len(self.selected)} selected)",
                         "callback_data": f"retailor:{self.jid}:{self.nonce}"})
        last.append({"text": "⬅️ Back to resume", "callback_data": f"back:{self.jid}:{self.nonce}"})
        rows.append(last)
        return {"inline_keyboard": rows}

    def toggle(self, cq: dict, idx: int):
        with self._lock:
            if not self.can_retailor or not 0 <= idx < len(self.selectable):
                answer_callback(cq["id"], "Not selectable.")
                return
            self.selected ^= {idx}
            on = idx in self.selected
            kb = self.keyboard()
        answer_callback(cq["id"], "☑ selected" if on else "☐ removed")
        try:
            edit_reply_markup(self.message_id, kb)
        except Exception:
            pass

    def refuse(self, action: str):
        """Gate for wait_for_decision: a 0-selected Retailor tap is refused."""
        if action == "retailor":
            with self._lock:
                if not self.can_retailor:
                    return "Retailor unavailable for this card."
                if not self.selected:
                    return "Select at least one item"
        return None


def send_score_card(job: dict, fb: dict, nonce: str, **kw) -> ScoreCard:
    """Send the §4A.5 score card and register its toggle handler with the dispatcher.
    Returns the ScoreCard; wait on it with wait_for_decision(jid, nonce, refuse=card.refuse)."""
    _require()
    if not DISPATCHER.running:  # toggles + refusals only work through the dispatcher
        raise RuntimeError("send_score_card needs the CallbackDispatcher running")
    card = ScoreCard(job, nonce, fb, **kw)
    DISPATCHER.on_toggle(card.jid, nonce, card.toggle)
    card.message_id = _send_text(card.text(), card.keyboard(), card.jid, nonce, "score card")
    return card


def _commit_offset(offset):
    """Confirm processed updates to Telegram (one getUpdates with the advanced
    offset) so the decided tap isn't replayed by a later run/poller."""
    if offset is None:
        return
    try:
        requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=20)
    except requests.RequestException:
        pass


def send_cover_letter_for_approval(pdf_path: str, job: dict, nonce: str = "") -> int:
    """Send a generated cover-letter PDF with its OWN Approve/Reject buttons, so a
    required cover letter is never submitted to an employer sight-unseen (C6)."""
    _require()
    jid = job["job_id"]
    caption = (
        f"✍️ <b>Cover letter ready — review before it's sent</b>\n\n"
        f"<b>{html.escape(job.get('title','')[:80])}</b>\n"
        f"{html.escape(job.get('company',''))}\n\n"
        "This job REQUIRES a cover letter. Approve to attach + submit it with your "
        "resume, or Reject to skip this application (it won't be submitted without it)."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve letter", "callback_data": f"approve:{jid}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"reject:{jid}:{nonce}"},
    ]]}
    _expect(jid, nonce)
    pdf_bytes = Path(pdf_path).read_bytes()  # read once; retries need a fresh stream each attempt
    pdf_name = Path(pdf_path).name
    res = _post_with_retry(lambda: requests.post(
        f"{API}/sendDocument",
        data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
              "reply_markup": json.dumps(keyboard)},
        files={"document": (pdf_name, io.BytesIO(pdf_bytes), "application/pdf")},
        timeout=60).json())
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument (cover letter) failed: {res}")
    return res["result"]["message_id"]


# ── Callback dispatcher (plan §5) ────────────────────────────────────────────

ACK_TEXT = {"approve": "Approved ✅", "reject": "Rejected ❌", "lowok": "Tailoring… ✍️",
            "lowno": "Skipped ⏭️", "scan": "Scanning… (takes up to 10 min)",
            "retailor": "Retailoring… 🔁", "back": "Back to resume ⬅️"}
EXPIRED = "This card expired — see the latest one."


class CallbackDispatcher:
    """ONE thread owns getUpdates and routes each tap by callback_data
    action:job_id:nonce[:idx] into a per-(job_id, nonce) queue, so many cards can be
    live at once and each job worker blocks only on its own card. Score-card toggles
    (action 'tg') go to a registered handler instead (message edited in place). Taps
    for an unknown / finished (job_id, nonce) are acked as expired and dropped."""

    ORPHAN_GRACE_S = 10  # a tap may beat its waiter's registration; hold it this long

    def __init__(self):
        self._queues: dict = {}
        self._toggles: dict = {}
        self._orphans: dict = {}  # (job_id, nonce) -> [(action, cq, t)] not yet claimed
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.offset = None
        self.on_command = None  # pipeline sets this: handles /start + /stop mid-run

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def expect(self, job_id: str, nonce: str) -> "queue.Queue":
        key = (str(job_id), nonce)
        with self._lock:
            q = self._queues.setdefault(key, queue.Queue())
            for action, cq, _ in self._orphans.pop(key, []):
                q.put((action, cq))  # tap arrived before the waiter registered
            return q

    def sweep(self, now: float = None):
        """Ack orphaned taps older than the grace period as expired."""
        now = time.time() if now is None else now
        with self._lock:
            stale = []
            for key, taps in list(self._orphans.items()):
                keep = [t for t in taps if now - t[2] < self.ORPHAN_GRACE_S]
                stale += [t for t in taps if now - t[2] >= self.ORPHAN_GRACE_S]
                if keep:
                    self._orphans[key] = keep
                else:
                    self._orphans.pop(key)
        for action, cq, _ in stale:
            answer_callback(cq["id"], EXPIRED if action in ACK_TEXT else
                            "This card is no longer active.")

    def on_toggle(self, job_id: str, nonce: str, handler):
        with self._lock:
            self._toggles[(str(job_id), nonce)] = handler

    def release(self, job_id: str, nonce: str):
        with self._lock:
            self._queues.pop((str(job_id), nonce), None)
            self._toggles.pop((str(job_id), nonce), None)

    def route(self, cq: dict):
        parts = (cq.get("data") or "").split(":")
        if len(parts) < 2:
            return
        action, jid = parts[0], parts[1]
        n = parts[2] if len(parts) > 2 else ""
        key = (jid, n)
        with self._lock:
            q, toggle = self._queues.get(key), self._toggles.get(key)
        if action == "tg":
            if toggle is None:
                answer_callback(cq["id"], EXPIRED)
                return
            try:
                toggle(cq, int(parts[3]))
            except (IndexError, ValueError):
                answer_callback(cq["id"], "Not selectable.")
            return
        if q is None:
            # Unknown card: maybe its waiter hasn't registered yet (a card sent
            # without _expect, e.g. external_autofill's). Hold briefly; sweep()
            # acks it as expired if nobody claims it.
            with self._lock:
                self._orphans.setdefault(key, []).append((action, cq, time.time()))
            return
        q.put((action, cq))

    def poll_once(self, poll_timeout: int = 25):
        params = {"timeout": poll_timeout}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            r = requests.get(f"{API}/getUpdates", params=params,
                             timeout=poll_timeout + 15).json()
        except (requests.RequestException, ValueError):
            time.sleep(3)
            return
        for upd in r.get("result", []):
            self.offset = upd["update_id"] + 1
            cmd = parse_command(upd)
            if cmd and self.on_command:
                try:
                    self.on_command(cmd)
                except Exception:
                    pass
            cq = upd.get("callback_query")
            if cq:
                try:
                    self.route(cq)
                except Exception:
                    pass  # one bad tap must never kill the poller

    def start(self):
        if self.running:
            return
        _require()
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                self.poll_once(poll_timeout=5)  # short polls so orphans get swept
                self.sweep()

        self._thread = threading.Thread(target=loop, name="tg-dispatcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=45)
        self._thread = None
        self.sweep(now=float("inf"))  # clear any spinners still held
        _commit_offset(self.offset)  # don't replay processed taps next run


DISPATCHER = CallbackDispatcher()

COMMANDS = ("/start", "/stop", "/apply")
COMMAND_MAX_AGE_S = 120  # older commands are replays (e.g. after a crash) — ignore
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
APPLY_QUEUE = Path(__file__).resolve().parent / "data/apply_queue.txt"  # one job_id per line
# Touched by control_bot when an /apply (not /start or a scheduled slot) starts the run:
# only then does the pipeline go manual-only. Consumed (unlinked) by pipeline.main.
MANUAL_FLAG = Path(__file__).resolve().parent / "logs/manual_run.flag"
APPLY_USAGE = "Usage: /apply &lt;NUworks posting URL or job id&gt;"


@contextlib.contextmanager
def _queue_lock():
    """flock on a sidecar file: control_bot appends and the pipeline drains from
    different processes (and threads — each open() is its own lock holder)."""
    with open(APPLY_QUEUE.with_suffix(".lock"), "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def parse_command(upd: dict, now: float = None):
    """'/start', '/stop', '/apply <job_id>' (bare '/apply' if no id in the text) if
    this update is a fresh control command from Dev's chat, else None. Only CHAT_ID
    may drive the pipeline."""
    msg = upd.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
        return None
    now = time.time() if now is None else now
    if now - msg.get("date", 0) > COMMAND_MAX_AGE_S:
        return None
    text = (msg.get("text") or "").lower()
    words = text.split()
    cmd = words[0].split("@")[0] if words else ""
    if cmd not in COMMANDS:
        return None
    if cmd == "/apply":
        m = JOB_ID_RE.search(text)
        return f"/apply {m.group()}" if m else "/apply"
    return cmd


def queue_apply(job_id: str):
    """Hand a job_id to the next/current pipeline run (both bots share this file)."""
    with _queue_lock(), open(APPLY_QUEUE, "a") as f:
        f.write(job_id + "\n")


def drain_apply_queue() -> list[str]:
    """Pop every queued job_id (deduped, in order); the file is emptied."""
    with _queue_lock():
        if not APPLY_QUEUE.exists():
            return []
        ids = APPLY_QUEUE.read_text().split()
        APPLY_QUEUE.write_text("")
    return list(dict.fromkeys(ids))


def _expect(job_id: str, nonce: str):
    """Register a card's (job_id, nonce) BEFORE it's sent, so a fast tap can't arrive
    before its waiter and be dropped as expired."""
    if DISPATCHER.running:
        DISPATCHER.expect(job_id, nonce)


def wait_for_decision(job_id: str, nonce: str = "", timeout: int = 86400, poll: int = 3,
                      refuse=None) -> str:
    """Wait until the user taps a button on this job's card (and, if a nonce is given,
    THIS send). Returns the action ('approve', 'reject', 'lowok', 'scan', …) or
    'timeout'. Acks every tap so spinners clear; a stale nonce is acked as expired
    and ignored so it can't approve a resume the user never reviewed.

    With the dispatcher running this reads from its per-card queue; otherwise it
    polls getUpdates itself (legacy single-card path). refuse(action) -> str|None
    may veto a tap (acked with that text, wait continues) — e.g. 0-selected Retailor."""
    _require()
    if DISPATCHER.running:
        q = DISPATCHER.expect(job_id, nonce)
        deadline = time.time() + timeout
        try:
            while True:
                left = deadline - time.time()
                if left <= 0:
                    return "timeout"
                try:
                    action, cq = q.get(timeout=left)
                except queue.Empty:
                    return "timeout"
                why = refuse(action) if refuse else None
                if why:
                    answer_callback(cq["id"], why)
                    continue
                answer_callback(cq["id"], ACK_TEXT.get(action, "OK"))
                return action
        finally:
            DISPATCHER.release(job_id, nonce)
    deadline = time.time() + timeout
    offset = None
    while time.time() < deadline:
        params = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40).json()
        except requests.RequestException:
            time.sleep(poll)
            continue

        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            data = cq.get("data", "")
            parts = data.split(":")
            if len(parts) < 2:
                continue
            action, jid = parts[0], parts[1]
            n = parts[2] if len(parts) > 2 else ""
            if jid != job_id:
                # Tap on some other job's card (e.g. left over from an earlier
                # run) — ack so the spinner clears, but don't act on it.
                requests.post(f"{API}/answerCallbackQuery",
                              data={"callback_query_id": cq["id"],
                                    "text": "This card is no longer active."},
                              timeout=20)
                continue
            if nonce and n != nonce:
                # Stale tap for this job from an earlier send — acknowledge so the
                # spinner clears, but do NOT act on it.
                requests.post(f"{API}/answerCallbackQuery",
                              data={"callback_query_id": cq["id"],
                                    "text": "This card expired — see the latest one."},
                              timeout=20)
                continue
            # Ack the tap so the spinner clears
            requests.post(f"{API}/answerCallbackQuery",
                          data={"callback_query_id": cq["id"],
                                "text": "Approved ✅" if action == "approve" else "Rejected ❌"},
                          timeout=20)
            _commit_offset(offset)  # don't let this tap replay later
            return action
        time.sleep(poll)
    _commit_offset(offset)
    return "timeout"


def send_duo_decision(nonce: str = "") -> int:
    """When a DUO push wasn't approved in its window, ask the user (two inline
    buttons) whether to resend the push and try again now, or skip the run for
    today. Returns the sent message_id. callback_data = action:nonce — the nonce
    lets wait_for_duo_decision ignore a stale tap left over from an earlier prompt."""
    _require()
    text = (
        "🔐 <b>DUO wasn't approved in time</b>\n\n"
        "I couldn't log in to NUworks — the DUO push wasn't approved within the "
        "window. What should I do?\n\n"
        "🔁 <b>Rerun</b> — send a fresh DUO push and try again right now.\n"
        "⏭️ <b>Skip today</b> — stop for today (no more DUO prompts until tomorrow)."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "🔁 Rerun (resend DUO)", "callback_data": f"duo_rerun:{nonce}"},
        {"text": "⏭️ Skip today", "callback_data": f"duo_skip:{nonce}"},
    ]]}
    res = _post_with_retry(lambda: requests.post(
        f"{API}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "reply_markup": json.dumps(keyboard)},
        timeout=30).json())
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendMessage (duo decision) failed: {res}")
    return res["result"]["message_id"]


def wait_for_duo_decision(nonce: str = "", timeout: int = 3600, poll: int = 3) -> str:
    """Poll getUpdates until the user taps Rerun or Skip on a DUO decision prompt
    (matching this nonce). Returns 'rerun', 'skip', or 'timeout'. Acks every tap so
    the spinner clears, and commits the offset so the tap isn't replayed by a later
    run/poller. A tap with a stale nonce is acked + ignored."""
    _require()
    if DISPATCHER.running:  # it owns getUpdates for the whole run; duo taps key as (nonce, "")
        q = DISPATCHER.expect(nonce, "")
        try:
            while True:
                try:
                    action, cq = q.get(timeout=timeout)
                except queue.Empty:
                    return "timeout"
                if action in ("duo_rerun", "duo_skip"):
                    choice = "rerun" if action == "duo_rerun" else "skip"
                    answer_callback(cq["id"], "Rerunning… 🔁" if choice == "rerun" else "Skipped ⏭️")
                    return choice
                answer_callback(cq["id"], EXPIRED)
        finally:
            DISPATCHER.release(nonce, "")
    deadline = time.time() + timeout
    offset = None
    while time.time() < deadline:
        params = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40).json()
        except requests.RequestException:
            time.sleep(poll)
            continue

        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            data = cq.get("data", "")
            parts = data.split(":")
            action = parts[0]
            n = parts[1] if len(parts) > 1 else ""
            if action not in ("duo_rerun", "duo_skip"):
                continue  # not a DUO-decision tap
            if nonce and n != nonce:
                requests.post(f"{API}/answerCallbackQuery",
                              data={"callback_query_id": cq["id"],
                                    "text": "This prompt expired — see the latest one."},
                              timeout=20)
                continue
            choice = "rerun" if action == "duo_rerun" else "skip"
            requests.post(f"{API}/answerCallbackQuery",
                          data={"callback_query_id": cq["id"],
                                "text": "Rerunning… 🔁" if choice == "rerun" else "Skipped ⏭️"},
                          timeout=20)
            _commit_offset(offset)  # don't let this tap replay later
            return choice
        time.sleep(poll)
    _commit_offset(offset)
    return "timeout"


if __name__ == "__main__":
    # Smoke test: confirms creds work and sends a hello message.
    if not configured():
        print("Telegram NOT configured. Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env")
    else:
        print(send_message("✅ NUworks auto-apply bot connected."))
