#!/usr/bin/env python3
"""
Always-on Telegram listener for /start, /stop and /apply on @NuWorksAutoApplyBot.

One bot token = one getUpdates stream, so ownership is handed off by the
pipeline's flock (logs/pipeline.lock): while a run holds it, pipeline.py's
tg.DISPATCHER polls and handles the commands (pipeline._on_command); while
it's free, this process polls. Runs under launchd (com.devpatel.nuworks-control).
"""
import os
import time
import fcntl
import subprocess
from datetime import date
from pathlib import Path

import requests

import telegram_bot as tg

HERE = Path(__file__).resolve().parent
LOCK_FILE = HERE / "logs/pipeline.lock"
RUN_STAMP = HERE / "logs/last_run_date.txt"
PAUSE_FLAG = HERE.parent / ".autoapply_paused.nuworks"
LABEL = "com.devpatel.nuworks-autoapply"
KICK_COOLDOWN_S = 60  # launchd needs a moment to take the lock; don't re-kick for a queued /apply
_last_kick = 0.0


def pipeline_running() -> bool:
    """Lock we can't take = a run is live. O_RDWR so the pid line survives."""
    if not LOCK_FILE.exists():
        return False
    fd = os.open(LOCK_FILE, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def kickstart():
    # The scheduled label is TCC-granted and launchd owns the process.
    global _last_kick
    _last_kick = time.time()
    subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], check=True)


def handle(cmd: str):
    verb, _, arg = cmd.partition(" ")
    if verb == "/apply":
        if not arg:
            tg.send_message(tg.APPLY_USAGE)
            return
        tg.queue_apply(arg)
        if pipeline_running():  # lost the race; the run (or the idle loop after it) takes it
            tg.send_message("📥 Queued into current run.")
        elif PAUSE_FLAG.exists():
            tg.send_message("⏸️ NUworks is paused — queued; it runs when you resume.")
        else:
            tg.MANUAL_FLAG.touch()
            kickstart()
            tg.send_message("📥 Starting a run for that posting.")
        return
    if pipeline_running():
        # Lost the race to a just-started run; it'll see its own commands from now on.
        if verb == "/start":
            tg.send_message("▶️ NUworks run is already in progress. Send /stop to kill it.")
        return
    if verb == "/start":
        PAUSE_FLAG.unlink(missing_ok=True)
        RUN_STAMP.unlink(missing_ok=True)  # bypass the once-per-day guard
        tg.MANUAL_FLAG.unlink(missing_ok=True)  # a full run; queued /apply ids ride along
        kickstart()
        tg.send_message("🚀 Starting a fresh NUworks run.")
    elif verb == "/stop":
        RUN_STAMP.write_text(date.today().isoformat())
        tg.send_message("🛑 Nothing is running. Cancelled the rest of today's "
                        "scheduled runs — send /start to run now.")


def drain(updates: list, offset):
    """Handle a getUpdates batch; returns (offset to confirm, handed_off). A run can
    take the lock while our poll was in flight: from then on the batch is left
    unconfirmed so the pipeline's dispatcher (which starts at offset None) receives
    it — e.g. a /stop meant for that run is never swallowed here."""
    # ponytail: per-update lock check; a run starting mid-handle() is still a ms-wide window
    for upd in updates:
        if pipeline_running():
            return offset, True
        offset = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if cq:
            tg.answer_callback(cq["id"], "This card is no longer active.")
        cmd = tg.parse_command(upd)
        if cmd:
            try:
                handle(cmd)
            except Exception as e:
                print(f"{cmd} failed: {e}", flush=True)
                tg.send_message(f"⚠️ {cmd} failed: {e}")
    return offset, False


def main():
    tg._require()
    requests.post(f"{tg.API}/setMyCommands", json={"commands": [
        {"command": "start", "description": "Start a fresh NUworks run now"},
        {"command": "stop", "description": "Kill the current run, skip rest of today"},
        {"command": "apply", "description": "Apply to a NUworks posting URL"},
    ]}, timeout=20)
    offset = None
    while True:
        if pipeline_running():
            offset = None  # the pipeline advances the stream; re-sync after
            time.sleep(5)
            continue
        if (tg.APPLY_QUEUE.exists() and tg.APPLY_QUEUE.stat().st_size and not PAUSE_FLAG.exists()
                and time.time() - _last_kick >= KICK_COOLDOWN_S):
            try:  # an /apply landed while a run was closing — start the run that takes it
                tg.MANUAL_FLAG.touch()
                kickstart()
            except Exception as e:
                print(f"queued /apply kickstart failed: {e}", flush=True)
        params = {"timeout": 5}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(f"{tg.API}/getUpdates", params=params, timeout=20).json()
        except (requests.RequestException, ValueError):
            time.sleep(5)
            continue
        offset, handed_off = drain(r.get("result", []), offset)
        tg._commit_offset(offset)  # confirm now, so a run starting next never replays these
        if handed_off:
            offset = None
            continue
        if not r.get("ok", True):
            time.sleep(5)  # 409 while the pipeline's poller hands back


if __name__ == "__main__":
    main()
