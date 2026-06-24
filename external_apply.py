#!/usr/bin/env python3
"""
External job-application handling.

Some NUworks postings also say "apply online at <url>" (e.g. a Lever/Greenhouse
page). This module:
  1. extract_apply_urls()  — pulls apply URLs out of a job description
  2. detect_ats()          — identifies the applicant-tracking system
  3. track()               — records external apps in data/external_applications.json
  4. apply_lever()         — auto-fills + (optionally) submits a Lever form

Personal answers come from data/profile.json. Sensitive/unknown required answers
(e.g. visa sponsorship when null) are NOT guessed — the form is filled as far as
possible, screenshotted, and left for the user (mode="dry" never submits).
"""
import re
import json
import asyncio
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
PROFILE = json.loads((HERE / "data" / "profile.json").read_text())
TRACK_FILE = HERE / "data" / "external_applications.json"
SHOTS = HERE / "logs" / "apply_screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)

ATS_DOMAINS = {
    "lever.co": "lever", "greenhouse.io": "greenhouse", "boards.greenhouse": "greenhouse",
    "myworkdayjobs.com": "workday", "ashbyhq.com": "ashby", "icims.com": "icims",
    "jobvite.com": "jobvite", "smartrecruiters.com": "smartrecruiters",
    "workable.com": "workable", "bamboohr.com": "bamboohr", "rippling.com": "rippling",
    "breezy.hr": "breezy", "applytojob.com": "jazzhr", "taleo.net": "taleo",
}
_URL_RE = re.compile(r'https?://[^\s)>\]"\'}]+', re.I)


# ── URL extraction / classification ─────────────────────────────────────────

def extract_apply_urls(description: str) -> list[str]:
    """Return apply-relevant URLs from a job description (ATS links first)."""
    if not description:
        return []
    urls = [u.rstrip('.,;:)?"\'') for u in _URL_RE.findall(description)]
    # drop obvious non-apply links (company homepages get included only if nothing else)
    ats = [u for u in urls if any(d in u.lower() for d in ATS_DOMAINS)]
    seen, ordered = set(), []
    for u in (ats or urls):
        if u not in seen:
            seen.add(u); ordered.append(u)
    return ordered


def detect_ats(url: str) -> str:
    low = (url or "").lower()
    for d, name in ATS_DOMAINS.items():
        if d in low:
            return name
    return "unknown"


# ── Tracking ────────────────────────────────────────────────────────────────

def _load_track() -> list:
    if TRACK_FILE.exists():
        try:
            return json.loads(TRACK_FILE.read_text())
        except Exception:
            return []
    return []


def track(job: dict, url: str, ats: str, status: str, detail: str = ""):
    """Record/update an external application by (job_id, url)."""
    rows = _load_track()
    # NOTE: external_applications.json is shared with external_autofill.py, which
    # writes records in a DIFFERENT schema (no "job_id" key). Use .get() so a
    # foreign-schema record never KeyErrors here and abort the external-apply step.
    for r in rows:
        if r.get("job_id") == job.get("job_id") and r.get("url") == url:
            r.update(status=status, detail=detail, updated=datetime.now().isoformat(timespec="seconds"))
            break
    else:
        rows.append({
            "job_id": job.get("job_id"), "title": job.get("title", ""),
            "company": job.get("company", ""), "url": url, "ats": ats,
            "status": status, "detail": detail,
            "updated": datetime.now().isoformat(timespec="seconds"),
        })
    TRACK_FILE.write_text(json.dumps(rows, indent=2))


def already_external_applied(job_id: str, url: str) -> bool:
    return any(r.get("job_id") == job_id and r.get("url") == url and r.get("status") == "submitted"
              for r in _load_track())


# ── Lever auto-apply ─────────────────────────────────────────────────────────

def _yesno(question: str) -> str | None:
    """Map a Lever question to a Yes/No answer from the profile. None = unknown."""
    q = question.lower()
    if "eligible to work" in q or "authorized to work" in q:
        return "Yes" if PROFILE.get("work_authorized_us") else "No"
    if "visa sponsorship" in q or "require sponsorship" in q:
        s = PROFILE.get("requires_sponsorship")
        return None if s is None else ("Yes" if s else "No")
    if "referred by" in q or "referral" in q:
        return "Yes" if PROFILE.get("referred") else "No"
    if any(k in q for k in ("office", "hybrid", "in-person", "in person", "days per week", "on-site", "onsite")):
        return "Yes" if PROFILE.get("willing_in_office_hybrid") else "No"
    return None


async def apply_lever(url: str, resume_pdf: str, mode: str = "dry") -> dict:
    """Fill (and optionally submit) a Lever application. mode='dry' fills +
    screenshots + STOPS (never submits). Returns {status, detail, screenshot,
    unanswered}. status: filled | submitted | blocked_unanswered | error."""
    resume_pdf = str(Path(resume_pdf).resolve())
    apply_url = url if url.rstrip("/").endswith("/apply") else url.rstrip("/") + "/apply"
    tag = re.sub(r"[^a-z0-9]", "", url.lower())[-10:]
    result = {"status": "error", "detail": "", "screenshot": "", "unanswered": []}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        page = await (await browser.new_context(viewport={"width": 1400, "height": 1000})).new_page()
        try:
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # Upload résumé first (Lever auto-parses it), then override fields.
            try:
                await page.locator('input[type=file][name="resume"], #resume-upload-input').first.set_input_files(resume_pdf)
                await asyncio.sleep(5)  # parsing
            except Exception as e:
                result.update(detail=f"resume upload failed: {e}")

            async def fill(name_sel, value):
                if not value:
                    return
                try:
                    await page.fill(name_sel, value, timeout=4000)
                except Exception:
                    pass

            await fill('input[name="name"]', PROFILE["name"])
            await fill('input[name="email"]', PROFILE["email"])
            await fill('input[name="phone"]', PROFILE["phone"])
            await fill('input[name="org"]', PROFILE.get("current_company", ""))
            await fill('input[name="urls[LinkedIn]"]', PROFILE.get("linkedin", ""))
            await fill('input[name="urls[GitHub]"]', PROFILE.get("github", ""))

            # Location is an autocomplete; type (fire key events) then pick a suggestion.
            try:
                loc = page.locator('#location-input, input[name="location"]').first
                await loc.click(timeout=4000)
                await loc.fill("")
                await loc.type(PROFILE["location"], delay=80)
                await asyncio.sleep(2.5)
                picked = False
                for sel in ['.dropdown-location .dropdown-item', '.pac-item',
                            '[class*="dropdown"] [class*="item"]', 'ul[role=listbox] li']:
                    try:
                        await page.locator(sel).first.click(timeout=2000)
                        picked = True
                        break
                    except Exception:
                        pass
                if not picked:
                    await loc.press("ArrowDown")
                    await asyncio.sleep(0.3)
                    await loc.press("Enter")
            except Exception:
                pass

            # Answer Yes/No custom questions.
            unanswered = []
            cards = page.locator("li.application-question, .application-question")
            for i in range(await cards.count()):
                card = cards.nth(i)
                try:
                    qtext = (await card.inner_text()).strip()
                except Exception:
                    continue
                radios = card.locator('input[type=radio]')
                if await radios.count() == 0:
                    # required free-text (e.g. referral name) — fill N/A if not referred
                    ta = card.locator("textarea, input[type=text]")
                    if await ta.count() and "referr" in qtext.lower() and not PROFILE.get("referred"):
                        try:
                            await ta.first.fill("N/A")
                        except Exception:
                            pass
                    continue
                ans = _yesno(qtext)
                required = "✱" in qtext or "*" in qtext
                if ans is None:
                    if required:
                        unanswered.append(qtext.split("\n")[0][:90])
                    continue
                try:
                    await card.get_by_text(ans, exact=True).first.click(timeout=3000)
                except Exception:
                    try:
                        await radios.nth(0 if ans == "Yes" else 1).check(timeout=3000)
                    except Exception:
                        pass

            shot = str(SHOTS / f"lever_{tag}_{mode}.png")
            await page.screenshot(path=shot, full_page=True)
            result["screenshot"] = shot
            result["unanswered"] = unanswered

            if unanswered:
                result.update(status="blocked_unanswered",
                              detail="Required questions need answers before submit: " + "; ".join(unanswered))
                await browser.close()
                return result

            if mode == "dry":
                result.update(status="filled", detail="Form filled; STOPPED before submit (dry run).")
                await browser.close()
                return result

            CONF = ("thank you", "application submitted", "received your application",
                    "successfully", "we'll be in touch", "thanks for applying")

            if mode == "prepare":
                # Everything is filled and the browser is open on the user's screen.
                # They solve the CAPTCHA and click Submit; we watch for confirmation.
                # (We do NOT auto-solve captchas — that's anti-bot evasion.)
                deadline = asyncio.get_event_loop().time() + 300
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        body = (await page.inner_text("body")).lower()
                        if any(k in body for k in CONF):
                            shot2 = str(SHOTS / f"lever_{tag}_result.png")
                            await page.screenshot(path=shot2, full_page=True)
                            result.update(status="submitted", screenshot=shot2,
                                          detail="You completed the captcha + submit; confirmation detected.")
                            await browser.close()
                            return result
                    except Exception:
                        pass
                    await asyncio.sleep(4)
                result.update(status="prepared_timeout",
                              detail="Form was filled and left open, but no submission detected within 5 min.")
                await browser.close()
                return result

            # mode == "submit": will hit the captcha (kept for ATSes without one).
            await page.get_by_role("button", name=re.compile("submit application", re.I)).first.click(timeout=8000)
            await asyncio.sleep(5)
            body = (await page.inner_text("body")).lower()
            ok = any(k in body for k in CONF)
            shot2 = str(SHOTS / f"lever_{tag}_result.png")
            await page.screenshot(path=shot2, full_page=True)
            result["screenshot"] = shot2
            result.update(status="submitted" if ok else "error",
                          detail="Submitted; confirmation detected." if ok
                          else "Submit clicked but no confirmation detected (likely captcha) — verify manually.")
            await browser.close()
            return result
        except Exception as e:
            try:
                await page.screenshot(path=str(SHOTS / f"lever_{tag}_error.png"))
            except Exception:
                pass
            result.update(status="error", detail=f"{type(e).__name__}: {e}")
            await browser.close()
            return result


async def apply_external(url: str, resume_pdf: str, mode: str = "dry") -> dict:
    ats = detect_ats(url)
    if ats == "lever":
        return await apply_lever(url, resume_pdf, mode=mode)
    return {"status": "unsupported_ats", "detail": f"No automation for ATS '{ats}' yet — apply manually.",
            "screenshot": "", "unanswered": []}


if __name__ == "__main__":
    import sys
    # On-demand: pre-fill an external application form, then YOU solve the captcha
    # and click submit in the browser window it opens.
    #   python3 external_apply.py <apply_url> <resume_pdf>
    if len(sys.argv) >= 3:
        url, resume = sys.argv[1], sys.argv[2]
        print(f"Opening + filling {detect_ats(url)} form: {url}")
        print("→ Solve the captcha and click SUBMIT in the browser (5-min window).")
        r = asyncio.run(apply_external(url, resume, mode="prepare"))
        print("STATUS:", r["status"], "|", r["detail"])
    else:
        desc = "Apply online at https://jobs.lever.co/lendbuzz/07a1f425-fe31-4028-b902-235c4de32a47"
        print("URLs:", extract_apply_urls(desc))
        print("Usage: python3 external_apply.py <apply_url> <resume_pdf>")
