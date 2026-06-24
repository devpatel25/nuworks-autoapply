#!/usr/bin/env python3
"""
Manage NUworks uploaded documents (resumes).

NUworks caps how many documents you can store ("Maximum Documents Reached"),
which blocks new resume uploads. This module lists stored resumes and deletes
the oldest one to free a slot. The transcript and non-resume docs are never
touched.

CLI:
  python3 manage_docs.py list     # list all docs with parsed dates
  python3 manage_docs.py delete   # delete the oldest RESUME (live)
"""
import re
import sys
import asyncio
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

SESSION = str(Path(__file__).resolve().parent / "session.json")
DOC_URL = ("https://northeastern-csm.symplicity.com/students/index.php"
           "?mode=list&s=resume&ss=resumes")
_DATE_FMTS = ("%b %d, %Y, %I:%M %p", "%B %d, %Y, %I:%M %p", "%b %d, %Y", "%m/%d/%Y")


def _parse_date(text: str):
    m = re.search(r'last modified on\s+([^\n]+)', text, re.I)
    if not m:
        return None
    ds = m.group(1).strip().rstrip('.')
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(ds, fmt)
        except ValueError:
            continue
    return None


async def list_documents(page) -> list[dict]:
    """Return [{del_index, name, type, date, raw}] for each document card."""
    await page.goto(DOC_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    await asyncio.sleep(2)
    cards = await page.evaluate(r"""() => {
        const dels = [...document.querySelectorAll('a')]
            .filter(a => /^\s*delete\s*$/i.test(a.innerText || ''));
        return dels.map((a, i) => {
            let el = a;
            for (let k = 0; k < 10 && el; k++) {
                el = el.parentElement;
                if (el && /last modified/i.test(el.innerText || '')) break;
            }
            return { del_index: i, raw: el ? el.innerText.trim() : '' };
        });
    }""")
    docs = []
    for c in cards:
        lines = [l.strip() for l in c["raw"].split("\n") if l.strip()]
        m = re.search(r'last modified on\s+([^\n]+)', c["raw"], re.I)
        docs.append({
            "del_index": c["del_index"],
            "name": lines[0] if lines else "?",
            "type": lines[1] if len(lines) > 1 else "",
            "date": _parse_date(c["raw"]),
            "date_str": m.group(1).strip().rstrip(".") if m else "",
        })
    return docs


def _oldest_resume(docs: list[dict]) -> dict | None:
    resumes = [d for d in docs if "resume" in d["type"].lower() and d["date"]]
    return min(resumes, key=lambda d: d["date"]) if resumes else None


async def delete_oldest_resume(page, confirm: bool = True) -> dict:
    """Delete the oldest resume. confirm=False does a dry run (no deletion)."""
    docs = await list_documents(page)
    target = _oldest_resume(docs)
    if not target:
        return {"ok": False, "detail": "No datable resume found to delete.",
                "count": len(docs)}
    if not confirm:
        return {"ok": True, "dry": True, "target": target["name"],
                "date": str(target["date"]), "count": len(docs)}

    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))  # native confirm()

    # Click the VISIBLE Delete link whose card matches the oldest by name + date
    # (rows are duplicated for responsive layouts; one set is hidden).
    del_links = page.locator("a", has_text=re.compile(r"^\s*Delete\s*$", re.I))
    n = await del_links.count()

    async def card_text_of(link):
        return await link.evaluate(
            "el => { let e=el; for (let k=0;k<10&&e;k++){ e=e.parentElement;"
            " if (e && /last modified/i.test(e.innerText)) return e.innerText; } return ''; }")

    clicked = False
    # Pass 1: name + exact date. Pass 2: name only. Try clicking each match;
    # skip the hidden duplicate rows (their click raises, so move on).
    for strict in (True, False):
        for i in range(n):
            link = del_links.nth(i)
            try:
                ct = await card_text_of(link)
                ok = (target["name"] in ct and target["date_str"] in ct) if strict else (target["name"] in ct)
                if not ok:
                    continue
                await link.scroll_into_view_if_needed(timeout=2000)
                await link.click(timeout=4000)
                clicked = True
                break
            except Exception:
                continue
        if clicked:
            break
    if not clicked:
        return {"ok": False, "detail": f"Could not click Delete for {target['name']}."}

    await asyncio.sleep(1.5)
    # Handle a modal confirmation if one appears instead of a native dialog.
    for sel in ['button:has-text("Delete")', 'button:has-text("Yes")',
                'button:has-text("Confirm")', 'button:has-text("OK")',
                'a:has-text("Yes")']:
        try:
            el = page.locator(sel).last
            if await el.count() and await el.is_visible():
                await el.click(timeout=2000)
                break
        except Exception:
            pass
    await asyncio.sleep(3)
    after = await list_documents(page)
    names_after = [d["name"] for d in after]
    gone = len(after) < len(docs)
    return {"ok": gone, "deleted": target["name"], "date": str(target["date"]),
            "before_count": len(docs) // 2, "after_count": len(after) // 2}


async def _run(action: str):
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        page = await (await b.new_context(storage_state=SESSION,
                                          viewport={"width": 1400, "height": 900})).new_page()
        if action == "delete":
            res = await delete_oldest_resume(page, confirm=True)
        else:
            docs = await list_documents(page)
            docs_sorted = sorted([d for d in docs if d["date"]], key=lambda d: d["date"])
            print(f"{len(docs)} documents:")
            for d in docs_sorted:
                print(f"  {str(d['date']):20} | {d['type'][:18]:18} | {d['name']}")
            t = _oldest_resume(docs)
            print(f"\nOldest resume → {t['name']} ({t['date']})" if t else "no resume")
            res = {"listed": len(docs)}
        await b.close()
        return res


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    print(asyncio.run(_run(action)))
