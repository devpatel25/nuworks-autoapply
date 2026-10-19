#!/usr/bin/env python3
"""
Hybrid resume pipeline helpers (HYBRID_PIPELINE_PLAN.md Steps 3, 5, 6, 9, 10).

LLM proposes, Python verifies:
  • audit_bases_cached()   — base-resume ATS audit (sha256-cached), LLM repair of a
                             SCRATCH copy with a Python preamble-unchanged guard.
  • extract_keywords()     — Haiku (`claude -p`) returns must-have / nice-to-have JSON.
  • score_keywords()       — drops keywords not literally in the JD, normalizes
                             synonyms, word-boundary match, better-of-two-bases score.
  • verify_tailored_pdf()  — post-check of the tailored PDF (ATS + keyword coverage).
  • build_verdict()        — ≤400-char caption line for the resume card.

The base templates in resume_templates/ are NEVER written to — repaired copies live
in data/ats_audit_cache/ only.
"""
import re
import json
import shutil
import hashlib
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

import claude_runner
import tailor

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
AUDIT_FILE = HERE / "data" / "ats_audit.json"
AUDIT_CACHE = HERE / "data" / "ats_audit_cache"

AGENT_DEFAULTS = {
    "keyword_model": "claude-haiku-5-5",
    "keyword_attempts": 2,
    "min_match_score": 0.40,
    "min_keywords": 5,
    "low_match_timeout_s": 600,
    "posting_timeout_s": 3600,
    "ats_fix_attempts": 3,
    "ats_reaudit_days": 30,
    "xyz_bullets_per_role": 2,
    "bi_mcp_dir": "~/Desktop/Claude-env/Biginterview-MCP/Biginterview-MCP",
    "bi_scoring_guide": "Graduate - STEM Focus",
    "bi_poll_s": 20,
    "bi_scan_timeout_s": 600,
    "bi_reset_delete_count": 5,
    "retailor_max_rounds": 3,
    "bi_correction_categories": ["credibility", "ats_fit"],
    "bi_max_toggles": 20,
    "candidate_gpa": "3.93/4",
}


def agent_cfg(key: str):
    """config.json → agents.<key>, falling back to the §7 default."""
    try:
        return json.loads(CONFIG.read_text()).get("agents", {}).get(key, AGENT_DEFAULTS[key])
    except Exception:
        return AGENT_DEFAULTS[key]


# ── ATS checks ───────────────────────────────────────────────────────────────

BEGIN_DOC = r"\begin{document}"
LAYOUT_BAD = re.compile(r"\\(?:begin\{(?:tabular\*?|multicols\*?|minipage)\}|includegraphics)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
SECTION_RE = re.compile(r"\\section\*\{([^}]*)\}")
CRITICAL_CHECKS = ("text_extractable", "contact_found")


def _preamble(tex: str) -> str | None:
    return tex.split(BEGIN_DOC, 1)[0] if BEGIN_DOC in tex else None


def _vspaces(tex: str) -> list:
    return re.findall(r"\\vspace\*?\{[^}]*\}", tex)


def preamble_unchanged(old: str, new: str) -> bool:
    """Invariant I2 in code: nothing before \\begin{document} may change, and the
    \\vspace sequence in the body must be identical."""
    p_old, p_new = _preamble(old), _preamble(new)
    return p_old is not None and p_old == p_new and _vspaces(old) == _vspaces(new)


def evaluate_extraction(text: str, tex: str, compiled: bool = True) -> dict:
    """The 7 checks (plan §4 Step 3). Returns {checks: {name: bool}, failed: [...]}."""
    body = tex.split(BEGIN_DOC, 1)[1] if BEGIN_DOC in tex else tex
    text = text or ""
    low = text.lower()
    headers = [h.replace(r"\&", "&").strip() for h in SECTION_RE.findall(body) if h.strip()]
    found = [low.find(h.lower()) for h in headers]
    pos, ordered = -1, True
    for h in headers:
        i = low.find(h.lower(), pos + 1)
        if i < 0:
            ordered = False
            break
        pos = i
    words = len(text.split())
    checks = {
        "tex_layout_lint": not LAYOUT_BAD.search(body),
        "compiles": bool(compiled),
        "text_extractable": len(text.strip()) > 200,
        "contact_found": bool(EMAIL_RE.search(text) and PHONE_RE.search(text)),
        "sections_present": bool(headers) and all(i >= 0 for i in found),
        "reading_order": bool(headers) and ordered,
        "word_count_sane": 200 <= words <= 1200,
    }
    return {"checks": checks, "failed": [k for k, v in checks.items() if not v],
            "words": words}


def pdf_text(pdf) -> str:
    try:
        return subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                              capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def run_ats_checks(tex_path) -> dict:
    """Compile a COPY of tex_path in a temp dir → pdftotext → evaluate_extraction."""
    tex_path = Path(tex_path)
    src = tex_path.read_text()
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / tex_path.name
        work.write_text(src)
        pdf, _ = tailor.compile_tex(work)
        text = pdf_text(pdf) if pdf else ""
    return evaluate_extraction(text, src, compiled=bool(pdf))


# ── Base audit (Step 3) ──────────────────────────────────────────────────────

ATS_FIX_PROMPT = (
    "The LaTeX resume {name} in this directory fails these ATS text-extraction checks: "
    "{failed}. Check meanings: tex_layout_lint = no tabular/multicols/minipage/"
    "includegraphics in the body; compiles = pdflatex must succeed; text_extractable = "
    "the PDF must yield >200 chars of text; contact_found = email AND phone must be "
    "plain text; sections_present / reading_order = every \\section*{{...}} header must "
    "appear in the extracted text, in document order; word_count_sane = 200-1200 words. "
    "Edit {name} IN PLACE to fix ONLY these failures. Do NOT change anything before "
    "\\begin{{document}} (preamble, packages, geometry, fonts) and do NOT add, remove or "
    "change any \\vspace. Do NOT change wording, facts or metrics. You have NO shell "
    "access; just edit the file."
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_audit() -> dict:
    try:
        return json.loads(AUDIT_FILE.read_text())
    except Exception:
        return {}


def _repair(name: str, src: str, sha: str, report: dict) -> tuple:
    """Bounded LLM repair of a scratch copy. Returns (repaired_tex_path | None, report)."""
    work_dir = AUDIT_CACHE / f"{sha}.work"
    work_dir.mkdir(parents=True, exist_ok=True)
    work = work_dir / name
    work.write_text(src)
    good = src
    for attempt in range(1, int(agent_cfg("ats_fix_attempts")) + 1):
        claude_runner.run_claude_p(
            ATS_FIX_PROMPT.format(name=name, failed=", ".join(report["failed"])),
            work_dir, add_dir=str(work_dir), model=tailor.TAILOR_MODEL)
        new = work.read_text() if work.exists() else good
        if not preamble_unchanged(src, new):
            report = {**report, "guard_rejections": report.get("guard_rejections", 0) + 1}
            work.write_text(good)  # reject the edit, restore last good text
            continue
        good = new
        report = {**run_ats_checks(work), "repair_attempts": attempt,
                  "guard_rejections": report.get("guard_rejections", 0)}
        if not report["failed"]:
            out = AUDIT_CACHE / f"{sha}.tex"
            out.write_text(good)
            shutil.rmtree(work_dir, ignore_errors=True)
            return out, report
    shutil.rmtree(work_dir, ignore_errors=True)
    return None, report


def audit_bases_cached(bases=None, src_dir=None) -> dict:
    """Step 3. Returns {variant_name: {status, usable_tex, report}} where status is
    green (original clean) / yellow (repaired copy in AUDIT_CACHE) / red (unusable).
    Cache hit = same sha256 and record younger than ats_reaudit_days. Red results
    are not cached (they may be transient, e.g. a claude -p network failure)."""
    bases = bases or tailor.BASE_VARIANTS
    src_dir = Path(src_dir or tailor.RESUMEBOT)
    cache = _load_audit()
    max_age = timedelta(days=int(agent_cfg("ats_reaudit_days")))
    out = {}
    for name in bases:
        path = src_dir / name
        if not path.exists():
            out[name] = {"status": "red", "usable_tex": None,
                         "report": {"failed": ["missing"], "checks": {}}}
            continue
        sha = _sha(path)
        rec = cache.get(name) or {}
        fresh = False
        try:
            fresh = datetime.now() - datetime.fromisoformat(rec["ts"]) < max_age
        except Exception:
            pass
        if rec.get("sha") == sha and fresh and rec.get("status") in ("green", "yellow") \
                and Path(rec.get("usable_tex") or "").exists():
            out[name] = {"status": rec["status"], "usable_tex": rec["usable_tex"],
                         "report": rec.get("report", {}), "cached": True}
            continue
        src = path.read_text()
        report = run_ats_checks(path)
        if not report["failed"]:
            status, usable = "green", str(path)
        else:
            fixed, report = _repair(name, src, sha, report)
            status, usable = ("yellow", str(fixed)) if fixed else ("red", None)
        out[name] = {"status": status, "usable_tex": usable, "report": report}
        if status == "red":
            cache.pop(name, None)
        else:
            cache[name] = {"sha": sha, "status": status, "usable_tex": usable,
                           "report": report, "ts": datetime.now().isoformat()}
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_FILE.write_text(json.dumps(cache, indent=2))
    return out


# ── Keyword extraction (Step 5) ──────────────────────────────────────────────

KEYWORD_EXTRACT_PROMPT = (
    "You are an ATS keyword extractor. From the job description below, return STRICT "
    "JSON ONLY — no prose, no markdown — in exactly this shape:\n"
    '{{"must_have": ["..."], "nice_to_have": ["..."]}}\n'
    "Rules: 15-30 terms total. must_have = terms from the required / minimum "
    "qualifications / responsibilities; nice_to_have = preferred / bonus / nice-to-have "
    "terms. Use each term EXACTLY as spelled in the job description. Concrete, "
    "searchable skills, tools, technologies, methods and domains only. No generic "
    "filler (experience, skills, team, opportunity), no company names unless they are "
    "a technology platform (AWS, Azure, GCP). Never invent a term that is not in the "
    "text. Do not use any tools.\n\nJOB DESCRIPTION:\n{jd}"
)


def _parse_keywords_json(text: str) -> dict | None:
    """Find the first JSON object with must_have / nice_to_have string lists, even if
    the model wrapped it in prose or a code fence. None if nothing usable."""
    dec = json.JSONDecoder()
    text = text or ""
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        must, nice = obj.get("must_have"), obj.get("nice_to_have", [])
        if isinstance(must, list) and isinstance(nice, list):
            clean = lambda xs: [x.strip() for x in xs if isinstance(x, str) and x.strip()]
            return {"must_have": clean(must), "nice_to_have": clean(nice)}
    return None


def extract_keywords(job: dict, workdir) -> dict:
    """Haiku keyword extraction. Cached in <workdir>/keywords.json (a low-match job
    re-asked next run reuses it). Returns {must_have, nice_to_have} or {error}."""
    workdir = Path(workdir)
    cache = workdir / "keywords.json"
    jd = (workdir / "job_description.txt").read_text()
    jd_sha = hashlib.sha256(jd.encode()).hexdigest()
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
        except Exception:
            cached = {}
        got = _parse_keywords_json(json.dumps(cached))
        if got and cached.get("jd_sha256") == jd_sha:   # JD changed → re-extract
            return got
    attempts = int(agent_cfg("keyword_attempts"))
    log_path = workdir / "keywords.log"
    last = "no attempt"
    for i in range(1, attempts + 1):
        run = claude_runner.run_claude_p(
            KEYWORD_EXTRACT_PROMPT.format(jd=jd), workdir, timeout=180, max_turns=2,
            model=agent_cfg("keyword_model"))
        claude_runner._append_attempt_log(log_path, i, attempts, run)
        got = _parse_keywords_json(run.get("stdout", ""))
        if got and got["must_have"] + got["nice_to_have"]:
            cache.write_text(json.dumps({**got, "jd_sha256": jd_sha}, indent=2))
            return got
        last = "timed out" if run.get("timed_out") else "no parseable JSON"
    return {"error": f"keyword extraction failed after {attempts} attempt(s): {last}"}


# ── Verify + score (Step 6) ──────────────────────────────────────────────────

# alias (lowercase) → canonical form. Extend when a score looks wrong (§12).
SYNONYMS = {
    "k8s": "Kubernetes", "kubernetes": "Kubernetes",
    "postgres": "PostgreSQL", "postgresql": "PostgreSQL",
    "js": "JavaScript", "javascript": "JavaScript",
    "ts": "TypeScript", "typescript": "TypeScript",
    "ml": "Machine Learning", "machine learning": "Machine Learning",
    "ai": "Artificial Intelligence", "artificial intelligence": "Artificial Intelligence",
    "nlp": "Natural Language Processing",
    "natural language processing": "Natural Language Processing",
    "cv": "Computer Vision", "computer vision": "Computer Vision",
    "gcp": "Google Cloud Platform", "google cloud": "Google Cloud Platform",
    "google cloud platform": "Google Cloud Platform",
    "amazon web services": "AWS", "aws": "AWS",
    "ci/cd": "CI/CD", "cicd": "CI/CD", "continuous integration": "CI/CD",
    "node": "Node.js", "nodejs": "Node.js", "node.js": "Node.js",
    "react.js": "React", "reactjs": "React", "react": "React",
    "golang": "Go", "go": "Go",
    "mongo": "MongoDB", "mongodb": "MongoDB",
    "sklearn": "Scikit-learn", "scikit-learn": "Scikit-learn",
    "llm": "LLMs", "llms": "LLMs", "large language models": "LLMs",
    "rest": "REST API", "rest api": "REST API", "restful": "REST API",
    "rest apis": "REST API", "restful apis": "REST API",
}


# Slash terms that are ONE skill; every other "A/B" keyword (e.g. "JavaScript/TypeScript",
# "ML/AI concepts") is split into its parts before scoring.
NO_SPLIT = {"ci/cd", "tcp/ip", "i/o", "pl/sql", "a/b testing", "a/b", "ui/ux"}


def split_slash(k: str) -> list:
    if "/" not in k or k.strip().lower() in NO_SPLIT:
        return [k]
    return [p.strip() for p in k.split("/") if p.strip()]


def _canon(k: str) -> str:
    return SYNONYMS.get(k.strip().lower(), k.strip())


def _aliases(canonical: str) -> set:
    return {canonical} | {a for a, c in SYNONYMS.items() if c == canonical}


def _term_re(term: str):
    """Whole-term regex: no letter/digit directly before or after (so Java ≠
    JavaScript, Go ≠ Google). Terms of ≤2 chars (R, Go, C) match case-sensitively so
    they don't hit ordinary English words."""
    flags = 0 if len(term) <= 2 else re.I
    body = r"\s+".join(re.escape(w) for w in term.split())   # phrases may wrap lines
    # C must not match inside C++ / C#: after an alphanumeric end, + and # extend the name.
    tail = r"(?![A-Za-z0-9+#])" if term[-1:].isalnum() else r"(?![A-Za-z0-9])"
    return re.compile(r"(?<![A-Za-z0-9])" + body + tail, flags)


def has_term(term: str, text: str) -> bool:
    return bool(_term_re(term).search(text or ""))


def has_keyword(canonical: str, text: str) -> bool:
    """Keyword present under its canonical name or any synonym. Short aliases (≤2
    chars, e.g. 'js', 'go') are tried only as UPPER / Capitalized — never lowercase,
    so the English word "go" never counts as the language Go."""
    for a in _aliases(canonical):
        forms = {a} if len(a) > 2 else {a.upper(), a[:1].upper() + a[1:]}
        if any(has_term(f, text) for f in forms):
            return True
    return False


def detex(tex: str) -> str:
    """Rough LaTeX → text for keyword matching (body only, commands stripped)."""
    body = tex.split(BEGIN_DOC, 1)[1] if BEGIN_DOC in tex else tex
    body = re.sub(r"(?<!\\)%.*", " ", body)
    body = body.replace(r"\&", "&").replace(r"\%", "%").replace(r"\$", "$").replace(r"\#", "#")
    body = re.sub(r"\\[a-zA-Z]+\*?", " ", body)
    return re.sub(r"[{}\[\]]", " ", body)


def score_keywords(kw: dict, jd_text: str, base_texts: dict) -> dict:
    """Plan §4 Step 6. kw={must_have, nice_to_have}; base_texts={variant: tex or text}.
    Returns {score|None, must_hit, must_total, missing_must, covered, py_variant_guess,
    keywords, dropped, per_base}."""
    dropped, must, nice, seen = [], [], [], set()
    for bucket, src in ((must, kw.get("must_have", [])), (nice, kw.get("nice_to_have", []))):
        for k in (part for term in src for part in split_slash(term)):
            if not has_term(k, jd_text):          # Haiku can't invent requirements
                dropped.append(k)
                continue
            c = _canon(k)
            if c.lower() in seen:
                continue
            seen.add(c.lower())
            bucket.append(c)
    keywords = {"must_have": must, "nice_to_have": nice}
    out = {"score": None, "must_hit": 0, "must_total": len(must), "missing_must": [],
           "covered": [], "py_variant_guess": None, "keywords": keywords,
           "dropped": dropped, "per_base": {}}
    if len(must) + len(nice) < int(agent_cfg("min_keywords")):
        return out  # thin JD → Match n/a
    denom = len(must) + 0.5 * len(nice)
    best = None
    for name, tex in base_texts.items():
        text = detex(tex) if "\\" in tex else tex   # plain text: keep "95%" lines intact
        mh = [k for k in must if has_keyword(k, text)]
        nh = [k for k in nice if has_keyword(k, text)]
        s = (len(mh) + 0.5 * len(nh)) / denom
        out["per_base"][name] = round(s, 4)
        if best is None or s > best[0]:
            best = (s, name, mh, nh)
    if best:
        s, name, mh, nh = best
        out.update(score=s, must_hit=len(mh), py_variant_guess=name, covered=mh + nh,
                   missing_must=[k for k in must if k not in mh])
    return out


# ── Post-check (Step 9) ──────────────────────────────────────────────────────

def _tailor_log_keywords(workdir) -> list:
    """Fallback keyword list when Haiku failed: the tailor's own labeled line."""
    try:
        log = (Path(workdir) / "claude_tailor.log").read_text()
    except Exception:
        return []
    m = None
    for m in re.finditer(r"Extracted Keywords:?\**:?\s*(.+)", log):
        pass
    return [k.strip(" *.") for k in m.group(1).split(",") if k.strip(" *.")] if m else []


def verify_tailored_pdf(pdf, tex, keywords: dict | None) -> dict:
    """Re-run the ATS checks on the tailored PDF and count keyword coverage.
    ok=False only when a critical check (text_extractable, contact_found) fails."""
    text = pdf_text(pdf)
    tex_src = Path(tex).read_text() if tex and Path(tex).exists() else ""
    ev = evaluate_extraction(text, tex_src, compiled=bool(pdf) and Path(pdf).exists())
    if keywords:
        terms = list(keywords.get("must_have", [])) + list(keywords.get("nice_to_have", []))
    else:
        terms = [_canon(k) for k in _tailor_log_keywords(Path(pdf).parent)]
    hit = [k for k in terms if has_keyword(k, text)]
    return {"ok": all(ev["checks"][c] for c in CRITICAL_CHECKS),
            "notes": [c for c in ev["failed"] if c not in CRITICAL_CHECKS],
            "failed": ev["failed"], "kw_in_final": len(hit), "kw_total": len(terms),
            "kw_missing": [k for k in terms if k not in hit]}


# ── Verdict (Step 10) ────────────────────────────────────────────────────────

def detect_base(tex: str, base_texts: dict) -> str | None:
    """Which base the LLM started from: highest share of shared non-trivial lines."""
    lines = lambda t: {l.strip() for l in t.splitlines() if len(l.strip()) > 20}
    tl = lines(tex)
    best = max(base_texts.items(), key=lambda kv: len(tl & lines(kv[1])), default=None)
    return best[0] if best and tl & lines(best[1]) else None


def short_variant(name: str | None) -> str:
    if not name:
        return "?"
    return Path(name).stem.replace("Resume_", "").replace("_General", "")


def build_verdict(*, ats_status: str, match: dict | None, base: str | None,
                  post: dict | None, xyz: tuple | None = None, model: str = "",
                  attempts: int | None = None, escalated: bool = False) -> str:
    """One caption line, ≤400 chars, e.g.
    ATS ✓ · Match 72% (13/18 must) · Base: SWE (py: SWE) · Keywords in final: 22/25 ·
    XYZ 6/11 · Sonnet, 2 tries · Post-check ✓"""
    parts = ["ATS ✓ (repaired)" if ats_status == "yellow" else "ATS ✓"]
    if match and match.get("score") is not None:
        parts.append(f"Match {round(match['score'] * 100)}% "
                     f"({match['must_hit']}/{match['must_total']} must)")
    else:
        parts.append("Match n/a")
    py = short_variant(match.get("py_variant_guess")) if match and match.get("py_variant_guess") else "?"
    parts.append(f"Base: {short_variant(base)} (py: {py})")
    if post and post.get("kw_total"):
        parts.append(f"Keywords in final: {post['kw_in_final']}/{post['kw_total']}")
    if xyz:
        parts.append(f"XYZ {xyz[0]}/{xyz[1]}")
    if escalated:
        parts.append(f"Opus (escalated), {attempts} tries")
    else:
        m = "Sonnet" if "sonnet" in (model or "").lower() else (model or "?")
        parts.append(f"{m}, {attempts} tr{'y' if attempts == 1 else 'ies'}")
    if post:
        parts.append("Post-check ✓" if post.get("ok") and not post.get("notes")
                     else ("Post-check ⚠ " + ",".join(post.get("notes", [])) if post.get("ok")
                           else "Post-check ✗"))
    return " · ".join(parts)[:400]
