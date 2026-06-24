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

# API query params — mirrors the URL the user provided; postdate=1 = past 24 h
# Change postdate to 7 (week) or 30 (month) for broader results
API_PARAMS = (
    "perPage=100&sort=!postdate&ocr=f"
    "&job_type=5,17"
    "&postdate=1"
    "&targeted_academic_majors=0160"
    "&exclude_applied_jobs=1"
    "&json_mode=read_only&enable_translation=false"
)


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


# ── API scraping: intercept the React app's own authenticated calls ────────────

async def scrape_all_jobs(page) -> list[dict]:
    """
    Navigate to each page of the job search URL and intercept the API response
    that the React app fires automatically — this carries all auth headers the
    app adds and is guaranteed to work.
    """
    print("  Fetching jobs by intercepting React app API calls...")

    all_jobs: list[dict] = []
    per_page = 100

    # --- page 1: navigate and grab first response ---------------------------
    first_data: dict = {}
    got_first = asyncio.Event()

    async def capture_first(response):
        if "/api/v2/jobs?" in response.url and "json_mode=read_only" in response.url and response.status == 200:
            try:
                body = await response.json()
                if isinstance(body, dict) and "models" in body:
                    first_data.update(body)
                    got_first.set()
            except Exception:
                pass

    page.on("response", capture_first)
    await page.goto(
        f"{PORTAL_URL}?perPage={per_page}&page=1&sort=!postdate&ocr=f"
        "&job_type=5,17&postdate=1&targeted_academic_majors=0160&exclude_applied_jobs=1",
        wait_until="domcontentloaded",
        timeout=30_000,
    )
    try:
        await asyncio.wait_for(got_first.wait(), timeout=15)
    except asyncio.TimeoutError:
        pass
    page.remove_listener("response", capture_first)

    if not first_data:
        print("  ⚠  API returned nothing — session may have expired.")
        return []

    total    = first_data.get("total", 0)
    per_page = first_data.get("perPage", per_page)
    pages    = max(1, -(-total // per_page))
    all_jobs.extend(first_data.get("models", []))
    print(f"  Found {total} jobs across {pages} page(s).")

    # --- subsequent pages ---------------------------------------------------
    for pg in range(2, pages + 1):
        page_data: dict = {}
        got_page = asyncio.Event()

        async def capture_page(response, _pg=pg):
            if (f"page={_pg}" in response.url and "/api/v2/jobs?" in response.url
                    and "json_mode=read_only" in response.url and response.status == 200):
                try:
                    body = await response.json()
                    if isinstance(body, dict) and "models" in body:
                        page_data.update(body)
                        got_page.set()
                except Exception:
                    pass

        page.on("response", capture_page)
        await page.goto(
            f"{PORTAL_URL}?perPage={per_page}&page={pg}&sort=!postdate&ocr=f"
            "&job_type=5,17&postdate=1&targeted_academic_majors=0160&exclude_applied_jobs=1",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        try:
            await asyncio.wait_for(got_page.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass
        page.remove_listener("response", capture_page)

        all_jobs.extend(page_data.get("models", []))
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
    if not jobs:
        print("\n  ⚠  No jobs to save.")
        return
    fieldnames = list(jobs[0].keys())
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
    print(f"\n  ✓ {len(jobs)} jobs saved → {OUTPUT_FILE}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
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

        raw_jobs = await scrape_all_jobs(page)
        jobs     = [flatten(j) for j in raw_jobs]
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


if __name__ == "__main__":
    asyncio.run(main())
