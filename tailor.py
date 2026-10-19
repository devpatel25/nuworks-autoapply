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
PROJECT_BANK = RESUMEBOT / "project_bank.md"   # personal, gitignored
BASE_VARIANTS = ["Resume_SWE_General.tex", "Resume_AI-ML_General.tex"]


def load_instructions() -> str:
    """System prompt = ResumeBot2.1 instructions + the Project Bank appendix (if present)."""
    s = INSTRUCTIONS.read_text()
    if PROJECT_BANK.exists():
        s += "\n\n---\n\n" + PROJECT_BANK.read_text()
    # The .md (shared with interactive use) says to compile + recompile; claude -p
    # has no shell, so it stalled or asked for permissions instead (2026-10-15).
    s += ("\n\nUNATTENDED MODE: ignore every compile / verify / recompile step above. "
          "You have no shell access. Only edit the .tex file — Python compiles it and "
          "checks the page count. Every file you need (both base .tex variants, "
          "job_description.txt) is in the current directory: use relative paths only and "
          "never read resume_templates/ or anything outside this directory.")
    return s


def _pascal(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-z ]", "", s).strip()
    return "".join(w[:1].upper() + w[1:] for w in s.split())


def workdir_for(job: dict) -> Path:
    d = APPS / date.today().isoformat() / job["job_id"][:8]
    d.mkdir(parents=True, exist_ok=True)
    return d


# variant name → ATS-repaired copy (data/ats_audit_cache/<sha>.tex) for bases whose
# audit came back yellow. Set once by the pipeline after audit_bases_cached(), before
# any worker starts; read-only afterwards. The template files themselves are never
# written to.
BASE_OVERRIDES: dict = {}


def stage(job: dict) -> Path:
    """Create the per-job working dir with base resumes + the job description."""
    d = workdir_for(job)
    for tex in BASE_VARIANTS:
        shutil.copy(BASE_OVERRIDES.get(tex) or RESUMEBOT / tex, d / tex)
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

# Global settings run at effortLevel xhigh; inherited by `claude -p`, the tailor
# thinks until it hits the output-token cap (thinking only, no .tex — every
# attempt times out). Pin tailoring to medium.
TAILOR_EFFORT = "medium"

# First-draft generation legitimately takes ~240-270s (2026-10-15: four attempts
# killed at 300s). Trim attempts are small edits and keep the shorter timeout.
GEN_TIMEOUT = 480


def max_total_attempts(retries: int = claude_runner.DEFAULT_RETRIES) -> int:
    """Worst-case attempt count across both phases (for the pipeline watchdog)."""
    return (max(retries, 1) + 2) + ESCALATION_ATTEMPTS


_GEN_PROMPT = (
    "Read job_description.txt in this directory. Following your resume "
    "optimization process, select the best base variant (Resume_SWE_General.tex "
    "or Resume_AI-ML_General.tex, both here) and tailor it for this job, "
    "integrating the job's keywords WITHOUT fabricating experience. "
    "CRITICAL — PRESERVE DENSITY: the chosen base variant is ALREADY a complete, "
    "well-balanced ONE-page resume. Your job is to EDIT its text in place to weave "
    "in keywords, NOT to shorten it. Keep its full structure: every Skills category "
    "line, every work-experience bullet (typically 3/2/2 across the three jobs), the "
    "full coursework lines, and every project bullet (lead project 3, others 2). "
    "There is NO Summary section — never add one; put the exact job title in the "
    "headline under the name. PROJECTS: choose 3 projects from the Project Bank "
    "appendix by keyword fit (section 4E) — the base's projects are only a default; "
    "add a 4th only if the page clearly has room, and never invent a project. "
    "Do NOT delete skill lines or bullets, and do NOT leave a half-empty page of white "
    "space — the finished resume must look as full as the base variant. Rewrite "
    "bullets to roughly the same length they already are. "
    "LINE BUDGET: the base fills the page with only ~2 spare lines. Adding keywords "
    "must not make a skill line or bullet wrap onto an extra line — the tailored resume "
    "must not have more rendered lines than the base. "
    "Write the final result to a file named exactly {tex_name} in this directory. "
    "IMPORTANT: You have NO shell access here — do NOT run pdflatex or any shell "
    "command. Only write the .tex file; compilation and one-page verification are "
    "handled automatically after you finish."
)

XYZ_BULLET_GUIDANCE = (
    "XYZ BULLETS: rewrite only the TOP {n} bullets of each work-experience entry and "
    "each project as 'Accomplished [X], as measured by [Y], by doing [Z]'. Use ONLY "
    "metrics that already exist in the base resume; if a bullet has no real metric, "
    "write it as 'Did X by doing Z' — NEVER invent a number. Leave all other bullets "
    "in their current style. The ~29-word cap and every density rule still apply."
)

RETAILOR_PROMPT = (
    "Edit {tex_name} in this directory IN PLACE. It is an already-tailored, finished "
    "ONE-page resume for the job in job_description.txt. Apply ONLY the changes the "
    "candidate picked below — nothing else.\n"
    "Keywords to integrate (each MUST appear naturally, placed only where the "
    "candidate's real experience supports it — never fabricate experience): {keywords}\n"
    "Corrections to apply: {corrections}\n"
    "Candidate facts — use one ONLY when a picked correction asks for it, and never "
    "invent any other fact: current GPA {gpa} (Northeastern University, MS Computer "
    "Science); location for the header line: {location}.\n"
    "Change NOTHING else: keep every other bullet, skill line, project and section, "
    "the same density, the XYZ-format bullets, and every existing metric. Never invent "
    "a number. Do NOT change anything before \\begin{{document}} (preamble, geometry, "
    "fonts) or any \\vspace. You have NO shell access — do NOT run pdflatex or any "
    "shell command; just edit the .tex file. Compilation and one-page verification "
    "run automatically after you finish."
)


def gen_prompt(tex_name: str, keywords: dict | None = None) -> str:
    """Generation prompt. keywords=None → exactly today's prompt (backward compatible).
    With keywords, the pre-extracted list is supplied so the tailor skips its own
    extraction (D6), plus the XYZ guidance (D11)."""
    p = _GEN_PROMPT.format(tex_name=tex_name)
    if not keywords:
        return p
    must = keywords.get("must_have", [])
    nice = keywords.get("nice_to_have", [])
    covered = set(keywords.get("covered", []))
    j = lambda xs: ", ".join(xs) or "—"
    try:
        import resume_agents
        n = int(resume_agents.agent_cfg("xyz_bullets_per_role"))
    except Exception:
        n = 2
    return (p + "\n\nKEYWORDS (pre-extracted — use these, do NOT run your own extraction):\n"
            f"Must-have: {j(must)}\nNice-to-have: {j(nice)}\n"
            f"Already in base: {j([k for k in must + nice if k in covered])}\n"
            f"Missing from base: {j([k for k in must + nice if k not in covered])}\n\n"
            + XYZ_BULLET_GUIDANCE.format(n=n))


def page2_spill(pdf: Path) -> tuple[int, str]:
    """(non-blank line count, text) of what spilled onto page 2 — the trimmer can't
    compile, so without this it only knows "2 pages" and cuts words that save no lines."""
    try:
        txt = subprocess.run(["pdftotext", "-f", "2", "-l", "2", str(pdf), "-"],
                             capture_output=True, text=True).stdout
    except Exception:
        return 0, ""
    lines = [l for l in txt.splitlines() if l.strip()]
    return len(lines), "\n".join(lines)


def _trim_prompt(tex_name: str, pages: int, spill_lines: int = 0, spill_text: str = "") -> str:
    spill = ""
    if spill_lines:
        spill = (f"Exactly {spill_lines} rendered line(s) spilled onto page 2:\n"
                 f"<<<\n{spill_text}\n>>>\nYou must free AT LEAST {spill_lines} whole "
                 "rendered lines (aim for 1 more as margin). Savings count ONLY in whole "
                 "lines: shortening a bullet that still wraps to 2 lines saves nothing. Target "
                 "bullets and skill lines that barely wrap and make them fit on one line. ")
    return (
        f"The resume {tex_name} currently compiles to {pages} pages, but it MUST be "
        f"EXACTLY ONE page — and only slightly over. {spill}Edit {tex_name} to recover the "
        "overflow IN THIS ORDER, stopping as soon as it fits: (1) shorten the Others "
        "skills line, then the Certifications line (drop the least job-relevant items); "
        "(2) tighten the longest bullets (cut filler, merge redundant phrasing) so each "
        "is under ~29 words; (3) if there are 4 projects, remove the least relevant one; "
        "(4) shorten the coursework lines; (5) last resort: remove the lead project's "
        "3rd bullet. Never go below 3 projects, never drop a "
        "skill-category line or a work-experience entry or bullet, and never add a "
        "Summary section. The result "
        "must stay as full and dense as a normal one-page resume — never leave large "
        "white space. Keep all true metrics and the keyword-rich content. Do NOT "
        "change margins, geometry, font size, \\vspace values, or any preamble. You "
        "have NO shell access; just edit the .tex file. Compilation and page "
        "verification run automatically after you finish."
    )


def _run_phases(d: Path, tex_name: str, first_prompt: str, log_path: Path,
                timeout: int, retries: int, reset, find_tex) -> dict:
    """Shared compile → one-page trim → Opus escalation loop (tailor + retailor).

    reset()    — called before a fresh (non-trim) attempt in the Sonnet phase.
    find_tex() — returns the produced .tex Path (named tex_name) or None.

    Two phases: Sonnet; if it can't reach exactly one page, ESCALATE to Opus, which
    keeps trimming Sonnet's best draft. `last_pages` carries across phases so the
    escalation continues trimming rather than starting over."""
    phases = [(TAILOR_MODEL, max(retries, 1) + 2),
              (ESCALATION_MODEL, ESCALATION_ATTEMPTS)]
    total = sum(n for _, n in phases)

    runs = []
    last_pages = 0
    spill = (0, "")
    attempt_no = 0
    escalated = False
    compile_failed = False
    instructions = load_instructions()

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
            # Fresh generation (reset) only happens in the first phase. The
            # escalation phase always continues from the existing draft.
            if not trimming and phase_idx == 0:
                reset()
            prompt = _trim_prompt(tex_name, last_pages, *spill) if trimming else first_prompt

            run = claude_runner.run_claude_p(
                prompt, d, append_system_prompt=instructions,
                add_dir=str(d), timeout=timeout if trimming else max(timeout, GEN_TIMEOUT),
                model=model,
                effort=TAILOR_EFFORT)
            runs.append(run)
            claude_runner._append_attempt_log(log_path, attempt_no, total, run)

            tex = find_tex()
            if not tex:
                last_pages = 0  # no output (stall/timeout) — regenerate fresh next pass
                if attempt_no < total:
                    time.sleep(claude_runner.DEFAULT_BACKOFF * min(attempt_no, 4))
                continue

            pdf, pages = compile_tex(tex)
            if not pdf:
                compile_failed = True
                last_pages = 0  # LaTeX error — regenerate from scratch next pass
                if attempt_no < total:
                    time.sleep(claude_runner.DEFAULT_BACKOFF * min(attempt_no, 4))
                continue

            if pages == 1:
                return {"ok": True, "pdf": str(pdf), "tex": str(tex), "pages": 1,
                        "workdir": str(d), "attempts": attempt_no,
                        "model": model, "escalated": escalated}

            # Compiled but >1 page → keep the .tex and trim it on the next pass.
            last_pages = pages
            spill = page2_spill(pdf)

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
    if compile_failed:
        code, reason = ("compile_failed",
                        "claude produced a resume .tex but it failed to compile to a PDF "
                        "(a LaTeX error). Check the .tex in the workdir.")
    return {"ok": False, "pdf": None, "tex": None, "pages": 0, "workdir": str(d),
            "attempts": attempt_no, "fail_code": code, "fail_reason": reason,
            "log": str(log_path)}


def tailor_with_claude(job: dict, timeout: int = claude_runner.DEFAULT_TIMEOUT,
                       retries: int = claude_runner.DEFAULT_RETRIES,
                       keywords: dict | None = None) -> dict:
    """Unattended path: `claude -p` writes the tailored .tex (content only — no
    shell access here), then Python compiles it deterministically and ENFORCES a
    strict one-page result.

    After each compile the page count is checked. If the PDF is >1 page, claude is
    asked to trim CONTENT (never formatting) and the .tex is recompiled — looping
    until it fits on a single page or the attempt budget is exhausted. A resume is
    only ever consolidated / returned as ok once `pages == 1`.

    keywords (optional, {must_have, nice_to_have, covered}) — Haiku's verified list;
    when given the tailor uses it instead of extracting its own. None = old behavior.

    Each attempt has a hard wall-clock kill (see claude_runner); on total failure a
    classified probable cause is returned. Returns {ok, pdf, tex, pages, workdir,
    attempts} and, on failure, {fail_code, fail_reason, log}."""
    d = stage(job)
    base = expected_basename(job)
    tex_name = f"{base}.tex"

    def find_tex():
        tex = _find_tailored_tex(d, base)
        if tex and tex.name != tex_name:
            tex = tex.rename(d / tex_name)
        return tex

    res = _run_phases(d, tex_name, gen_prompt(tex_name, keywords), d / "claude_tailor.log",
                      timeout, retries, reset=lambda: _clear_tailored_artifacts(d, base),
                      find_tex=find_tex)
    if res["ok"]:
        consolidate(Path(res["pdf"]), Path(res["tex"]))
        res["version"] = 1
    return res


def header_location(job: dict) -> str:
    """Job posting's location for a location-matching correction ("City, State");
    when the posting has no single place, the header stays unchanged."""
    loc = re.sub(r",\s*United States$", "", (job.get("location") or "").strip())
    if not loc or re.search(r"multiple|remote|various|anywhere", loc, re.I):
        return "not available — leave the header location unchanged"
    return loc


VALIDATED = ".validated"  # marker in <workdir>/vN/ once vN passed the post-check


def version_of(tex) -> int:
    """v1 lives in the job workdir; retailored versions in <workdir>/vN/."""
    m = re.fullmatch(r"v(\d+)", Path(tex).parent.name)
    return int(m.group(1)) if m else 1


def retailor(job: dict, tex, picked_keywords: list, picked_corrections: list,
             timeout: int = claude_runner.DEFAULT_TIMEOUT,
             retries: int = claude_runner.DEFAULT_RETRIES) -> dict:
    """Plan §4A.6. Copy the current version's .tex (vN) to <workdir>/v{N+1}/ (vN is
    kept), have Sonnet edit the copy IN PLACE with ONLY the picked keywords and
    corrections, then the same compile → 1-page trim → Opus escalation loop.

    Python guards: an edit that leaves the file unchanged or touches anything before
    \\begin{document} / any \\vspace is rejected (the copy is restored). Returns the
    tailor result shape plus {version}; on failure {fail_code, fail_reason}."""
    import resume_agents  # lazy: resume_agents imports tailor
    tex = Path(tex)
    src = tex.read_text()
    n = version_of(tex)
    root = tex.parent if n == 1 else tex.parent.parent
    d = root / f"v{n + 1}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    work = d / tex.name
    jd = root / "job_description.txt"
    if jd.exists():
        shutil.copy(jd, d / "job_description.txt")
    if not (picked_keywords or picked_corrections):
        return {"ok": False, "fail_code": "nothing_picked", "version": n + 1,
                "fail_reason": "No keywords or corrections were selected."}

    def reset():
        work.write_text(src)
        work.with_suffix(".pdf").unlink(missing_ok=True)

    rejected = []

    def find_tex():
        new = work.read_text() if work.exists() else src
        if new == src:
            return None
        if not resume_agents.preamble_unchanged(src, new):
            rejected.append(1)
            reset()  # forbidden preamble / \vspace edit
            return None
        return work

    prompt = RETAILOR_PROMPT.format(
        tex_name=tex.name,
        keywords=", ".join(picked_keywords) or "(none)",
        corrections="; ".join(picked_corrections) or "(none)",
        gpa=resume_agents.agent_cfg("candidate_gpa"),
        location=header_location(job))
    res = _run_phases(d, tex.name, prompt, d / "claude_retailor.log",
                      timeout, retries, reset=reset, find_tex=find_tex)
    if not res["ok"] and rejected and res["fail_code"] not in ("page_overflow", "compile_failed"):
        res["fail_code"] = "edit_rejected"
        res["fail_reason"] = (f"Every retailor edit ({len(rejected)}) changed the preamble or a "
                              "\\vspace and was rejected; the previous version is unchanged.")
    res["version"] = n + 1
    return res


METRIC_RE = re.compile(r"\d|%|\\\$|\$|×|\\times|\bfold\b", re.I)


def xyz_bullet_stats(tex) -> tuple:
    """Advisory: (bullets containing a metric, total bullets) in a .tex (path or text)."""
    text = Path(tex).read_text() if isinstance(tex, Path) or (
        isinstance(tex, str) and tex.endswith(".tex") and Path(tex).exists()) else tex
    body = text.split(r"\begin{document}", 1)[-1]
    items = [i for i in re.split(r"\\item\b", body)[1:]]
    items = [re.split(r"\\end\{itemize\}", i)[0] for i in items]
    # drop LaTeX comments and spacing commands so "\vspace{-1mm}" / "% 2024" don't count
    items = [re.sub(r"\\[vh]space\*?\{[^}]*\}", " ", re.sub(r"(?<!\\)%.*", " ", i)) for i in items]
    return (sum(1 for i in items if METRIC_RE.search(i)), len(items))


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
    attempt must never be reused (it would skip tailoring and ship a 2-page resume).
    Retailored vN/ PDFs count only once they passed the post-check (VALIDATED marker),
    so a retry restores the newest validated version, not v1."""
    jid = job['job_id'][:8]
    matches = list(APPS.glob(f"*/{jid}/Resume_*.pdf")) + [
        p for p in APPS.glob(f"*/{jid}/v*/Resume_*.pdf") if (p.parent / VALIDATED).exists()]
    matches.sort(key=lambda p: (p.relative_to(APPS).parts[0], version_of(p), p.name))
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
