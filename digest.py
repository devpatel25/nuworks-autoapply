#!/usr/bin/env python3
"""
Nightly NUworks status digest (dead-man's switch). Run by launchd at ~21:30.

Reads today's stats (stats.py) + announced_jobs.json and Telegrams a one-shot
summary: scraped / new / applied / skipped (+ errors). If nothing ran today at
all, it says so — that's the whole point: a silent-failure day becomes visible.
"""
import html
from datetime import date
from pathlib import Path

import stats
import telegram_bot as tg

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"


def build_message() -> str:
    today = date.today()
    tpath = stats.today_path(today)
    runlog = LOG_DIR / f"run_{today:%Y%m%d}.log"

    if not tpath.exists() and not runlog.exists():
        return ("🌙 <b>NUworks nightly digest</b>\n\n"
                "⚠️ The pipeline did <b>NOT run</b> today. Check the Mac was on and "
                "that you approved the DUO push when it fired.")

    t = stats.load_today()
    announced = stats._load(stats.ANNOUNCED, {})
    new_today = [v for v in announced.values() if v.get("date") == today.isoformat()]

    scraped = len(t.get("scraped_ids", []))
    applied = t.get("applied", [])
    skipped = t.get("skipped", [])
    errors = t.get("errors", [])
    runs = t.get("runs", [])

    msg = (f"🌙 <b>NUworks nightly digest — {today.isoformat()}</b>\n\n"
           f"🔎 Scraped: {scraped} posting(s)\n"
           f"🆕 New today: {len(new_today)}\n"
           f"✅ Applied: {len(applied)}\n"
           f"⏭️ Skipped: {len(skipped)}\n")
    if errors:
        msg += f"🛑 Unresolved errors: {len(errors)}\n"
    if runs:
        msg += f"🕑 Runs: {', '.join(runs)}\n"

    esc = html.escape
    if applied:
        msg += "\n<b>Applied:</b>\n" + "\n".join(f"• {esc(a.get('job',''))}" for a in applied)
    if new_today:
        msg += "\n\n<b>New postings:</b>\n" + "\n".join(
            f"• {esc(n.get('title',''))} — {esc(n.get('company',''))}" for n in new_today)
    if errors:
        msg += "\n\n<b>Errors (will retry):</b>\n" + "\n".join(
            f"• {esc(e.get('job',''))} [{esc(e.get('reason',''))}]" for e in errors)
    if not (applied or new_today or skipped or errors):
        msg += "\nNo new activity today."
    return msg


def main():
    msg = build_message()
    if tg.configured():
        tg.send_message(msg)
    else:
        print(msg)


if __name__ == "__main__":
    main()
