#!/usr/bin/env python3
"""
Northeastern NUworks Job Scraper
- Credentials saved to .env (prompted once)
- Browser session saved to session.json (DUO only on first run or when session expires)
- Calls the portal's own JSON API directly — fast and reliable
- Output: jobs.csv
"""

import os
import re
import sys
import csv
import json
import html
import asyncio
import getpass
from pathlib import Path
from dotenv import load_dotenv, set_key
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HERE         = Path(__file__).resolve().parent
SESSION_FILE = HERE / "session.json"
ENV_FILE     = HERE / ".env"
OUTPUT_FILE  = HERE / "data" / "jobs.csv"

BASE         = "https://northeastern-csm.symplicity.com"
PORTAL_URL   = f"{BASE}/students/app/jobs/search"
LOGIN_HINTS  = ["login", "shibboleth", "idp.", "auth", "signin"]

DUO_WAIT_SECONDS = int(os.getenv("DUO_WAIT_SECONDS", "600"))  # wait up to 10 min for DUO approval (override via env, e.g. for demos/tests)


def notify_telegram(text: str):
    """Best-effort Telegram alert; silent no-op if not configured/available."""
    try:
        import requests
        load_dotenv(ENV_FILE)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not (token and chat_id):
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text}, timeout=20)
    except Exception:
        pass

# Output CSV columns (kept here so save_csv can write a header even for an empty
# result — clearing stale rows when a scrape genuinely finds nothing).
CSV_FIELDS = [
    "job_id", "title", "company", "location", "type", "posted",
    "deadline", "salary", "remote", "qualified", "apply_urls", "description",
]


class ScrapeError(RuntimeError):
    """The jobs API could not be reached/parsed at all (network/session problem).
    Distinct from a successful fetch that legitimately returns zero jobs — the
    caller exits non-zero on this so the pipeline leaves the day UNMARKED and
    retries, instead of silently keeping a stale jobs.csv (the 2026-06-25 bug)."""


# Job-search filters. job_type=5,17 = co-op/internship; targeted_academic_majors
# is the major code; exclude_applied_jobs drops anything already applied to.
# `postdate` is the look-back window in days (1 = past 24h) — override on the CLI
# with --postdate / --window-days N for a broader sweep.
def _filters(postdate: int) -> str:
    return ("&job_type=5,17"
            f"&postdate={int(postdate)}"
            "&targeted_academic_majors=0160"
            "&exclude_applied_jobs=1")


def search_url(postdate: int, page: int = 1, per_page: int = 100) -> str:
    """Full job-search page URL. Navigating here makes the React app fire its own
    authenticated, filtered jobs call — which scrape_all_jobs intercepts."""
    return (f"{PORTAL_URL}?perPage={per_page}&page={page}&sort=!postdate&ocr=f"
            + _filters(postdate))


# ── Credentials ───────────────────────────────────────────────────────────────

def load_credentials():
    load_dotenv(ENV_FILE)
    username = os.getenv("NEU_USERNAME", "").strip()
    password = os.getenv("NEU_PASSWORD", "").strip()

    if not username or not password:
        print("=" * 55)
        print("  First-time setup — enter credentials once.")
        print("=" * 55)
        username = input("  Northeastern username: ").strip()
        password = getpass.getpass("  Password: ")
        ENV_FILE.touch(mode=0o600)
        set_key(str(ENV_FILE), "NEU_USERNAME", username)
        set_key(str(ENV_FILE), "NEU_PASSWORD", password)
        print("\n  ✓ Credentials saved to .env\n")

    return username, password


# ── Login / session ───────────────────────────────────────────────────────────

def looks_like_login(url: str) -> bool:
    return any(h in url.lower() for h in LOGIN_HINTS)


async def trigger_duo_push(page) -> bool:
    """Auto-click DUO's 'Send Me a Push' so the push is sent without manual
    interaction. Checks the main page AND any Duo iframe; retries ~30s while the
    prompt renders. Returns True once clicked."""
    push_selectors = [
        'button:has-text("Send Me a Push")',
        'a:has-text("Send Me a Push")',
        'button:has-text("Send me a Push")',
        'button:has-text("Send a Push")',
        '[role=button]:has-text("Send Me a Push")',
    ]
    for _ in range(15):
        for fr in page.frames:                      # includes the main frame + Duo iframe
            for sel in push_selectors:
                try:
                    el = fr.locator(sel).first
                    if await el.count() and await el.is_visible():
                        await el.click(timeout=2000)
                        print("  ✓ Auto-clicked 'Send Me a Push' — push sent")
                        return True
                except Exception:
                    pass
        await asyncio.sleep(2)
    return False


async def do_login(page, username: str, password: str):
    await asyncio.sleep(2)

    # Step 0: account-type chooser page. The portal may first show a landing page
    # with "Current Students and Alumni" vs "Local Login". Click the student SSO
    # option to reach the actual username/password form.
    for sel in ['a:has-text("Current Students and Alumni")',
                'button:has-text("Current Students and Alumni")',
                ':text("Current Students and Alumni")',
                'a:has-text("Current Students")',
                'button:has-text("Current Students")']:
        try:
            await page.click(sel, timeout=2500)
            print("  ✓ Clicked 'Current Students and Alumni' chooser")
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            break
        except Exception:
            pass

    for sel in ['input[id="username"]', 'input[name="username"]',
                'input[type="email"]', 'input[name="USER"]']:
        try:
            await page.fill(sel, username, timeout=3000)
            break
        except Exception:
            pass

    for sel in ['input[type="password"]', 'input[id="password"]',
                'input[name="password"]', 'input[name="PASSWORD"]']:
        try:
            await page.fill(sel, password, timeout=3000)
            break
        except Exception:
            pass

    for sel in ['button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Login")', 'button:has-text("Sign in")']:
        try:
            await page.click(sel, timeout=3000)
            break
        except Exception:
            pass

    # Auto-send the DUO push (this config requires clicking "Send Me a Push").
    await asyncio.sleep(3)  # let the DUO prompt render
    pushed = await trigger_duo_push(page)

    print("\n" + "=" * 55)
    if pushed:
        print("  DUO push SENT automatically — just APPROVE it on your phone.")
    else:
        print("  ACTION REQUIRED: Approve DUO on your phone (tap 'Send Me a Push' if needed).")
    print(f"  Waiting up to {DUO_WAIT_SECONDS // 60} minutes...")
    print("=" * 55 + "\n")
    notify_telegram(
        ("🔐 DUO push sent automatically — approve it on your phone within "
         if pushed else
         "🔐 NUworks needs DUO approval (tap 'Send Me a Push', then approve) within ")
        + f"{DUO_WAIT_SECONDS // 60} minutes, or this run is skipped.")

    deadline = asyncio.get_event_loop().time() + DUO_WAIT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        try:
            if "northeastern-csm.symplicity.com" in page.url and not looks_like_login(page.url):
                break
        except Exception:
            pass
        await asyncio.sleep(2)
    else:
        print(f"  ⚠  Timed out waiting for DUO ({DUO_WAIT_SECONDS // 60} min).")
        # Exit code 3 = "DUO not approved in time" specifically (vs other login
        # failures). pipeline.py treats this code as a prompt to ask the user, via
        # Telegram, whether to resend the push and rerun now or skip for today — so
        # we DON'T send a "run skipped" message here (the pipeline owns that UX).
        sys.exit(3)

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass
    print("  ✓ Login + DUO complete!\n")


async def is_authenticated(page) -> bool:
    """Probe the auth API — a stale cookie can load the page shell without
    redirecting to login, so a URL check alone gives false positives."""
    try:
        return bool(await page.evaluate("""async () => {
            try {
                const r = await fetch('/api/v2/auth/current-user',
                    {credentials:'include', headers:{Accept:'application/json'}});
                if (!r.ok) return false;
                const b = await r.json();
                return !!(b && (b.login || b.username));
            } catch (e) { return false; }
        }"""))
    except Exception:
        return False


async def ensure_logged_in(page, context, username: str, password: str):
    await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeout:
        pass

    authed = (not looks_like_login(page.url)) and await is_authenticated(page)
    if authed:
        print("  ✓ Saved session still valid — no DUO needed!\n")
        return

    print("  Session missing or expired — logging in...")
    # Clear the stale cookies so the portal cleanly redirects to the SSO login.
    await context.clear_cookies()
    await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeout:
        pass
    await do_login(page, username, password)
    await context.storage_state(path=str(SESSION_FILE))
    # session.json holds live NUworks auth cookies — keep it owner-only (600),
    # not the default world-readable 644.
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass
    print(f"  ✓ Session saved → {SESSION_FILE}\n")


# ── API scraping ──────────────────────────────────────────────────────────────
# IMPORTANT: this jobs API only returns real results to the React app's OWN
# request. A bare replayed fetch — even authenticated and on-origin — gets an
# empty 200 (total=0), because the result set is tied to request context the app
# establishes (a /api/v2/jobs/filters/students call + XHR headers). So the only
# reliable path is to navigate to the FILTERED search page and INTERCEPT the
# response the app fires for itself.
#
# Hardening over the original (which silently returned [] on a miss — the
# 2026-06-25 empty scrape): we match OUR filtered call via its job_type signature
# (so we never grab the app's unfiltered default 1400-job call), retry the
# navigation on a miss, and RAISE ScrapeError when nothing is ever captured. That
# last point is the key fix — a capture miss (mechanism failure) is now distinct
# from a genuinely empty result, so the pipeline retries instead of marking the
# day done off a stale jobs.csv.

async def intercept_jobs_page(page, postdate: int, pg: int, per_page: int = 100,
                              attempts: int = 3, timeout: int = 30) -> dict | None:
    """Navigate to the filtered search page and capture the jobs API response the
    React app fires. Returns the parsed body (even when it legitimately holds zero
    jobs), or None if no matching response was captured after `attempts` tries."""
    for attempt in range(1, attempts + 1):
        data: dict = {}
        got = asyncio.Event()

        async def capture(response):
            url = response.url
            if ("/api/v2/jobs?" in url and "json_mode=read_only" in url
                    and "job_type=5,17" in url and response.status == 200
                    and (pg == 1 or f"page={pg}" in url)):
                try:
                    body = await response.json()
                    if isinstance(body, dict) and "models" in body:
                        data.update(body)
                        got.set()
                except Exception:
                    pass

        page.on("response", capture)
        try:
            await page.goto(search_url(postdate, pg, per_page),
                            wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass
        try:
            await asyncio.wait_for(got.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        page.remove_listener("response", capture)
        if data:
            return data
        if attempt < attempts:
            print(f"  ⚠  No jobs response captured (attempt {attempt}/{attempts}) — retrying...")
            await asyncio.sleep(3)
    return None


async def scrape_all_jobs(page, postdate: int = 7) -> list[dict]:
    """Fetch every page of the job-search results by intercepting the app's own
    API calls. Raises ScrapeError if the data call is never captured (so the
    caller exits non-zero and the pipeline retries rather than keeping a stale
    jobs.csv). Returns [] ONLY when the fetch genuinely succeeds with zero jobs."""
    print(f"  Fetching jobs (window: past {postdate} day(s))...")
    per_page = 100

    first = await intercept_jobs_page(page, postdate, 1, per_page)
    if not first:
        raise ScrapeError(
            "could not capture the jobs API response after retries — the search "
            "page never fired its data call (slow render / redirect / session "
            "issue), NOT a confirmed-empty board")

    total    = first.get("total", 0)
    per_page = first.get("perPage", per_page)
    pages    = max(1, -(-total // per_page))
    all_jobs = list(first.get("models", []))
    print(f"  Found {total} job(s) across {pages} page(s).")

    # --- subsequent pages ---------------------------------------------------
    for pg in range(2, pages + 1):
        data = await intercept_jobs_page(page, postdate, pg, per_page)
        if data:
            all_jobs.extend(data.get("models", []))
        print(f"  Page {pg}/{pages}: {len(all_jobs)} total so far...")

    return all_jobs


# ── HTML → clean plain text ───────────────────────────────────────────────────

def html_to_text(raw: str) -> str:
    """Convert the job_desc HTML blob into readable plain text."""
    if not raw:
        return ""
    text = raw
    # Turn block-level tags and <br> into newlines so structure survives
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "• ", text)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities (&nbsp; &amp; &ndash; etc.)
    text = html.unescape(text)
    # Collapse excess whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ── Flatten API job object → CSV row ─────────────────────────────────────────

def flatten(job: dict) -> dict:
    def g(*keys):
        for k in keys:
            v = job.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    # Salary range
    lo = g("compensation_from")
    hi = g("compensation_to")
    freq = g("compensation_frequency")
    salary = f"{lo}–{hi} {freq}".strip("– ") if (lo or hi) else ""

    # Job type label
    jt = job.get("job_type")
    if isinstance(jt, list):
        jt = ", ".join(str(x) for x in jt)

    # External apply links: pull href URLs out of the raw HTML job_desc (these
    # get dropped by html_to_text, but they're how postings say "apply online at...").
    raw_html = job.get("job_desc", "") or ""
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', raw_html)
    apply_urls = ";".join(dict.fromkeys(
        u for u in hrefs if u.startswith("http") and "symplicity.com" not in u))

    return {
        "job_id":      g("job_id"),
        "title":       g("job_title", "name"),
        "company":     g("name"),
        "location":    g("job_location"),
        "type":        str(jt or ""),
        "posted":      g("postdate"),
        "deadline":    g("deadline"),
        "salary":      salary,
        "remote":      g("symp_remote_onsite"),
        "qualified":   g("qualified"),
        "apply_urls":  apply_urls,
        "description": html_to_text(job.get("job_desc", "")),
    }


# ── Save CSV ──────────────────────────────────────────────────────────────────

def save_csv(jobs: list[dict]):
    """Write jobs.csv. ALWAYS writes a header — even for zero jobs — so a
    genuinely empty (but successful) scrape clears stale rows from a prior run
    instead of leaving them to be reprocessed. A failed fetch never reaches here
    (it raises ScrapeError), so the old CSV is preserved in that case."""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
    if jobs:
        print(f"\n  ✓ {len(jobs)} jobs saved → {OUTPUT_FILE}")
    else:
        print(f"\n  ✓ Scrape OK — 0 new jobs in the window; wrote empty {OUTPUT_FILE}.")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_window(argv) -> int:
    """Look-back window in days. Default 7 (past week); override with
    --postdate N / --window-days N / --days N for a broader sweep."""
    for flag in ("--postdate", "--window-days", "--days"):
        if flag in argv:
            try:
                return max(1, int(argv[argv.index(flag) + 1]))
            except (ValueError, IndexError):
                pass
    return 7


async def main(postdate: int = 7) -> int:
    username, password = load_credentials()

    print("  Launching browser...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )

        ctx_opts = {}
        if SESSION_FILE.exists():
            print(f"  Loading saved session from {SESSION_FILE}...")
            ctx_opts["storage_state"] = str(SESSION_FILE)

        context = await browser.new_context(
            **ctx_opts,
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        await ensure_logged_in(page, context, username, password)

        try:
            raw_jobs = await scrape_all_jobs(page, postdate)
        except ScrapeError as e:
            print(f"\n  ✗ Scrape failed: {e}")
            notify_telegram("⚠️ NUworks scrape couldn't reach the jobs API "
                            "(session/network). Day left unmarked — it'll retry "
                            "on next login.")
            await browser.close()
            return 4  # non-zero → pipeline leaves the day unmarked and retries

        jobs = [flatten(j) for j in raw_jobs]
        save_csv(jobs)

        # Print a quick preview
        if jobs:
            print("\n  Preview (first 5):")
            print(f"  {'Title':<45} {'Company':<25} {'Location'}")
            print("  " + "-" * 85)
            for j in jobs[:5]:
                print(f"  {j['title'][:44]:<45} {j['company'][:24]:<25} {j['location'][:30]}")

        await browser.close()
        print("\n  Done.\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_window(sys.argv[1:]))))
