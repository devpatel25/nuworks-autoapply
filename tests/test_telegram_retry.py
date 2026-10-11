#!/usr/bin/env python3
"""Committed unit test for telegram_bot._post_with_retry(): the fix for the
2026-09-10 incident where an un-retried ConnectionResetError on
send_resume_for_approval crashed 4 jobs in one run.

Offline; monkeypatches time.sleep and never sets real Telegram creds. Run:
  python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import telegram_bot as TB  # noqa: E402


class TestPostWithRetry(unittest.TestCase):
    def test_succeeds_first_try(self):
        calls = []
        result = TB._post_with_retry(lambda: calls.append(1) or {"ok": True})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)

    @patch("telegram_bot.time.sleep", lambda *_: None)
    def test_retries_past_a_transient_connection_error_then_succeeds(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise requests.exceptions.ConnectionError("Connection reset by peer")
            return {"ok": True, "result": {"message_id": 1}}

        result = TB._post_with_retry(flaky)
        self.assertEqual(attempts["n"], 3)
        self.assertTrue(result["ok"])

    @patch("telegram_bot.time.sleep", lambda *_: None)
    def test_raises_after_exhausting_all_attempts(self):
        def always_fails():
            raise requests.exceptions.ConnectionError("Connection reset by peer")

        with self.assertRaises(RuntimeError):
            TB._post_with_retry(always_fails, attempts=3)

    @patch("telegram_bot.time.sleep", lambda *_: None)
    def test_honors_429_retry_after_without_counting_as_a_failed_attempt(self):
        calls = []

        def rate_limited_once():
            calls.append(1)
            if len(calls) == 1:
                return {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
            return {"ok": True}

        result = TB._post_with_retry(rate_limited_once)
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
