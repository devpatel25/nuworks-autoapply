#!/usr/bin/env python3
"""
Tailor a resume for one job using the ResumeBot2.1 process.

For unattended (10AM) runs this shells out to `claude -p` headless, feeding the
ResumeBot2.1 system_instructions as the system prompt and pointing it at the two
base .tex variants. claude does the full loop: pick variant -> inject JD keywords
-> compile pdflatex -> verify 1 page -> save Resume_[Title]_[Company].{tex,pdf}.

NOTE: when developing inside an interactive Claude Code session, spawning a
nested `claude -p` agent is blocked by the safety classifier. In that case the
in-session agent tailors directly instead. The scheduled launchd job (the user's
own cron) is what invokes `claude -p`, which is permitted.
"""
import re
import csv
import json
import time
import shutil
import subprocess
from pathlib import Path
from datetime import date

import claude_runner

HERE = Path(__file__).resolve().parent
RESUMEBOT = HERE / "resume_templates"
APPS = HERE / "applications"
RESUMES = HERE / "resumes"          # consolidated copies of every tailored resume
JOBS_CSV = HERE / "data" / "jobs.csv"
INSTRUCTIONS = RESUMEBOT / "system_instructions.md"
BASE_VARIANTS = ["Resume_SWE_General.tex", "Resume_AI-ML_General.tex"]


def _pascal(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-z ]", "", s).strip()
    return "".join(w[:1].upper() + w[1:] for w in s.split())


def workdir_for(job: dict) -> Path:
    d = APPS / date.today().isoformat() / job["job_id"][:8]
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage(job: dict) -> Path:
    """Create the per-job working dir with base resumes + the job description."""
    d = workdir_for(job)
    for tex in BASE_VARIANTS:
        shutil.copy(RESUMEBOT / tex, d / tex)
    (d / "job_description.txt").write_text(
        f"Job Title: {job.get('title','')}\nCompany: {job.get('company','')}\n"
        f"Location: {job.get('location','')}\nType: {job.get('type','')}\n\n"
        f"{job.get('description','')}\n"
    )
    return d


def expected_basename(job: dict) -> str:
    return f"Resume_{_pascal(job.get('title','Role'))}_{_pascal(job.get('company','Company'))}"


def _find_tailored_tex(d: Path, base: str) -> Path | None:
    """Locate the tailored .tex claude produced. Prefer the expected name, else
    any .tex that isn't a base variant (claude sometimes names it resume.tex)."""
    preferred = d / f"{base}.tex"
    if preferred.exists():
        return preferred
    candidates = [t for t in d.glob("*.tex") if t.name not in BASE_VARIANTS]
    if not candidates:
        return None
    # newest non-base .tex
    return max(candidates, key=lambda p: p.stat().st_mtime)


def compile_tex(tex_path: Path) -> tuple[Path | None, int]:
    """Compile a .tex with pdflatex (in Python — no shell perms needed by claude).
    Returns (pdf_path or None, page_count)."""
    d = tex_path.parent
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
                   cwd=str(d), capture_output=True)
    for ext in (".aux", ".log", ".out"):
        (d / (tex_path.stem + ext)).unlink(missing_ok=True)
    pdf = tex_path.with_suffix(".pdf")
    if not pdf.exists():
        return None, 0
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
        pages = int(next(l.split(":")[1].strip() for l in out.splitlines() if l.startswith("Pages")))
    except Exception:
        pages = 0
    return pdf, pages


def _clear_tailored_artifacts(d: Path, base: str):
    """Remove any prior tailored .tex/.pdf (keep the base variants) so a retry's
    success check only ever sees the *current* attempt's output — never a stale
    file from a previous attempt that failed to compile."""
    for p in list(d.glob("*.tex")) + list(d.glob("*.pdf")):
        if p.name in BASE_VARIANTS:
            continue
        p.unlink(missing_ok=True)


# Tailoring is pinned to Sonnet 4.6 (Dev's choice 2026-06-10): the task is
# well-bounded keyword injection into a .tex, so the faster/cheaper model is
# preferred over the account default. Other claude -p uses (cover letters,
# bridge, career-ops) stay on the default model.
TAILOR_MODEL = "claude-sonnet-4-6"

# If Sonnet can't get the resume to exactly one page, ESCALATE to a higher-
# capability model (Opus) which continues trimming Sonnet's best draft. Opus is
# slower/pricier, so it's only used as the fallback when Sonnet hits page_overflow.
ESCALATION_MODEL = "claude-opus-4-8"
ESCALATION_ATTEMPTS = 3


def max_total_attempts(retries: int = claude_runner.DEFAULT_RETRIES) -> int:
    """Worst-case attempt count across both phases (for the pipeline watchdog)."""
    return (max(retries, 1) + 2) + ESCALATION_ATTEMPTS


def tailor_with_claude(job: dict, timeout: int = claude_runner.DEFAULT_TIMEOUT,
                       retries: int = claude_runner.DEFAULT_RETRIES) -> dict:
    """Unattended path: `claude -p` writes the tailored .tex (content only — no
    shell access here), then Python compiles it deterministically and ENFORCES a
    strict one-page result.

    After each compile the page count is checked. If the PDF is >1 page, claude is
    asked to trim CONTENT (never formatting) and the .tex is recompiled — looping
    until it fits on a single page or the attempt budget is exhausted. A resume is
    only ever consolidated / returned as ok once `pages == 1`. (Previously the page
    count was measured but ignored, so 2-page resumes shipped — that was the bug.)

    Each attempt has a hard wall-clock kill (see claude_runner); on total failure a
    classified probable cause is returned. Returns {ok, pdf, tex, pages, workdir,
    attempts} and, on failure, {fail_code, fail_reason, log}."""
    d = stage(job)
    base = expected_basename(job)
    tex_name = f"{base}.tex"
    log_path = d / "claude_tailor.log"

    gen_prompt = (
        "Read job_description.txt in this directory. Following your resume "
        "optimization process, select the best base variant (Resume_SWE_General.tex "
        "or Resume_AI-ML_General.tex, both here) and tailor it for this job, "
        "integrating the job's keywords WITHOUT fabricating experience. "
        "CRITICAL — PRESERVE DENSITY: the chosen base variant is ALREADY a complete, "
        "well-balanced ONE-page resume. Your job is to EDIT its text in place to weave "
        "in keywords, NOT to shorten it. Keep its full structure: every Skills category "
        "line, every work-experience bullet (typically 3/2/3 across the three jobs), the "
        "full coursework line, and BOTH bullet points under EACH project. Do NOT delete "
        "skill lines, bullets, or projects, and do NOT leave a half-empty page of white "
        "space — the finished resume must look as full as the base variant. Rewrite "
        "bullets to roughly the same length they already are. "
        f"Write the final result to a file named exactly {tex_name} in this directory. "
        "IMPORTANT: You have NO shell access here — do NOT run pdflatex or any shell "
        "command. Only write the .tex file; compilation and one-page verification are "
        "handled automatically after you finish."
    )

    def trim_prompt(pages: int) -> str:
        return (
            f"The resume {tex_name} currently compiles to {pages} pages, but it MUST be "
            f"EXACTLY ONE page — and only slightly over. Edit {tex_name} to recover the "
            "overflow by TIGHTENING wording, not by gutting content: shorten the longest "
            "bullets (cut filler words, merge redundant phrasing) so each is under ~29 "
            "words, and trim the Summary if it runs 3+ lines. Do this FIRST and only as "
            "much as needed. Do NOT delete whole skill-category lines, do NOT drop a "
            "project or a project's bullet, and do NOT remove a work-experience entry "
            "unless tightening every bullet still leaves it over one page. The result "
            "must stay as full and dense as a normal one-page resume — never leave large "
            "white space. Keep all true metrics and the keyword-rich content. Do NOT "
            "change margins, geometry, font size, \\vspace values, or any preamble. You "
            "have NO shell access; just edit the .tex file. Compilation and page "
            "verification run automatically after you finish."
        )

    # Two phases: tailor with Sonnet; if it can't reach exactly one page, ESCALATE
    # to Opus, which keeps trimming Sonnet's best draft (a more capable model often
    # resolves the last bit of overflow). `last_pages` carries across phases so the
    # escalation continues trimming rather than starting over.
    phases = [(TAILOR_MODEL, max(retries, 1) + 2),
              (ESCALATION_MODEL, ESCALATION_ATTEMPTS)]
    total = sum(n for _, n in phases)

    runs = []
    last_pages = 0
    attempt_no = 0
    escalated = False
    instructions = INSTRUCTIONS.read_text()

    for phase_idx, (model, n_attempts) in enumerate(phases):
        if phase_idx > 0:
            # Only escalate if there's a genuine page overflow left to fix.
            if not (last_pages and last_pages > 1):
                break
            escalated = True
            with open(log_path, "a") as f:
                f.write(f"\n=== ESCALATING to {model} (Sonnet left it at {last_pages} "
                        f"pages) ===\n")

        for _ in range(n_attempts):
            attempt_no += 1
            trimming = last_pages > 1
            # Fresh generation (clearing prior artifacts) only happens in the first
            # phase. The escalation phase always continues from the existing draft.
            if not trimming and phase_idx == 0:
                _clear_tailored_artifacts(d, base)
            prompt = trim_prompt(last_pages) if trimming else gen_prompt

            run = claude_runner.run_claude_p(
                prompt, d, append_system_prompt=instructions,
                add_dir=str(d), timeout=timeout, model=model)
            runs.append(run)
            claude_runner._append_attempt_log(log_path, attempt_no, total, run)

            tex = _find_tailored_tex(d, base)
            if not tex:
                last_pages = 0  # no output (stall/timeout) — regenerate fresh next pass
                if attempt_no < total:
                    time.sleep(claude_runner.DEFAULT_BACKOFF * min(attempt_no, 4))
                continue
            if tex.name != tex_name:
                tex = tex.rename(d / tex_name)

            pdf, pages = compile_tex(tex)
            if not pdf:
                last_pages = 0  # LaTeX error — regenerate from scratch next pass
                if attempt_no < total:
                    time.sleep(claude_runner.DEFAULT_BACKOFF * min(attempt_no, 4))
                continue

            if pages == 1:
                consolidate(pdf, tex)
                return {"ok": True, "pdf": str(pdf), "tex": str(tex), "pages": 1,
                        "workdir": str(d), "attempts": attempt_no,
                        "model": model, "escalated": escalated}

            # Compiled but >1 page → keep the .tex and trim it on the next pass.
            last_pages = pages

    # Exhausted both phases without a one-page PDF — classify why.
    if last_pages and last_pages > 1:
        return {"ok": False, "pdf": None, "tex": None, "pages": last_pages,
                "workdir": str(d), "attempts": attempt_no, "escalated": escalated,
                "fail_code": "page_overflow",
                "fail_reason": (f"The tailored resume still compiled to {last_pages} pages "
                                f"after trim attempts with {TAILOR_MODEL} AND escalation to "
                                f"{ESCALATION_MODEL} — it must be exactly one page. The "
                                ".tex in the workdir needs manual trimming."),
                "log": str(log_path)}
    code, reason = claude_runner.classify_failure(runs)
    if _find_tailored_tex(d, base):
        code, reason = ("compile_failed",
                        "claude produced a resume .tex but it failed to compile to a PDF "
                        "(a LaTeX error). Check the .tex in the workdir.")
    return {"ok": False, "pdf": None, "tex": None, "pages": 0, "workdir": str(d),
            "attempts": attempt_no, "fail_code": code, "fail_reason": reason,
            "log": str(log_path)}


def consolidate(pdf: Path, tex: Path | None = None) -> str:
    """Copy a finished tailored resume into resumes/ with a date prefix so all
    resumes live in one easy-to-browse folder. Returns the consolidated PDF path."""
    RESUMES.mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    dest_pdf = RESUMES / f"{stamp}__{Path(pdf).name}"
    shutil.copy(pdf, dest_pdf)
    if tex and Path(tex).exists():
        shutil.copy(tex, RESUMES / f"{stamp}__{Path(tex).name}")
    return str(dest_pdf)


def latest_pdf(job: dict) -> str | None:
    """Find an already-produced tailored PDF for this job to reuse. Only returns a
    PDF that is EXACTLY one page — a leftover 2-page PDF from a failed page_overflow
    attempt must never be reused (it would skip tailoring and ship a 2-page resume)."""
    matches = sorted(APPS.glob(f"*/{job['job_id'][:8]}/Resume_*.pdf"))
    for p in reversed(matches):
        try:
            out = subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True).stdout
            pages = int(next(l.split(":")[1].strip()
                             for l in out.splitlines() if l.startswith("Pages")))
        except Exception:
            pages = 0
        if pages == 1:
            return str(p)
    return None


if __name__ == "__main__":
    import sys
    rows = list(csv.DictReader(open(JOBS_CSV)))
    job = rows[int(sys.argv[1])] if len(sys.argv) > 1 else rows[0]
    print("Staging:", stage(job))
    print("Expected output basename:", expected_basename(job))
    print("Existing tailored PDF:", latest_pdf(job))
