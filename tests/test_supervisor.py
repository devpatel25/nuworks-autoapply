#!/usr/bin/env python3
"""Committed unit tests for supervisor.py: the deterministic classification floor
(pre_classify / route / build_report) and the loop-break state machine (verify_gap
retry -> evict, data_gap eviction, age-out, the launchd backstop), adapted to NUworks's
apply taxonomy.

Offline: Telegram is mocked and every JSON state file is redirected to a temp dir, so no
real data/* file is ever touched. Run: python3 -m unittest discover -s tests
"""
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import supervisor as S  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_success_routes(self):
        for st in ("applied", "already_applied", "staged"):
            self.assertEqual(S.route(st, S.pre_classify(st), 0), "success")

    def test_benign_routes(self):
        for st in ("reject", "cover_letter_rejected", "no_inportal_apply", "external", "no_telegram"):
            self.assertEqual(S.route(st, S.pre_classify(st), 0), "info")

    def test_submitted_unverified_verify_gap_then_code_break(self):
        self.assertEqual(S.pre_classify("submitted_unverified", 0), "verify_gap")
        self.assertEqual(S.pre_classify("submitted_unverified", 1), "verify_gap")
        self.assertEqual(S.pre_classify("submitted_unverified", 2), "code_break")

    def test_blocked_missing_documents_is_data_gap_and_evicts(self):
        self.assertEqual(S.pre_classify("blocked_missing_documents", 0), "data_gap")
        self.assertEqual(S.route("blocked_missing_documents", "data_gap", 0), "evict")

    def test_error_transient_then_code_break(self):
        self.assertEqual(S.pre_classify("error", 0), "transient")
        self.assertEqual(S.pre_classify("error", 1), "code_break")
        self.assertEqual(S.pre_classify("crashed", 0), "transient")

    def test_deferrals_keep_retry_regardless_of_attempts(self):
        for st in ("timeout", "cover_letter_timeout", "cap_reached"):
            self.assertEqual(S.pre_classify(st, 0), "transient")
            self.assertEqual(S.route(st, "transient", 99), "keep_retry")  # never hit the cap

    def test_verify_gap_keep_retry_until_cap(self):
        self.assertEqual(S.route("submitted_unverified", "verify_gap", 0), "keep_retry")
        self.assertEqual(S.route("submitted_unverified", "verify_gap", S.MAX_RETRY_ATTEMPTS), "evict")


class TestReport(unittest.TestCase):
    NOW = datetime(2026, 6, 26, 10, 30)

    def test_build_report_sections(self):
        msg = S.build_report(["Job A"], [("Job B", "retrying soon")],
                             [("Job C", "needs a transcript", None)], [], self.NOW)
        self.assertIn("NUworks run report", msg)
        self.assertIn("✅ Applied: 1", msg)
        self.assertIn("Job A", msg)
        self.assertIn("Retrying next run: 1", msg)
        self.assertIn("Job B", msg)
        self.assertIn("Needs you: 1", msg)
        self.assertIn("needs a transcript", msg)

    def test_build_report_escapes_html(self):
        msg = S.build_report(["<script>"], [], [], [], self.NOW)
        self.assertNotIn("<script>", msg)
        self.assertIn("&lt;script&gt;", msg)


class _State(unittest.TestCase):
    """Redirect every state file + mock Telegram so _run is exercised with zero side effects."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: getattr(S, k) for k in
                       ("PENDING_FILE", "WONTFIX_FILE", "PROPOSED_FIXES_FILE", "LOGS", "DATA")}
        S.DATA = self.tmp
        S.LOGS = self.tmp / "logs"
        S.PENDING_FILE = self.tmp / "pending_retry.json"
        S.WONTFIX_FILE = self.tmp / "wontfix.json"
        S.PROPOSED_FIXES_FILE = self.tmp / "proposed_fixes.json"
        self.sent = []
        self._conf, S.tg.configured = S.tg.configured, (lambda: True)
        self._send, S.tg.send_message = S.tg.send_message, (lambda m, *a, **k: self.sent.append(m))
        self.now = datetime(2026, 6, 26, 10, 30)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(S, k, v)
        S.tg.configured, S.tg.send_message = self._conf, self._send
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pending(self, items):
        S._save(S.PENDING_FILE, items)

    def _wontfix(self):
        return S._load(S.WONTFIX_FILE, {})


class TestLoopBreak(_State):
    def test_submitted_unverified_evicts_after_two_attempts(self):
        # The regression that motivated the supervisor: a verify_gap that never confirms
        # must STOP being re-queued. At attempts>=2 it reclassifies to code_break -> evict.
        jid = "555"
        self._pending([{"job_id": jid, "title": "Risky Co", "_attempts": 2,
                        "_last_class": "verify_gap", "_first_queued": self.now.isoformat()}])
        S._run([{"job_id": jid, "job": "Risky Co", "result": "submitted_unverified"}], self.now)
        self.assertIn(jid, self._wontfix())                      # evicted to wontfix
        self.assertEqual(S._load(S.PENDING_FILE, []), [])        # removed from the retry queue
        self.assertTrue(any("Risky Co" in m for m in self.sent))

    def test_verify_gap_below_threshold_keeps_retrying(self):
        jid = "556"
        self._pending([{"job_id": jid, "title": "Maybe Co", "_attempts": 1,
                        "_last_class": "verify_gap", "_first_queued": self.now.isoformat()}])
        S._run([{"job_id": jid, "job": "Maybe Co", "result": "submitted_unverified"}], self.now)
        self.assertNotIn(jid, self._wontfix())                   # NOT evicted yet
        self.assertTrue(any("Retrying next run" in m for m in self.sent))

    def test_data_gap_evicts_even_when_not_in_pending(self):
        # blocked_missing_documents is never queued by process(); it must still be evicted
        # so eligible() filters it out of the next scrape.
        jid = "777"
        self._pending([])
        S._run([{"job_id": jid, "job": "Docs Co", "result": "blocked_missing_documents",
                 "detail": "transcript required"}], self.now)
        wf = self._wontfix()
        self.assertIn(jid, wf)
        self.assertIn("transcript", wf[jid]["reason"])

    def test_age_out_evicts_stragglers(self):
        jid = "888"
        old = (self.now - timedelta(days=S.AGE_OUT_DAYS + 1)).isoformat()
        self._pending([{"job_id": jid, "title": "Stale Co", "_attempts": 1,
                        "_last_class": "transient", "_first_queued": old}])
        S._run([], self.now)                                      # not seen this run
        self.assertIn(jid, self._wontfix())

    def test_clean_run_reports_applied_no_eviction(self):
        self._pending([])
        S._run([{"job_id": "1", "job": "Good Co", "result": "applied"}], self.now)
        self.assertEqual(self._wontfix(), {})
        self.assertTrue(any("Applied: 1" in m for m in self.sent))

    def test_benign_only_run_is_silent(self):
        self._pending([])
        S._run([{"job_id": "2", "job": "Nope Co", "result": "reject"}], self.now)
        self.assertEqual(self._wontfix(), {})
        self.assertEqual(self.sent, [])          # nothing applied/retrying/needs-you -> no report


class TestBackstop(_State):
    def test_backstop_evicts_capped_pending_when_no_sentinel(self):
        jid = "999"
        self._pending([{"job_id": jid, "title": "Capped Co", "_attempts": S.MAX_RETRY_ATTEMPTS,
                        "_last_class": "verify_gap", "_first_queued": self.now.isoformat()}])
        S.reconstruct_and_run(now=self.now)
        self.assertIn(jid, self._wontfix())

    def test_backstop_is_noop_if_sentinel_fresh(self):
        self._pending([{"job_id": "x", "title": "Y", "_attempts": 99, "_last_class": "verify_gap",
                        "_first_queued": self.now.isoformat()}])
        S._write_sentinel(self.now, {"applied": 0})
        S.reconstruct_and_run(now=self.now)
        self.assertEqual(self._wontfix(), {})    # in-process run already reported -> untouched


if __name__ == "__main__":
    unittest.main()
