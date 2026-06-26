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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline as P  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
