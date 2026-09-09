#!/usr/bin/env python3
"""Committed unit test for job_scraper.classify_login_page(): asserts it labels
each real captured dead-end page correctly, using the actual HTML dumps in
logs/screenshots/ as fixtures (no network, no browser, no new deps).

Run: python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import job_scraper as JS  # noqa: E402

SHOTS = ROOT / "logs" / "screenshots"

# filename -> expected label, from the real 2026-09-03 / 2026-09-07 captures.
FIXTURES = {
    "20260903_161410_login_timeout.html": "network_error",
    "20260903_174456_login_timeout.html": "duo_warning",
    "20260907_182015_login_timeout.html": "duo_warning",
    "20260907_203716_login_timeout.html": "aadsts_error",
    "20260907_214001_login_timeout.html": "duo_push_timeout",
}


class TestClassifyLoginPage(unittest.TestCase):
    def test_known_dead_ends_labeled_correctly(self):
        for name, expected in FIXTURES.items():
            path = SHOTS / name
            self.assertTrue(path.exists(), f"missing fixture {path}")
            content = path.read_text(encoding="utf-8")
            self.assertEqual(JS.classify_login_page(content), expected, name)

    def test_in_progress_page_is_not_a_dead_end(self):
        self.assertIsNone(JS.classify_login_page("<html><body>loading…</body></html>"))

    def test_authenticated_portal_page_is_not_a_dead_end(self):
        self.assertIsNone(JS.classify_login_page("<html><body>NUworks Job Search</body></html>"))


if __name__ == "__main__":
    unittest.main()
