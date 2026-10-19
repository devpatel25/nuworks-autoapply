#!/usr/bin/env python3
"""Committed unit tests for pipeline.py Phase-0 plumbing:
  • _result() ALWAYS carries job_id (the supervisor keys eviction / age-out by it),
    keeps "job" as the title, coerces ids to str, drops None extras.
  • add_pending() is an idempotent-keyed UPSERT that tracks _attempts / _last_class /
    _last_detail / _first_queued — the fields the supervisor caps doomed retry loops on.

Offline; redirects the pending-queue file to a temp dir so data/pending_retry.json is
never touched. Run: python3 -m unittest discover -s tests
"""
import sys
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncio  # noqa: E402
from playwright.async_api import Error as PWError  # noqa: E402

import apply_type as AT  # noqa: E402
import pipeline as P  # noqa: E402

_REAL_LOG_DIR = P.LOG_DIR


def setUpModule():
    """Keep pipeline.log() out of the real logs/run_<date>.log."""
    P.LOG_DIR = Path(tempfile.mkdtemp())


def tearDownModule():
    shutil.rmtree(P.LOG_DIR, ignore_errors=True)
    P.LOG_DIR = _REAL_LOG_DIR


class TestResult(unittest.TestCase):
    def test_carries_job_id_and_title(self):
        job = {"job_id": "12345", "title": "Data Engineer Intern at Foo"}
        r = P._result(job, "applied", detail="ok")
        self.assertEqual(r["job_id"], "12345")
        self.assertEqual(r["result"], "applied")
        self.assertEqual(r["detail"], "ok")
        self.assertTrue(r["job"].startswith("Data Engineer"))
        self.assertNotIn("system_failure", r)        # only present when True

    def test_system_failure_and_extra(self):
        job = {"job_id": 7, "title": "X"}
        r = P._result(job, "error", detail="boom", system_failure=True,
                      extra={"external": ["http://a"], "resume": None})
        self.assertIs(r["system_failure"], True)
        self.assertEqual(r["job_id"], "7")           # coerced to str
        self.assertEqual(r["external"], ["http://a"])
        self.assertNotIn("resume", r)                # None extras are dropped

    def test_missing_job_id_is_empty_string(self):
        r = P._result({"title": "No id"}, "timeout")
        self.assertEqual(r["job_id"], "")


class TestAddPendingUpsert(unittest.TestCase):
    JOB = {"job_id": "999", "title": "ML Intern", "company": "Foo",
           "apply_urls": "", "_apply_type": "in_portal", "_required_docs": []}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._pf = P.PENDING_FILE
        P.PENDING_FILE = self.tmp / "pending_retry.json"

    def tearDown(self):
        P.PENDING_FILE = self._pf
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_queue_records_attempt_one(self):
        P.add_pending(self.JOB, "verify_gap", "unconfirmed submit")
        items = P.load_pending()
        self.assertEqual(len(items), 1)
        e = items[0]
        self.assertEqual(e["job_id"], "999")
        self.assertEqual(e["_attempts"], 1)
        self.assertEqual(e["_last_class"], "verify_gap")
        self.assertEqual(e["_last_detail"], "unconfirmed submit")
        self.assertIn("_first_queued", e)

    def test_reupsert_increments_and_refreshes_class(self):
        P.add_pending(self.JOB, "transient", "first")
        first_queued = P.load_pending()[0]["_first_queued"]
        P.add_pending(self.JOB, "verify_gap", "second")
        items = P.load_pending()
        self.assertEqual(len(items), 1)              # de-duped on job_id
        e = items[0]
        self.assertEqual(e["_attempts"], 2)          # incremented
        self.assertEqual(e["_last_class"], "verify_gap")
        self.assertEqual(e["_last_detail"], "second")
        self.assertEqual(e["_first_queued"], first_queued)   # preserved across re-queue

    def test_default_class_is_transient(self):
        P.add_pending(self.JOB)
        self.assertEqual(P.load_pending()[0]["_last_class"], "transient")

    def test_legacy_entry_without_attempts_upgrades(self):
        # A pre-Phase-0 pending entry (no _attempts) must upsert cleanly: 0 (missing) + 1.
        P.save_pending([{"job_id": "999", "title": "ML Intern"}])
        P.add_pending(self.JOB, "transient", "x")
        e = P.load_pending()[0]
        self.assertEqual(e["_attempts"], 1)


JID = "ef8ec512afababa3bf222a9c4f3cc258"


class _FakePage:
    def __init__(self, url, result=None, exc=None):
        self.url, self.result, self.exc, self.calls = url, result, exc, 0

    async def evaluate(self, js):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class TestFetchDetail(unittest.TestCase):
    def run_fd(self, page):
        return asyncio.run(AT.fetch_detail(page, JID))

    def test_off_origin_page_returns_empty_without_fetching(self):
        page = _FakePage("https://careers.northeastern.edu/", {"job_id": JID})
        self.assertEqual(self.run_fd(page), {})
        self.assertEqual(page.calls, 0)

    def test_playwright_error_returns_empty(self):
        page = _FakePage(AT.PORTAL, exc=PWError("TypeError: Failed to fetch"))
        self.assertEqual(self.run_fd(page), {})
        self.assertEqual(page.calls, 1)

    def test_ok_page_returns_detail_and_null_becomes_empty(self):
        self.assertEqual(self.run_fd(_FakePage(AT.PORTAL, {"job_id": JID})), {"job_id": JID})
        self.assertEqual(self.run_fd(_FakePage(AT.PORTAL, None)), {})

    def test_missing_or_corrupt_session_is_none_so_manual_job_relogs(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "bad.json").write_text("{bad")
        for f in ("missing.json", "bad.json"):  # real headless launch; returns before any goto
            with patch.object(AT, "SESSION", str(d / f)):
                self.assertIsNone(AT.fetch_job(JID), f)


class TestManualApply(unittest.TestCase):
    """/apply <id>: command routing, reject semantics, and _manual_job's early exits."""

    def setUp(self):
        P._closing, P._dry_apply = False, False
        P._manual_futs.clear()
        P._active_ids.clear()
        P._ctx_cache.clear()
        self.msgs = []
        self.tmp = Path(tempfile.mkdtemp())
        self.ps = [patch.object(P, "log", lambda *a: None),
                   patch.object(P.tg, "send_message", lambda t, *a, **k: self.msgs.append(t)),
                   patch.object(P, "_msg", lambda t: self.msgs.append(t)),
                   patch.object(P.tg, "APPLY_QUEUE", self.tmp / "apply_queue.txt")]
        for x in self.ps:
            x.start()

    def tearDown(self):
        for x in self.ps:
            x.stop()
        P._closing = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, **kw):
        return {"job_id": JID, "title": "T", "company": "C", "_apply_type": "in_portal",
                "_apply_target": None, "_required_docs": [], **kw}

    def test_bare_apply_and_unknown_never_kill(self):
        with patch.object(P, "_kill_tree") as kill, patch.object(P.os, "_exit") as ex, \
                patch.object(P, "mark_ran_today") as mark:
            P._on_command("/apply")
            P._on_command("/bogus")
        kill.assert_not_called()
        ex.assert_not_called()
        mark.assert_not_called()
        self.assertEqual(self.msgs, [P.tg.APPLY_USAGE])

    def test_stop_still_kills(self):
        with patch.object(P, "_kill_tree") as kill, patch.object(P.os, "_exit") as ex, \
                patch.object(P, "mark_ran_today") as mark, patch.object(P.tg, "_commit_offset"):
            P._on_command("/stop")
        kill.assert_called_once()
        ex.assert_called_once_with(0)
        mark.assert_called_once()

    def test_stop_kills_even_when_telegram_ack_fails(self):
        def boom(*a, **k):
            raise OSError("network down")
        with patch.object(P, "_kill_tree") as kill, patch.object(P.os, "_exit") as ex, \
                patch.object(P, "mark_ran_today"), patch.object(P.tg, "_commit_offset"), \
                patch.object(P.tg, "send_message", boom):
            P._on_command("/stop")
        kill.assert_called_once()
        ex.assert_called_once_with(0)

    def test_scheduled_batch_skips_posting_claimed_by_apply(self):
        P._active_ids.add(JID)  # a /apply worker owns it
        with patch.object(P.tg, "DISPATCHER"), patch.object(P, "bump_watchdog"), \
                patch.object(P.tg, "send_job_for_approval") as send, \
                patch.object(P, "_posting_worker") as worker:
            self.assertEqual(P.run_postings([self.job()], {}, False), [])
        send.assert_not_called()
        worker.assert_not_called()
        self.assertIn(JID, P._active_ids)  # the /apply claim survives

    def test_apply_submits_then_queues_once_closing(self):
        done = Future()
        done.set_result(None)
        with patch.object(P, "_manual_pool") as pool:
            pool.submit.return_value = done
            P._on_command(f"/apply {JID}")
            pool.submit.assert_called_once_with(P._manual_job, JID)
            self.assertEqual(self.msgs[-1], "📥 Fetching posting…")
            P._join_manual()  # main() past the join point
            P._on_command(f"/apply {JID}")
            pool.submit.assert_called_once()  # not submitted again
        self.assertEqual(P.tg.drain_apply_queue(), [JID])

    def test_join_keeps_run_open_for_a_second_apply_until_none_in_flight(self):
        gate, started = threading.Event(), threading.Event()

        def job(jid):
            started.set()
            gate.wait(5)
            return {"result": "applied", "job_id": jid}

        with patch.object(P, "_manual_job", side_effect=job):
            self.assertTrue(P._submit_manual("a" * 32))
            started.wait(5)
            got = []
            t = threading.Thread(target=lambda: got.extend(P._join_manual()))
            t.start()
            time.sleep(0.1)  # join is waiting on A, run still open
            self.assertTrue(P._submit_manual("b" * 32))
            gate.set()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(sorted(r["job_id"] for r in got), ["a" * 32, "b" * 32])
        self.assertFalse(P._submit_manual("c" * 32))  # closed once nothing is in flight

    def test_manual_reject_skips_skiplist_scheduled_reject_adds(self):
        for manual, expect in ((True, 0), (False, 1)):
            with patch.object(P.tg, "wait_for_decision", return_value="reject"), \
                    patch.object(P, "add_to_skiplist") as skip, patch.object(P, "clear_pending"):
                r = P._posting_worker(self.job(_manual=manual), "n", {}, False)
            self.assertEqual(r["result"], "reject")
            self.assertEqual(skip.call_count, expect)

    def test_worker_releases_active_id(self):
        P._active_ids.add(JID)
        with patch.object(P.tg, "wait_for_decision", return_value="reject"), \
                patch.object(P, "add_to_skiplist"), patch.object(P, "clear_pending"):
            P._posting_worker(self.job(), "n", {}, False)
        self.assertNotIn(JID, P._active_ids)

    def test_manual_external_messages_and_sends_no_card(self):
        job = self.job(_apply_type="external", _apply_target="https://x.example/apply")
        with patch.object(P.apply_type, "fetch_job", return_value=job), \
                patch.object(P.tg, "send_job_for_approval") as card:
            r = P._manual_job(JID)
        card.assert_not_called()
        self.assertEqual(r["result"], "external")
        self.assertIn("https://x.example/apply", self.msgs[-1])

    def test_manual_refetches_after_one_duo_scrape_then_gives_up(self):
        with patch.object(P.apply_type, "fetch_job", return_value=None), \
                patch.object(P, "scrape_with_duo_retry", return_value=True) as scr, \
                patch.object(P.tg, "send_job_for_approval") as card:
            self.assertIsNone(P._manual_job(JID))
        scr.assert_called_once_with(mark_skip=False)
        card.assert_not_called()
        self.assertIn("Couldn't load posting", self.msgs[-1])

    def test_manual_fetch_none_none_then_job_after_duo_sends_card(self):
        with patch.object(P.apply_type, "fetch_job", side_effect=[None, None, self.job()]), \
                patch.object(P, "scrape_with_duo_retry", return_value=True) as scr, \
                patch.object(P, "_get_ctx", return_value={"audit": {}}), \
                patch.object(P.tg, "send_job_for_approval") as card, \
                patch.object(P, "_posting_worker", return_value={"result": "reject"}):
            r = P._manual_job(JID)
        scr.assert_called_once_with(mark_skip=False)
        card.assert_called_once()
        self.assertEqual(r, {"result": "reject"})

    def test_manual_duo_skip_never_marks_today(self):
        with patch.object(P.apply_type, "fetch_job", return_value=None), \
                patch.object(P, "scrape_with_duo_retry", return_value=False), \
                patch.object(P, "mark_ran_today") as mark:
            self.assertIsNone(P._manual_job(JID))
        mark.assert_not_called()

    def test_manual_duplicate_card_is_refused_and_keeps_claim(self):
        P._active_ids.add(JID)  # a scheduled card for it is live
        with patch.object(P.apply_type, "fetch_job", return_value=self.job()), \
                patch.object(P.tg, "send_job_for_approval") as card:
            self.assertIsNone(P._manual_job(JID))
        card.assert_not_called()
        self.assertIn(JID, P._active_ids)  # the live card's claim survives

    def test_manual_happy_path_marks_job_manual_and_runs_worker(self):
        with patch.object(P.apply_type, "fetch_job", return_value=self.job()), \
                patch.object(P, "_get_ctx", return_value={"audit": {}}), \
                patch.object(P.tg, "send_job_for_approval") as card, \
                patch.object(P, "_posting_worker", return_value={"result": "reject"}) as w:
            r = P._manual_job(JID)
        self.assertEqual(r, {"result": "reject"})
        card.assert_called_once()
        self.assertTrue(w.call_args[0][0]["_manual"])

    def _run_main(self, queued, flag=False, day_done=False):
        """main() with every side effect stubbed; returns the mocks that matter."""
        flagfile = self.tmp / "manual_run.flag"
        if flag:
            flagfile.touch()
        names = ("mark_ran_today", "scrape_with_duo_retry", "load_jobs", "eligible",
                 "merge_pending", "_start_watchdog")
        ps = {n: patch.object(P, n) for n in names}
        ps["supervisor"] = patch("supervisor.run_supervisor")
        ps["announce"] = patch.object(P.stats, "announce_new_postings")
        res = {"result": "reject", "job": "T", "job_id": JID}
        with patch.object(P, "PAUSE_FLAG", self.tmp / "nope"), \
                patch.object(P.tg, "MANUAL_FLAG", flagfile), \
                patch.object(P, "acquire_singleton_lock", return_value=True), \
                patch.object(P, "already_ran_today", return_value=day_done), \
                patch.object(P.events, "instrument"), patch.object(P.atexit, "register"), \
                patch.object(P.tg, "DISPATCHER"), \
                patch.object(P.tg, "configured", return_value=True), \
                patch.object(P.tg, "drain_apply_queue", return_value=queued), \
                patch.object(P, "_manual_job", return_value=res) as job, \
                patch.object(P.stats, "record_run") as rec, \
                patch.object(sys, "argv", ["pipeline.py"]):
            m = {n: p.start() for n, p in ps.items()}
            m["scrape_with_duo_retry"].return_value = True
            m["load_jobs"].return_value = []
            m["eligible"].return_value = []
            m["merge_pending"].side_effect = lambda t: t
            try:
                P.main()
            finally:
                for p in ps.values():
                    p.stop()
        return m, job, rec, flagfile

    def test_main_queue_with_flag_is_manual_and_consumes_flag(self):
        m, job, rec, flagfile = self._run_main([JID], flag=True)
        job.assert_called_once_with(JID)
        m["scrape_with_duo_retry"].assert_not_called()
        m["load_jobs"].assert_not_called()
        m["mark_ran_today"].assert_not_called()
        self.assertFalse(rec.call_args[1]["marked"])
        self.assertFalse(flagfile.exists())

    def test_main_queue_no_flag_day_open_runs_normally_and_ids_ride_along(self):
        m, job, rec, flagfile = self._run_main([JID])
        m["scrape_with_duo_retry"].assert_called_once()
        m["load_jobs"].assert_called_once()
        job.assert_called_once_with(JID)
        m["mark_ran_today"].assert_called_once()  # a normal run still commits the day
        self.assertTrue(rec.call_args[1]["marked"])

    def test_main_queue_no_flag_day_done_is_manual(self):
        m, job, rec, _ = self._run_main([JID], day_done=True)
        job.assert_called_once_with(JID)
        m["scrape_with_duo_retry"].assert_not_called()
        m["mark_ran_today"].assert_not_called()

    def test_main_no_queue_day_done_exits_without_any_manual_path(self):
        m, job, rec, _ = self._run_main([], day_done=True)
        job.assert_not_called()
        m["scrape_with_duo_retry"].assert_not_called()
        rec.assert_not_called()  # the day guard returned before anything ran


if __name__ == "__main__":
    unittest.main()
