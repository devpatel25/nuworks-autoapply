#!/usr/bin/env python3
"""Telegram /start + /stop control: command parsing, dispatcher routing,
DUO decision via the dispatcher, and control_bot's idle handling.
Offline. Run: python3 -m unittest discover -s tests
"""
import sys
import time
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import telegram_bot as TB  # noqa: E402
import control_bot as CB  # noqa: E402


def msg(text, chat="42", age=0):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text,
                                        "date": int(time.time()) - age}}


@patch("telegram_bot.CHAT_ID", "42")
class TestParseCommand(unittest.TestCase):
    def test_fresh_commands(self):
        self.assertEqual(TB.parse_command(msg("/stop")), "/stop")
        self.assertEqual(TB.parse_command(msg("/start@NuWorksAutoApplyBot")), "/start")

    def test_rejects_other_chat_stale_and_noise(self):
        self.assertIsNone(TB.parse_command(msg("/stop", chat="7")))
        self.assertIsNone(TB.parse_command(msg("/stop", age=600)))
        self.assertIsNone(TB.parse_command(msg("hello")))
        self.assertIsNone(TB.parse_command({"update_id": 1, "callback_query": {}}))

    def test_apply_extracts_job_id(self):
        jid = "ef8ec512afababa3bf222a9c4f3cc258"
        for text in (f"/apply https://northeastern-csm.symplicity.com/students/app/jobs/detail/{jid}",
                     f"/apply https://x.symplicity.com/students/app/jobs/search?currentJobId={jid}&perPage=20",
                     f"/apply {jid}", f"/apply@NuWorksAutoApplyBot {jid.upper()}"):
            self.assertEqual(TB.parse_command(msg(text)), f"/apply {jid}", text)

    def test_apply_bare_or_no_id(self):
        self.assertEqual(TB.parse_command(msg("/apply")), "/apply")
        self.assertEqual(TB.parse_command(msg("/apply not-a-job")), "/apply")
        self.assertIsNone(TB.parse_command(msg("apply ef8ec512afababa3bf222a9c4f3cc258")))

    def test_apply_wrong_chat_rejected(self):
        self.assertIsNone(TB.parse_command(msg("/apply ef8ec512afababa3bf222a9c4f3cc258", chat="7")))

    def test_dispatcher_routes_command(self):
        d = TB.CallbackDispatcher()
        got = []
        d.on_command = got.append
        fake = type("R", (), {"json": lambda self: {"result": [msg("/stop")]}})()
        with patch("telegram_bot.requests.get", return_value=fake):
            d.poll_once(poll_timeout=0)
        self.assertEqual(got, ["/stop"])
        self.assertEqual(d.offset, 2)


class TestApplyQueue(unittest.TestCase):
    def test_queue_drain_round_trip(self):
        with patch.object(TB, "APPLY_QUEUE", Path(tempfile.mkdtemp()) / "apply_queue.txt"):
            self.assertEqual(TB.drain_apply_queue(), [])  # no file yet
            TB.queue_apply("a" * 32)
            TB.queue_apply("b" * 32)
            TB.queue_apply("a" * 32)  # duplicate collapses
            self.assertEqual(TB.drain_apply_queue(), ["a" * 32, "b" * 32])
            self.assertEqual(TB.drain_apply_queue(), [])  # drained = emptied

    def test_queue_lock_is_flock_shared_across_processes(self):
        import fcntl
        with patch.object(TB, "APPLY_QUEUE", Path(tempfile.mkdtemp()) / "apply_queue.txt"):
            # Another process holding the flock (a separate open = separate holder).
            held = open(TB.APPLY_QUEUE.with_suffix(".lock"), "a")
            fcntl.flock(held, fcntl.LOCK_EX)
            t = threading.Thread(target=TB.queue_apply, args=("a" * 32,))
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive())  # blocked: no append between read and truncate
            self.assertFalse(TB.APPLY_QUEUE.exists())
            held.close()  # releases the flock
            t.join(5)
            self.assertEqual(TB.drain_apply_queue(), ["a" * 32])


class TestDuoViaDispatcher(unittest.TestCase):
    @patch("telegram_bot.answer_callback", lambda *a: None)
    @patch("telegram_bot.configured", lambda: True)
    def test_rerun_tap_reaches_waiter(self):
        d = TB.CallbackDispatcher()
        d._thread = threading.Thread(target=time.sleep, args=(2,))
        d._thread.start()  # fake "running"
        with patch("telegram_bot.DISPATCHER", d):
            threading.Timer(0.2, d.route, [{"id": "x", "data": "duo_rerun:abc"}]).start()
            self.assertEqual(TB.wait_for_duo_decision(nonce="abc", timeout=2), "rerun")


class TestControlBotIdle(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.stamp, self.flag = tmp / "last_run_date.txt", tmp / ".paused"
        self.queue, self.mflag = tmp / "apply_queue.txt", tmp / "manual_run.flag"
        self.sent = []
        self.p = [patch.object(CB, "RUN_STAMP", self.stamp), patch.object(CB, "PAUSE_FLAG", self.flag),
                  patch.object(CB, "LOCK_FILE", tmp / "pipeline.lock"),
                  patch.object(TB, "APPLY_QUEUE", self.queue),
                  patch.object(TB, "MANUAL_FLAG", self.mflag),
                  patch("telegram_bot.send_message", lambda t, *a, **k: self.sent.append(t))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_start_clears_guard_and_kicks(self):
        self.stamp.write_text(date.today().isoformat())
        self.flag.touch()
        with patch.object(CB, "kickstart") as kick:
            CB.handle("/start")
        kick.assert_called_once()
        self.assertFalse(self.stamp.exists() or self.flag.exists())

    def test_stop_idle_marks_today(self):
        CB.handle("/stop")
        self.assertEqual(self.stamp.read_text(), date.today().isoformat())

    JID = "ef8ec512afababa3bf222a9c4f3cc258"

    def test_apply_idle_queues_and_kicks_without_touching_stamp(self):
        self.stamp.write_text("2000-01-01")
        with patch.object(CB, "kickstart") as kick:
            CB.handle(f"/apply {self.JID}")
        kick.assert_called_once()
        self.assertEqual(TB.drain_apply_queue(), [self.JID])
        self.assertEqual(self.stamp.read_text(), "2000-01-01")
        self.assertTrue(self.mflag.exists())  # this run is manual-only

    def test_start_clears_manual_flag_so_queued_ids_ride_a_full_run(self):
        self.mflag.touch()
        with patch.object(CB, "kickstart"):
            CB.handle("/start")
        self.assertFalse(self.mflag.exists())

    def test_apply_while_running_only_queues(self):
        with patch.object(CB, "pipeline_running", return_value=True), \
                patch.object(CB, "kickstart") as kick:
            CB.handle(f"/apply {self.JID}")
        kick.assert_not_called()
        self.assertEqual(TB.drain_apply_queue(), [self.JID])
        self.assertIn("Queued into current run", self.sent[-1])
        self.assertFalse(self.mflag.exists())

    def test_apply_paused_queues_without_kick(self):
        self.flag.touch()
        with patch.object(CB, "kickstart") as kick:
            CB.handle(f"/apply {self.JID}")
        kick.assert_not_called()
        self.assertEqual(TB.drain_apply_queue(), [self.JID])
        self.assertFalse(self.mflag.exists())  # kick (and flag) wait for the resume

    def test_bare_apply_and_unknown_do_nothing(self):
        with patch.object(CB, "kickstart") as kick:
            CB.handle("/apply")
            CB.handle("/bogus")
        kick.assert_not_called()
        self.assertEqual(self.sent, [TB.APPLY_USAGE])
        self.assertEqual(TB.drain_apply_queue(), [])
        self.assertFalse(self.stamp.exists())  # unknown must not fall into the /stop branch

    def test_run_taking_lock_mid_batch_leaves_rest_unconfirmed(self):
        ups = [{"update_id": 7, "message": {}}, {"update_id": 8, "message": {}}]
        running = iter([False, True])  # the run grabs the lock after update 7
        with patch.object(CB, "pipeline_running", lambda: next(running)), \
                patch.object(TB, "parse_command", lambda u: "/stop"), \
                patch.object(CB, "handle") as h:
            offset, handed = CB.drain(ups, None)
        self.assertEqual((offset, handed), (8, True))  # 8 (the /stop) stays for the run
        h.assert_called_once_with("/stop")


if __name__ == "__main__":
    unittest.main()
