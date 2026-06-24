#!/usr/bin/env python3
"""
Cover-letter generation — only when a job *requires* one.

A job needs a cover letter when "Cover Letter" appears in the portal's
`documents_required` list (optional cover-letter slots are ignored).

The letter is written from the job description + the tailored resume, matching
the style of the user's previous letters in documents/CoverLetter_*.pdf
(header → date → recipient → 4 flowing paragraphs → "Sincerely, <Your Name>").

Unattended runs generate the body via `claude -p` (like resume tailoring);
in an interactive session the agent writes it directly. Either way `render()`
fills the LaTeX template, compiles to PDF, and consolidates into cover_letters/.
"""
import re
import json
import subprocess
from pathlib import Path
from datetime import date

import claude_runner

HERE = Path(__file__).resolve().parent
COVERS = HERE / "cover_letters"          # all generated cover letters, one folder
SAMPLES_DIR = HERE / "documents"          # CoverLetter_*.pdf style references
APPS = HERE / "applications"

def _identity() -> tuple[str, str]:
    """Applicant name + LaTeX contact line, loaded from data/profile.json (gitignored,
    per-user) so no personal contact info is hardcoded here. Neutral placeholders if absent."""
    try:
        p = json.loads((HERE / "data" / "profile.json").read_text())
    except Exception:
        p = {}
    name = p.get("name") or "Your Name"
    contact = (f"{p.get('location', 'Your City, ST')} $\\bullet$ {p.get('phone', '(000) 000-0000')} "
               f"$\\bullet$ {p.get('email', 'you@example.com')} $\\bullet$ LinkedIn: {name}")
    return name, contact


NAME, CONTACT = _identity()

CL_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{helvet}
\renewcommand{\familydefault}{\sfdefault}
\usepackage{ragged2e}
\usepackage[none]{hyphenat}
\usepackage{microtype}
\usepackage{setspace}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{8pt}
\begin{document}
\begin{center}
{\fontsize{16}{18}\selectfont \textbf{%(name)s}}\\[2pt]
\small %(contact)s
\end{center}
\vspace{6pt}
%(date)s

%(recipient)s

\vspace{4pt}
%(salutation)s

\justifying
%(body)s

\vspace{6pt}
Sincerely,\\[2pt]
%(name)s
\end{document}
"""


def needs_cover_letter(required_docs) -> bool:
    return any("cover letter" in str(d).lower() for d in (required_docs or []))


def _pascal(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-z ]", "", s).strip()
    return "".join(w[:1].upper() + w[1:] for w in s.split())


def latex_escape(s: str) -> str:
    repl = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
            "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(repl.get(c, c) for c in (s or ""))


def basename(job: dict) -> str:
    return f"CoverLetter_{_pascal(job.get('title','Role'))}_{_pascal(job.get('company','Company'))}"


def render(job: dict, body: str, recipient_lines: list[str], salutation: str) -> str:
    """Fill the template with an already-written body, compile, consolidate.
    `body` is plain text (paragraphs separated by blank lines); it is escaped."""
    d = APPS / date.today().isoformat() / job["job_id"][:8]
    d.mkdir(parents=True, exist_ok=True)
    base = basename(job)

    body_tex = "\n\n".join(latex_escape(p.strip()) for p in body.split("\n\n") if p.strip())
    recipient_tex = r"\\".join(latex_escape(x) for x in recipient_lines)
    tex = CL_TEMPLATE % {
        "name": NAME,
        "contact": CONTACT,
        "date": latex_escape(date.today().strftime("%B %d, %Y")),
        "recipient": recipient_tex,
        "salutation": latex_escape(salutation),
        "body": body_tex,
    }
    tex_path = d / f"{base}.tex"
    tex_path.write_text(tex)
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", f"{base}.tex"],
                   cwd=str(d), capture_output=True)
    for ext in (".aux", ".log", ".out"):
        (d / f"{base}{ext}").unlink(missing_ok=True)

    pdf = d / f"{base}.pdf"
    if pdf.exists():
        COVERS.mkdir(exist_ok=True)
        stamp = date.today().isoformat()
        dest = COVERS / f"{stamp}__{base}.pdf"
        dest.write_bytes(pdf.read_bytes())
        (COVERS / f"{stamp}__{base}.tex").write_text(tex)
        return str(dest)
    return ""


def generate_with_claude(job: dict, resume_tex_path: str,
                         timeout: int = claude_runner.DEFAULT_TIMEOUT,
                         retries: int = claude_runner.DEFAULT_RETRIES) -> dict:
    """Unattended path: have `claude -p` write the cover-letter body in the
    user's style (referencing documents/CoverLetter_*.pdf) from the JD + resume,
    then render it. Hardened like resume tailoring (hard wall-clock kill, retries,
    per-job log, classified failure). Returns {ok, pdf} and, on failure,
    {fail_code, fail_reason, log}."""
    d = APPS / date.today().isoformat() / job["job_id"][:8]
    d.mkdir(parents=True, exist_ok=True)
    (d / "job_description.txt").write_text(
        f"Job Title: {job.get('title','')}\nCompany: {job.get('company','')}\n"
        f"Location: {job.get('location','')}\n\n{job.get('description','')}\n")
    base = basename(job)
    log_path = d / "claude_coverletter.log"

    # H7: the style samples are PDFs in ../documents (outside claude -p's sandbox
    # and not text-readable). Extract them to .txt INSIDE the workdir so the model
    # can actually read them; reference those local files in the prompt.
    sample_note = "the four-paragraph structure described above"
    try:
        names = []
        for pdf_sample in sorted(SAMPLES_DIR.glob("CoverLetter_*.pdf")):
            txt_out = d / (pdf_sample.stem + ".sample.txt")
            subprocess.run(["pdftotext", "-layout", str(pdf_sample), str(txt_out)],
                           capture_output=True)
            if txt_out.exists() and txt_out.stat().st_size > 0:
                names.append(txt_out.name)
        if names:
            sample_note = "the tone and structure of the sample letters " + ", ".join(names)
    except Exception:
        pass

    prompt = (
        "Write a one-page cover letter body (4 short flowing paragraphs, no header/"
        "date/signature) for the job in job_description.txt, drawn from the tailored "
        f"resume at {resume_tex_path}. Match {sample_note} (enthusiasm + role; experience "
        "mapping with metrics; projects; mission fit + commitment). Use only TRUE "
        "experience from the resume — never invent anything. Output ONLY the body text, "
        "paragraphs separated by blank lines — no markdown, no preamble like 'Here is', "
        "no salutation, no signature."
    )

    def success_check(run) -> str | None:
        body = (run.get("stdout") or "").strip()
        if len(body) < 80:
            return None  # empty/short
        low = body.lower()
        # Reject refusals/apologies, markdown fences, and preamble that would get
        # rendered verbatim into a letter sent to an employer (C6).
        bad = ("i cannot", "i can't", "i am unable", "i'm unable", "as an ai",
               "```", "here is the", "here's the", "here is a", "i don't have access")
        if any(b in low for b in bad):
            return None
        if body.count("\n\n") < 2:  # expect ~4 paragraphs
            return None
        return body

    # Pinned to Sonnet 4.6 like resume tailoring (Dev's choice 2026-06-10) —
    # bounded writing task, faster/cheaper than the account default.
    res = claude_runner.run_with_retries(
        prompt, d, success_check, timeout=timeout, retries=retries,
        add_dir=str(d), log_path=log_path, model="claude-sonnet-4-6")

    if not res["ok"]:
        code, reason = claude_runner.classify_failure(res["runs"])
        return {"ok": False, "pdf": None, "attempts": res["attempts"],
                "fail_code": code, "fail_reason": reason, "log": str(log_path)}

    body = res["value"]
    company = job.get("company", "")
    loc = job.get("location", "").split(",")[0].strip()
    recipient = [company, "Hiring Team"] + ([loc] if loc else [])
    pdf = render(job, body, recipient, f"Dear {company} Hiring Team,")
    if not pdf:
        return {"ok": False, "pdf": None, "fail_code": "compile_failed",
                "fail_reason": "Cover-letter body was generated but the LaTeX render "
                               "failed to produce a PDF.", "log": str(log_path)}
    return {"ok": True, "pdf": pdf}


if __name__ == "__main__":
    # Template smoke test with a placeholder body.
    job = {"job_id": "testtest", "title": "Software Engineer Co-op", "company": "Acme Corp",
           "location": "Boston, MA"}
    body = ("I am writing to express my enthusiasm for the Software Engineer Co-op role at Acme Corp. "
            "As a Master's student in Computer Science at Northeastern University, I am excited to contribute.\n\n"
            "My technical profile strongly matches your requirements. At a previous internship I built REST APIs "
            "and microservices serving 500+ users with 95% test coverage.\n\n"
            "My project work further demonstrates my readiness, including an AI chatbot processing 2,000+ queries.\n\n"
            "What excites me most about Acme is the mission. Thank you for your consideration.")
    print("rendered ->", render(job, body, ["Acme Corp", "Hiring Team", "Boston, MA"], "Dear Acme Corp Hiring Team,"))
