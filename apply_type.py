#!/usr/bin/env python3
"""
Classify a job's application method so the pipeline knows whether it can
auto-apply. Fetches the per-job v3 detail via the authenticated session.

apply_type ->
  in_portal      : resume_mode == 'online'  -> our auto-apply module handles it
  email          : email the resume to resume_email
  external       : apply at student_link
  already_applied: applied == True
  unknown        : anything else (flag for manual handling)
"""
import sys
import csv
import json
import asyncio
import job_scraper
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Error as PWError, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
SESSION = str(HERE / "session.json")
BASE = "https://northeastern-csm.symplicity.com"
PORTAL = f"{BASE}/students/app/jobs/search"


def classify(detail: dict) -> dict:
    """resume_mode is a comma-separated list of accepted methods. Prefer
    in-portal whenever 'online' is offered, since that's what we can automate."""
    if detail.get("applied"):
        return {"type": "already_applied", "target": None}
    modes = {m.strip().lower() for m in (detail.get("resume_mode") or "").split(",") if m.strip()}
    email = detail.get("resume_email")
    link = detail.get("student_link")
    if "online" in modes or "other" in modes:
        # TEMP exception: 'other' is unverified (NUworks doesn't document it) — route
        # it through in-portal like 'online' so it at least reaches a posting card
        # instead of vanishing silently. If the Apply flow doesn't actually accept a
        # resume upload for these, the apply step itself will surface that, not a
        # silent drop. Revisit once we've seen a few of these go through.
        return {"type": "in_portal", "target": None}
    if "email" in modes or email:
        return {"type": "email", "target": email}
    if modes & {"offsite", "website", "url"} or link:
        return {"type": "external", "target": link}
    return {"type": "unknown", "target": f"resume_mode={sorted(modes)!r}"}


async def fetch_detail(page, job_id: str) -> dict:
    """{} when the detail can't be read — incl. an expired session, where the portal
    bounces to the SSO/careers origin and a cross-origin fetch would throw."""
    if urlparse(page.url).hostname != urlparse(BASE).hostname:
        return {}
    url = f"{BASE}/api/v3/jobs/{job_id}?json_mode=read_only&enable_translation=false"
    try:
        return await page.evaluate(f"""async () => {{
            const r = await fetch("{url}", {{credentials:"include", headers:{{Accept:"application/json"}}}});
            return r.ok ? await r.json() : null;
        }}""") or {}
    except PWError:
        return {}


async def classify_jobs(job_ids: list[str]) -> list[dict]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=SESSION)
        page = await ctx.new_page()
        await page.goto(PORTAL, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        out = []
        for jid in job_ids:
            d = await fetch_detail(page, jid)
            c = classify(d)
            out.append({
                "job_id": jid,
                "title": d.get("job_title", "?"),
                "resume_mode": d.get("resume_mode"),
                "type": c["type"],
                "target": c["target"],
                "required_docs": d.get("documents_required") or [],
            })
        await browser.close()
        return out


async def _fetch_job(job_id: str) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            try:
                ctx = await browser.new_context(storage_state=SESSION)
            except (OSError, ValueError, PWError):  # session.json missing/corrupt: same as expired
                return {}
            page = await ctx.new_page()
            await page.goto(PORTAL, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass
            return await fetch_detail(page, job_id)
        finally:
            await browser.close()


def fetch_job(job_id: str) -> dict | None:
    """One posting as a jobs.csv-shaped row plus _apply_type/_apply_target/
    _required_docs, or None if the detail came back empty (expired session / bad id)."""
    d = asyncio.run(_fetch_job(job_id))
    if not d:
        return None
    c = classify(d)
    row = job_scraper.flatten(d)
    row["posted"] = row["posted"][:10]  # v3 `created` is ISO; the CSV/list API shows a date
    return {**row, "job_id": job_id, "_apply_type": c["type"],
            "_apply_target": c["target"], "_required_docs": d.get("documents_required") or []}


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "data" / "jobs.csv")
    rows = list(csv.DictReader(open(csv_path)))
    ids = [r["job_id"] for r in rows]
    results = asyncio.run(classify_jobs(ids))
    print(f"{'TYPE':<16}{'MODE':<10}TITLE")
    print("-" * 70)
    for r in results:
        print(f"{r['type']:<16}{str(r['resume_mode']):<10}{r['title'][:40]}")
        if r["target"]:
            print(f"{'':<26}target: {r['target']}")


if __name__ == "__main__":
    main()
