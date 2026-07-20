#!/usr/bin/env python3
"""
NUworks auto-apply module.

apply_to_job() drives the portal's in-portal Apply flow:
  open job -> (skip if already APPLIED) -> click Apply -> "add a new resume"
  -> upload tailored PDF -> set label -> Save -> ensure selected -> Submit -> verify.

SAFETY: mode="dry" (default) stages the resume file + label in the modal,
screenshots the ready-to-submit state, then CANCELS — it never clicks Save or
Submit, so nothing is uploaded and no application is sent. mode="submit"
performs the real submission and is only used post-approval.

CLI (for testing only):
    python3 auto_apply.py <job_id> <resume_pdf> "<resume_label>" [--submit]
"""
import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
SESSION = str(HERE / "session.json")
BASE = "https://northeastern-csm.symplicity.com"
SHOTS = HERE / "logs" / "apply_screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)


def job_url(job_id: str) -> str:
    return (f"{BASE}/students/app/jobs/search?perPage=20&page=1&sort=!postdate"
            f"&ocr=f&job_type=5,17&targeted_academic_majors=0160"
            f"&currentJobId={job_id}")


async def _is_already_applied(modal_scope, page) -> bool:
    """Detail pane shows an APPLIED marker for jobs already applied to."""
    try:
        txt = await page.locator(".list-item-actions, .job-actions, [class*=detail]").first.inner_text(timeout=3000)
        return "APPLIED" in txt.upper()
    except Exception:
        return False


async def _applied_flag(page, job_id: str):
    """Authoritative applied check: read the portal's per-job `applied` flag from
    the v3 detail API (same field apply_type.classify uses). Returns True/False, or
    None if the probe itself failed (don't trust None as 'not applied')."""
    url = f"{BASE}/api/v3/jobs/{job_id}?json_mode=read_only&enable_translation=false"
    try:
        d = await page.evaluate(
            """async (u) => { const r = await fetch(u, {credentials:'include', headers:{Accept:'application/json'}}); return r.ok ? await r.json() : null; }""",
            url)
        if isinstance(d, dict):
            return bool(d.get("applied"))
        return None
    except Exception:
        return None


async def _attach_document(page, link_text: str, file_path: str, label: str):
    """Click an 'Add a new <doc>' link in the apply modal, upload a file, label
    it, and Save. Used for transcript (and reusable for cover letter, etc.)."""
    await page.get_by_text(link_text, exact=False).first.click(timeout=8000)
    modal = page.locator('[role=dialog], .modal').filter(has_text="Add New")
    await modal.last.wait_for(state="visible", timeout=8000)
    await modal.locator('input[type=file]').last.set_input_files(file_path)
    try:
        await modal.locator('#doc-label, input[type=text]').last.fill(label, timeout=3000)
    except Exception:
        pass
    await asyncio.sleep(1)
    await modal.get_by_role("button", name="Save").last.click(timeout=8000)
    await asyncio.sleep(3)


async def _select_existing_resume(page, resume_label) -> bool:
    """If a resume with this exact label already exists in the apply modal's
    resume dropdown, select it instead of uploading a duplicate — this is the
    root-cause fix for the 20-document library cap (every re-tailor used to
    add a new doc). Returns True on match+select, False on any mismatch/error."""
    try:
        sel = page.locator('select[id^="sy_formfield_resume"]').first
        # Options load async after the modal opens; poll until populated (more
        # than the "Select a resume" placeholder + blank) before deciding.
        for _ in range(10):
            options = await sel.locator('option').all_inner_texts()
            if any(o.strip() == resume_label for o in options):
                await sel.select_option(label=resume_label)
                return True
            if len(options) > 2:
                return False  # populated, genuinely no match
            await asyncio.sleep(0.5)
        return False
    except Exception:
        return False


async def _save_resume_with_cap_retry(page, ctx, add_modal, resume_pdf, resume_label,
                                       max_retries: int = 2) -> bool:
    """Save the staged add-resume form, recovering from the NUworks 20-document
    cap. The cap shows up two ways at Save time — a "Maximum Documents Reached"
    dialog OR an inline "reached the limit of documents" error that just disables
    Save — so success is detected structurally (the add form closed after Save),
    not by matching either message. On a cap: free the oldest resume, reopen +
    re-stage a fresh form, and retry. Returns True once the resume actually
    saves, False if still capped after max_retries."""
    for _ in range(max_retries + 1):
        save = add_modal.get_by_role("button", name="Save").first
        # The staged resume uploads asynchronously; Save stays DISABLED until the
        # upload finishes. That's the normal case, NOT the doc cap — so wait for
        # the upload to complete before deciding. Misreading "Save still disabled"
        # as the cap made the retry delete library resumes for nothing and re-stage
        # into the same race, over and over (2026-07-20 Tesla run).
        for _ in range(60):  # ~30s; real uploads finish in seconds, cap never enables
            try:
                if await save.is_enabled():
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        try:
            if await save.is_enabled():
                await save.click(timeout=8000)
        except Exception:
            pass

        # Success = the resume now appears in the apply modal's dropdown (it was
        # actually saved to the library). This is a positive goal check, robust to
        # both cap UIs and to hidden "Add New Resume" DOM templates. Poll briefly
        # since the option list refreshes async after Save.
        for _ in range(8):
            try:
                opts = await page.locator('select[id^="sy_formfield_resume"]').first.locator('option').all_inner_texts()
                if any(o.strip() == resume_label for o in opts):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)

        import manage_docs
        # Dropdown didn't show it — confirm against the library (source of truth)
        # before deleting anything, so a stale client view never triggers
        # unnecessary over-deletion of resumes.
        at_cap = False  # only delete a resume when the library is positively full
        try:
            lp = await ctx.new_page()
            try:
                docs = await manage_docs.list_documents(lp)
            finally:
                await lp.close()
            if any(d["name"] == resume_label for d in docs):
                return True
            # list_documents returns each card twice (responsive layout), so the
            # real count is len//2. NUworks caps the library at 20.
            at_cap = (len(docs) // 2) >= 20  # ponytail: hard-coded cap; bump if NUworks changes it
        except Exception:
            pass

        # Still open -> hit the cap (either UI). Dismiss it (Close = dialog,
        # add-form Cancel = inline error; scoped so we don't close the whole
        # apply modal), free a slot, then reopen + re-stage for the next attempt.
        try:
            await page.get_by_role("button", name="Close").first.click(timeout=2500)
        except Exception:
            pass
        try:
            await add_modal.get_by_role("button", name="Cancel").first.click(timeout=2500)
        except Exception:
            pass

        if at_cap:
            mp = await ctx.new_page()
            try:
                await manage_docs.delete_oldest_resume(mp)
            except Exception:
                pass
            finally:
                await mp.close()

        try:
            # scope to the visible apply modal — hidden duplicate "add a new
            # resume" nodes exist in the DOM and an unscoped .first can grab one.
            apply_modal = page.locator('[role=dialog], .modal').filter(has_text="Submit Your Application")
            await apply_modal.get_by_text("add a new resume", exact=False).first.click(timeout=8000)
            await add_modal.first.wait_for(state="visible", timeout=6000)
            await page.locator('input[type=file]').first.set_input_files(resume_pdf)
            await page.locator('#doc-label').fill(resume_label)
            await asyncio.sleep(1)
        except Exception:
            return False

    return False


async def apply_to_job(job_id: str, resume_pdf: str, resume_label: str,
                       mode: str = "dry",
                       transcript_pdf: str = None,
                       transcript_label: str = "Transcript",
                       cover_letter_pdf: str = None,
                       cover_letter_label: str = "Cover Letter",
                       extra_pdf: str = None,
                       extra_label: str = "Supporting Document") -> dict:
    """Returns {status, detail, screenshot}. status in {staged, applied,
    already_applied, submitted_unverified, no_inportal_apply,
    blocked_missing_documents, error}.
    submitted_unverified = Submit was clicked but confirmation couldn't be verified
    (page text or portal applied-flag); caller should NOT blindly resubmit."""
    resume_pdf = str(Path(resume_pdf).resolve())
    if transcript_pdf:
        transcript_pdf = str(Path(transcript_pdf).resolve())
        if not Path(transcript_pdf).exists():
            transcript_pdf = None
    if cover_letter_pdf:
        cover_letter_pdf = str(Path(cover_letter_pdf).resolve())
        if not Path(cover_letter_pdf).exists():
            cover_letter_pdf = None
    if extra_pdf:
        extra_pdf = str(Path(extra_pdf).resolve())
        if not Path(extra_pdf).exists():
            extra_pdf = None
    if not Path(resume_pdf).exists():
        return {"status": "error", "detail": f"resume not found: {resume_pdf}"}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = await browser.new_context(storage_state=SESSION,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        result = {"status": "error", "detail": "", "screenshot": ""}

        try:
            await page.goto(job_url(job_id), wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except PWTimeout:
                pass
            await asyncio.sleep(3)

            # Already applied? Check the authoritative v3 flag first (robust against
            # the fragile text probe), then fall back to the APPLIED marker. This is
            # the real double-apply guard — e.g. when a prior run's submit succeeded
            # but its confirmation text wasn't detected (status submitted_unverified).
            if await _applied_flag(page, job_id) is True or await _is_already_applied(None, page):
                result.update(status="already_applied", detail="Job already shows applied (portal flag/marker).")
                await browser.close()
                return result

            # Click the visible Apply button
            apply_btn = page.locator("button.btn_primary", has_text="Apply").locator("visible=true").first
            try:
                await apply_btn.click(timeout=12000)
            except PWTimeout:
                result.update(status="no_inportal_apply",
                              detail="No clickable in-portal Apply button (likely email/external apply).")
                await browser.close()
                return result

            # Wait for the apply modal
            apply_modal = page.locator('[role=dialog], .modal').filter(
                has_text="Submit Your Application")
            try:
                await apply_modal.first.wait_for(state="visible", timeout=10000)
            except PWTimeout:
                # Some jobs open an external apply or a different dialog
                result.update(status="no_inportal_apply",
                              detail="Apply did not open the in-portal application modal.")
                await page.screenshot(path=str(SHOTS / f"apply_{job_id[:8]}_unexpected.png"))
                await browser.close()
                return result

            # Reuse an existing library resume by label if one's already there —
            # root-cause fix for the 20-doc cap (every re-tailor used to upload a
            # fresh duplicate). Falls back to the upload flow if no match.
            reused = await _select_existing_resume(page, resume_label)

            # Open "add a new resume" sub-form. If it won't open (NUworks document
            # limit / "Maximum Documents Reached"), delete the oldest resume to free
            # a slot and retry.
            add_modal = page.locator('[role=dialog], .modal').filter(has_text="Add New Resume")

            if not reused:
                async def open_add_resume_form() -> bool:
                    try:
                        await page.get_by_text("add a new resume", exact=False).first.click(timeout=8000)
                        await add_modal.first.wait_for(state="visible", timeout=6000)
                        return True
                    except Exception:
                        return False

                if not await open_add_resume_form():
                    import manage_docs
                    mp = await ctx.new_page()
                    try:
                        freed = await manage_docs.delete_oldest_resume(mp)
                    except Exception as e:
                        freed = {"ok": False, "detail": str(e)}
                    finally:
                        await mp.close()
                    # Re-open the application and the add-resume form after freeing a slot.
                    await page.goto(job_url(job_id), wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    try:
                        await page.locator("button.btn_primary", has_text="Apply").locator("visible=true").first.click(timeout=12000)
                        await apply_modal.first.wait_for(state="visible", timeout=10000)
                    except Exception:
                        pass
                    if not await open_add_resume_form():
                        result.update(status="error",
                                      detail=f"Resume upload blocked by document limit; "
                                             f"freed slot ({freed.get('deleted','?')}) but form still wouldn't open.")
                        await browser.close()
                        return result

                # Stage the file + label
                await page.locator('input[type=file]').first.set_input_files(resume_pdf)
                await page.locator('#doc-label').fill(resume_label)
                await asyncio.sleep(1)

            shot = str(SHOTS / f"apply_{job_id[:8]}_ready.png")
            await page.screenshot(path=shot, full_page=True)
            result["screenshot"] = shot

            if mode != "submit":
                # DRY RUN — cancel out, nothing uploaded or submitted. Reused-resume
                # runs never opened add_modal, so cancel the apply modal instead.
                try:
                    if reused:
                        try:
                            await apply_modal.get_by_role("button", name="Cancel").first.click(timeout=5000)
                        except Exception:
                            await page.get_by_role("button", name="Cancel").first.click(timeout=5000)
                    else:
                        await add_modal.get_by_role("button", name="Cancel").first.click(timeout=5000)
                except Exception:
                    pass
                result.update(status="staged",
                              detail=("Reused existing library resume; " if reused else "") +
                                     "Resume staged in upload form; STOPPED before Save/Submit (dry run).")
                await browser.close()
                return result

            # ---- REAL SUBMISSION (post-approval only) ----
            if not reused:
                # Save the uploaded resume to the library, retrying through the
                # doc-cap dialog if the library filled to 20 in the meantime.
                saved = await _save_resume_with_cap_retry(page, ctx, add_modal, resume_pdf, resume_label)
                if not saved:
                    result.update(status="error",
                                  detail="Resume upload blocked by document limit; freed slots "
                                         "but Save still hit the cap.")
                    await page.screenshot(path=str(SHOTS / f"apply_{job_id[:8]}_capfail.png"), full_page=True)
                    await browser.close()
                    return result

                # Ensure the resume is selected in the dropdown
                try:
                    sel = page.locator('select[id^="sy_formfield_resume"]').first
                    await sel.select_option(label=resume_label, timeout=5000)
                except Exception:
                    pass  # newly added resume is typically auto-selected

            # Some jobs require extra documents (Transcript, etc.) — Submit stays
            # disabled until they're attached.
            submit_btn = apply_modal.get_by_role("button", name="Submit").first

            # If required documents keep Submit disabled, attach what we have.
            if not await submit_btn.is_enabled():
                modal_txt = await apply_modal.first.inner_text()
                if "Transcript" in modal_txt and transcript_pdf:
                    try:
                        await _attach_document(page, "Add a new transcript",
                                               transcript_pdf, transcript_label)
                        try:
                            tsel = page.locator('select[id*="transcript"]').first
                            await tsel.select_option(label=transcript_label, timeout=4000)
                        except Exception:
                            pass
                    except Exception:
                        pass
                if "Cover Letter" in modal_txt and cover_letter_pdf:
                    try:
                        await _attach_document(page, "Add a new cover letter",
                                               cover_letter_pdf, cover_letter_label)
                        try:
                            csel = page.locator('select[id*="cover"]').first
                            await csel.select_option(label=cover_letter_label, timeout=4000)
                        except Exception:
                            pass
                    except Exception:
                        pass
                # Generic required "Other" slot (e.g. "Other: See Job Description")
                # whose upload link is just "Add a new document" — attach the
                # user-supplied supporting doc if one was provided.
                if extra_pdf and not await submit_btn.is_enabled():
                    try:
                        await _attach_document(page, "Add a new document",
                                               extra_pdf, extra_label)
                    except Exception:
                        pass

            # Still disabled → report exactly what's missing and bail (no partial submit).
            if not await submit_btn.is_enabled():
                modal_txt = await apply_modal.first.inner_text()
                needs = [w for w in ("Transcript", "Cover Letter", "Work Sample",
                                     "Portfolio", "Writing")
                         if (w + " *") in modal_txt or (w + "*") in modal_txt
                         or (w == "Transcript" and "Add a new transcript" in modal_txt)]
                missing = ", ".join(needs) or "additional required document(s)"
                await page.screenshot(path=str(SHOTS / f"apply_{job_id[:8]}_blocked.png"), full_page=True)
                try:
                    await apply_modal.get_by_role("button", name="Cancel").first.click(timeout=4000)
                except Exception:
                    pass
                result.update(status="blocked_missing_documents",
                              detail=f"Resume uploaded & selected, but this job requires: {missing}. "
                                     "Not submitted (no transcript on file, or another required doc).")
                await browser.close()
                return result

            # Click the final Submit
            await submit_btn.click(timeout=8000)
            await asyncio.sleep(4)

            # Verify. Broadened confirmation phrases + the authoritative applied flag
            # so a differently-worded success page no longer reads as a false "error"
            # (H4 — this is what made BothBowls look failed on 06-10 though it applied).
            body = (await page.inner_text("body")).lower()
            phrases = ("application has been submitted", "your application has been",
                       "under review", "successfully submitted", "application submitted",
                       "thank you for applying", "has been received")
            text_ok = any(p in body for p in phrases) or await _is_already_applied(None, page)
            flag = await _applied_flag(page, job_id)  # True / False / None
            shot2 = str(SHOTS / f"apply_{job_id[:8]}_result.png")
            await page.screenshot(path=shot2, full_page=True)
            result["screenshot"] = shot2
            if text_ok or flag is True:
                result.update(status="applied", detail="Submitted; confirmation detected.")
            else:
                # Submit was clicked but neither the page text nor the portal flag
                # confirms it. This is NOT a clean error to blindly retry (that risks a
                # double-submit). Distinct status; the next run's authoritative
                # applied-flag check at the top decides whether a retry is even needed.
                result.update(status="submitted_unverified",
                              detail="Submit was clicked but the confirmation could not be "
                                     "verified (text or portal flag). Verify manually; a retry "
                                     "will be a no-op if it actually went through.")
            await browser.close()
            return result

        except Exception as e:
            try:
                await page.screenshot(path=str(SHOTS / f"apply_{job_id[:8]}_error.png"))
            except Exception:
                pass
            result.update(status="error", detail=f"{type(e).__name__}: {e}")
            await browser.close()
            return result


def main():
    if len(sys.argv) < 4:
        print('Usage: python3 auto_apply.py <job_id> <resume_pdf> "<label>" [--submit]')
        sys.exit(1)
    job_id, pdf, label = sys.argv[1], sys.argv[2], sys.argv[3]
    mode = "submit" if "--submit" in sys.argv else "dry"
    print(f"Mode: {mode.upper()}  Job: {job_id}")
    res = asyncio.run(apply_to_job(job_id, pdf, label, mode=mode))
    print("\nRESULT:")
    for k, v in res.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
