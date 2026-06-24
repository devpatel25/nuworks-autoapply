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
import os
import re
import json
import time
import html
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


def send_message(text: str, parse_mode: str = "HTML") -> dict:
    _require()
    r = requests.post(f"{API}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
                      timeout=30)
    return r.json()


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


def send_resume_for_approval(pdf_path: str, job: dict, summary: str = "",
                             nonce: str = "") -> int:
    """Send the tailored resume PDF with Approve/Reject buttons.
    Returns the sent message_id. callback_data encodes decision:job_id:nonce — the
    optional nonce lets wait_for_decision reject a stale tap from an earlier send
    so it can't auto-approve a freshly tailored resume the user never reviewed."""
    _require()
    jid = job["job_id"]
    caption = (
        f"📄 <b>Tailored resume ready</b>\n\n"
        f"<b>{html.escape(job.get('title','')[:80])}</b>\n"
        f"{html.escape(job.get('company',''))} — {html.escape(job.get('location',''))}\n"
        f"💵 Pay: {html.escape(_fmt_pay(job.get('salary','')))}\n"
        f"🗓 Deadline: {html.escape(str(job.get('deadline') or '—'))}\n"
    )
    # External apply link(s) — so you can apply yourself (e.g. from your phone).
    ext = [u.strip() for u in (job.get("apply_urls", "") or "").split(";") if u.strip()]
    if ext:
        caption += "🌐 Apply externally too (do this yourself): " + "  ".join(ext) + "\n"
    if summary:
        caption += f"\n{html.escape(summary[:400])}"
    caption += "\n\nApprove to auto-apply on NUworks, or Reject to skip."

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve & Apply", "callback_data": f"approve:{jid}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"reject:{jid}:{nonce}"},
    ]]}

    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
                  "reply_markup": __import__("json").dumps(keyboard)},
            files={"document": (Path(pdf_path).name, f, "application/pdf")},
            timeout=60,
        )
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {res}")
    return res["result"]["message_id"]


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
    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
                  "reply_markup": __import__("json").dumps(keyboard)},
            files={"document": (Path(pdf_path).name, f, "application/pdf")},
            timeout=60,
        )
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument (cover letter) failed: {res}")
    return res["result"]["message_id"]


def wait_for_decision(job_id: str, nonce: str = "", timeout: int = 86400, poll: int = 3) -> str:
    """Poll getUpdates until the user taps Approve/Reject for this job_id (and, if a
    nonce is given, for THIS send). Returns 'approve', 'reject', or 'timeout'. Acks
    every tap so spinners clear; a tap for this job with a stale nonce is treated as
    expired (acked + ignored) so it can't approve a resume the user never reviewed."""
    _require()
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
    r = requests.post(
        f"{API}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "reply_markup": json.dumps(keyboard)},
        timeout=30,
    )
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendMessage (duo decision) failed: {res}")
    return res["result"]["message_id"]


def wait_for_duo_decision(nonce: str = "", timeout: int = 3600, poll: int = 3) -> str:
    """Poll getUpdates until the user taps Rerun or Skip on a DUO decision prompt
    (matching this nonce). Returns 'rerun', 'skip', or 'timeout'. Acks every tap so
    the spinner clears, and commits the offset so the tap isn't replayed by a later
    run/poller. A tap with a stale nonce is acked + ignored."""
    _require()
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
