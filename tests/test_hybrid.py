#!/usr/bin/env python3
"""Offline tests for the hybrid resume pipeline (HYBRID_PIPELINE_PLAN.md §10).
`claude -p`, Telegram and the Big Interview MCP are mocked; nothing touches data/.
Run: python3 -m unittest tests.test_hybrid
"""
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import resume_agents as RA  # noqa: E402

PRE = "\\documentclass{article}\n\\usepackage[margin=0.5in]{geometry}\n"
BODY_SECTIONS = ["Summary", "Education", "Skills", "Work Experience", "Projects"]


def make_tex(body_extra="", pre=PRE):
    secs = "\n".join(f"\\section*{{{s}}}\nText for {s}.\n\\vspace{{-3mm}}" for s in BODY_SECTIONS)
    return pre + "\\begin{document}\n" + body_extra + secs + "\n\\end{document}\n"


def make_text(sections=BODY_SECTIONS, words=300, contact=True):
    head = "Dev Patel (555)555-0100 | dev@example.com\n" if contact else "Dev Patel\n"
    filler = " ".join(["word"] * words)
    return head + "\n".join(f"{s}\n{filler if i == 0 else ''}" for i, s in enumerate(sections))


def ok_run(stdout=""):
    return {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False, "duration": 0.1}


class TestEvaluateExtraction(unittest.TestCase):
    def test_all_pass(self):
        r = RA.evaluate_extraction(make_text(), make_tex())
        self.assertEqual(r["failed"], [])

    def test_layout_lint_fails(self):
        for bad in ("\\begin{tabular}{ll}a&b\\end{tabular}", "\\begin{multicols}{2}x\\end{multicols}",
                    "\\includegraphics{x.png}", "\\begin{minipage}{1in}x\\end{minipage}"):
            r = RA.evaluate_extraction(make_text(), make_tex(bad))
            self.assertIn("tex_layout_lint", r["failed"], bad)

    def test_layout_lint_ignores_preamble(self):
        r = RA.evaluate_extraction(make_text(), make_tex(pre=PRE + "% tabular minipage\n\\usepackage{graphicx}\n"))
        self.assertNotIn("tex_layout_lint", r["failed"])

    def test_compiles(self):
        self.assertIn("compiles", RA.evaluate_extraction(make_text(), make_tex(), compiled=False)["failed"])

    def test_text_extractable(self):
        r = RA.evaluate_extraction("tiny", make_tex())
        self.assertIn("text_extractable", r["failed"])

    def test_contact(self):
        self.assertIn("contact_found", RA.evaluate_extraction(make_text(contact=False), make_tex())["failed"])
        no_phone = make_text().replace("(555)555-0100", "")
        self.assertIn("contact_found", RA.evaluate_extraction(no_phone, make_tex())["failed"])

    def test_sections_present(self):
        r = RA.evaluate_extraction(make_text(sections=BODY_SECTIONS[:-1]), make_tex())
        self.assertIn("sections_present", r["failed"])

    def test_reading_order(self):
        shuffled = ["Education", "Summary"] + BODY_SECTIONS[2:]
        r = RA.evaluate_extraction(make_text(sections=shuffled), make_tex())
        self.assertIn("reading_order", r["failed"])
        self.assertNotIn("sections_present", r["failed"])

    def test_word_count(self):
        self.assertIn("word_count_sane", RA.evaluate_extraction(make_text(words=50), make_tex())["failed"])
        self.assertIn("word_count_sane", RA.evaluate_extraction(make_text(words=1500), make_tex())["failed"])


class TestPreambleGuard(unittest.TestCase):
    def test_accepts_body_edit(self):
        old = make_tex()
        self.assertTrue(RA.preamble_unchanged(old, old.replace("Text for Skills", "Python, AWS")))

    def test_rejects_preamble_edit(self):
        old = make_tex()
        self.assertFalse(RA.preamble_unchanged(old, old.replace("margin=0.5in", "margin=0.3in")))

    def test_rejects_vspace_edit_and_missing_begin(self):
        old = make_tex()
        self.assertFalse(RA.preamble_unchanged(old, old.replace("\\vspace{-3mm}", "\\vspace{-5mm}", 1)))
        self.assertFalse(RA.preamble_unchanged(old, old.replace("\\begin{document}", "")))


class AuditBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "templates"
        self.src.mkdir()
        (self.src / "A.tex").write_text(make_tex())
        self._paths = (RA.AUDIT_FILE, RA.AUDIT_CACHE)
        RA.AUDIT_FILE = self.tmp / "ats_audit.json"
        RA.AUDIT_CACHE = self.tmp / "cache"

    def tearDown(self):
        RA.AUDIT_FILE, RA.AUDIT_CACHE = self._paths
        shutil.rmtree(self.tmp, ignore_errors=True)


GREEN = {"checks": {}, "failed": [], "words": 300}
RED = {"checks": {}, "failed": ["contact_found"], "words": 300}


class TestAuditCache(AuditBase):
    def test_green_then_cache_hit(self):
        with patch.object(RA, "run_ats_checks", return_value=GREEN) as chk:
            r1 = RA.audit_bases_cached(["A.tex"], self.src)
            r2 = RA.audit_bases_cached(["A.tex"], self.src)
        self.assertEqual(r1["A.tex"]["status"], "green")
        self.assertTrue(r2["A.tex"].get("cached"))
        self.assertEqual(chk.call_count, 1)

    def test_changed_sha_reaudits(self):
        with patch.object(RA, "run_ats_checks", return_value=GREEN) as chk:
            RA.audit_bases_cached(["A.tex"], self.src)
            (self.src / "A.tex").write_text(make_tex("changed\n"))
            RA.audit_bases_cached(["A.tex"], self.src)
        self.assertEqual(chk.call_count, 2)

    def test_30_day_expiry(self):
        with patch.object(RA, "run_ats_checks", return_value=GREEN) as chk:
            RA.audit_bases_cached(["A.tex"], self.src)
            data = json.loads(RA.AUDIT_FILE.read_text())
            data["A.tex"]["ts"] = (datetime.now() - timedelta(days=31)).isoformat()
            RA.AUDIT_FILE.write_text(json.dumps(data))
            RA.audit_bases_cached(["A.tex"], self.src)
        self.assertEqual(chk.call_count, 2)

    def test_missing_base_is_red(self):
        r = RA.audit_bases_cached(["Nope.tex"], self.src)
        self.assertEqual(r["Nope.tex"]["status"], "red")


class TestRepairLoop(AuditBase):
    def test_repair_yellow_never_touches_template(self):
        original = (self.src / "A.tex").read_text()

        def fake_claude(prompt, cwd, **kw):
            p = Path(cwd) / "A.tex"
            p.write_text(p.read_text().replace("Text for Summary", "Fixed summary"))
            return ok_run()

        with patch.object(RA, "run_ats_checks", side_effect=[RED, GREEN]), \
             patch.object(RA.claude_runner, "run_claude_p", side_effect=fake_claude):
            r = RA.audit_bases_cached(["A.tex"], self.src)["A.tex"]
        self.assertEqual(r["status"], "yellow")
        self.assertIn("Fixed summary", Path(r["usable_tex"]).read_text())
        self.assertTrue(str(r["usable_tex"]).startswith(str(RA.AUDIT_CACHE)))
        self.assertEqual((self.src / "A.tex").read_text(), original)

    def test_preamble_edit_rejected_then_red(self):
        def evil(prompt, cwd, **kw):
            p = Path(cwd) / "A.tex"
            p.write_text(p.read_text().replace("margin=0.5in", "margin=0.1in"))
            return ok_run()

        with patch.object(RA, "run_ats_checks", return_value=RED) as chk, \
             patch.object(RA.claude_runner, "run_claude_p", side_effect=evil) as cl:
            r = RA.audit_bases_cached(["A.tex"], self.src)["A.tex"]
        self.assertEqual(r["status"], "red")
        self.assertEqual(cl.call_count, 3)            # bounded by ats_fix_attempts
        self.assertEqual(chk.call_count, 1)           # rejected edits never re-checked
        self.assertEqual(r["report"]["guard_rejections"], 3)
        self.assertFalse(json.loads(RA.AUDIT_FILE.read_text()).get("A.tex"))  # red not cached


@unittest.skipUnless(shutil.which("pdflatex") and shutil.which("pdftotext"), "needs TeX")
class TestRealBases(unittest.TestCase):
    def test_real_bases_pass(self):
        import tailor
        bases = [b for b in tailor.BASE_VARIANTS if (tailor.RESUMEBOT / b).exists()]
        if not bases:
            self.skipTest("private base templates not present (gitignored)")
        for b in bases:
            self.assertEqual(RA.run_ats_checks(tailor.RESUMEBOT / b)["failed"], [], b)


class TestParseKeywords(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(RA._parse_keywords_json('{"must_have": ["Python"], "nice_to_have": ["Go"]}'),
                         {"must_have": ["Python"], "nice_to_have": ["Go"]})

    def test_prose_wrapped(self):
        txt = 'Sure! {not json} Here:\n```json\n{"must_have": ["AWS", " "], "nice_to_have": []}\n```'
        self.assertEqual(RA._parse_keywords_json(txt), {"must_have": ["AWS"], "nice_to_have": []})

    def test_garbage(self):
        self.assertIsNone(RA._parse_keywords_json("no json here {oops"))
        self.assertIsNone(RA._parse_keywords_json('{"foo": 1}'))


class TestExtractKeywords(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "job_description.txt").write_text("We need Python and AWS.")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_second_attempt_succeeds_and_caches(self):
        runs = [ok_run("garbage"), ok_run('{"must_have":["Python"],"nice_to_have":["AWS"]}')]
        with patch.object(RA.claude_runner, "run_claude_p", side_effect=runs) as cl:
            r = RA.extract_keywords({}, self.d)
            self.assertEqual(r["must_have"], ["Python"])
            self.assertEqual(cl.call_args.kwargs["model"], "claude-haiku-5-5")
            RA.extract_keywords({}, self.d)            # cached → no 3rd call
        self.assertEqual(cl.call_count, 2)

    def test_cache_invalidated_when_jd_changes(self):
        run = ok_run('{"must_have":["Python"],"nice_to_have":[]}')
        with patch.object(RA.claude_runner, "run_claude_p", return_value=run) as cl:
            RA.extract_keywords({}, self.d)
            (self.d / "job_description.txt").write_text("Now we need Rust.")
            RA.extract_keywords({}, self.d)
        self.assertEqual(cl.call_count, 2)

    def test_both_fail_degrades(self):
        with patch.object(RA.claude_runner, "run_claude_p", return_value=ok_run("nope")) as cl:
            r = RA.extract_keywords({}, self.d)
        self.assertIn("error", r)
        self.assertEqual(cl.call_count, 2)


class TestScoreKeywords(unittest.TestCase):
    JD = ("Required: Python, AWS, Kubernetes, Terraform. Preferred: Go. "
          "Also Java and k8s and postgres and R.")

    def test_worked_example_44(self):
        kw = {"must_have": ["Python", "AWS", "Kubernetes", "Terraform"], "nice_to_have": ["Go"]}
        r = RA.score_keywords(kw, self.JD, {"B": "Skills: Python, AWS"})
        self.assertAlmostEqual(r["score"], 2 / 4.5)
        self.assertEqual(round(r["score"] * 100), 44)
        self.assertEqual(r["missing_must"], ["Kubernetes", "Terraform"])

    def test_drops_keywords_not_in_jd(self):
        kw = {"must_have": ["Python", "AWS", "Kubernetes", "Terraform", "Rust"], "nice_to_have": ["Go"]}
        r = RA.score_keywords(kw, self.JD, {"B": "Python"})
        self.assertEqual(r["dropped"], ["Rust"])
        self.assertNotIn("Rust", r["keywords"]["must_have"])

    def test_synonyms_dedupe_and_match(self):
        kw = {"must_have": ["k8s", "Kubernetes", "postgres", "Python", "AWS"], "nice_to_have": ["Terraform"]}
        r = RA.score_keywords(kw, self.JD, {"B": "Kubernetes, PostgreSQL"})
        self.assertEqual(r["keywords"]["must_have"], ["Kubernetes", "PostgreSQL", "Python", "AWS"])
        self.assertEqual(r["must_hit"], 2)

    def test_word_boundaries(self):
        self.assertFalse(RA.has_keyword("Java", "JavaScript and TypeScript"))
        self.assertTrue(RA.has_keyword("Java", "Core Java, Python"))
        self.assertFalse(RA.has_keyword("Go", "we go to Google to get going"))
        self.assertTrue(RA.has_keyword("Go", "Languages: Go, C++"))
        self.assertFalse(RA.has_keyword("R", "Research, React, our R&D... ROI"[0:20]))
        self.assertTrue(RA.has_keyword("R", "Languages: Python, R, SQL"))
        self.assertTrue(RA.has_keyword("Natural Language Processing", "Natural Language\nProcessing"))
        self.assertTrue(RA.has_keyword("Machine Learning", "Machine  Learning"))
        self.assertFalse(RA.has_keyword("C", "Languages: C++, C#"))
        self.assertTrue(RA.has_keyword("C", "Languages: C, C++"))
        self.assertTrue(RA.has_keyword("C++", "Languages: C++."))
        self.assertTrue(RA.has_keyword("C#", "C#, Java"))

    def test_slash_terms_split_before_scoring(self):
        jd = "Need JavaScript/TypeScript, ML/AI concepts, CI/CD, React and Git and SQL."
        kw = {"must_have": ["JavaScript/TypeScript", "ML/AI concepts", "CI/CD", "React", "Git"],
              "nice_to_have": ["SQL"]}
        r = RA.score_keywords(kw, jd, {"B": "JavaScript, TypeScript, Machine Learning, Git"})
        self.assertEqual(r["keywords"]["must_have"],
                         ["JavaScript", "TypeScript", "Machine Learning", "AI concepts", "CI/CD", "React", "Git"])
        self.assertEqual(r["must_hit"], 4)
        self.assertEqual(RA.split_slash("CI/CD"), ["CI/CD"])

    def test_better_of_two_bases(self):
        kw = {"must_have": ["Python", "AWS", "Kubernetes", "Terraform", "Java"], "nice_to_have": []}
        r = RA.score_keywords(kw, self.JD, {"SWE.tex": "Python, Java",
                                            "AIML.tex": "Python, AWS, Kubernetes"})
        self.assertEqual(r["py_variant_guess"], "AIML.tex")
        self.assertAlmostEqual(r["score"], 3 / 5)

    def test_thin_jd_is_none(self):
        kw = {"must_have": ["Python", "AWS"], "nice_to_have": ["Go"]}
        r = RA.score_keywords(kw, self.JD, {"B": "Python"})
        self.assertIsNone(r["score"])

    def test_plain_text_percent_kept(self):
        kw = {"must_have": ["Python", "AWS", "Kubernetes", "Terraform", "Java"], "nice_to_have": []}
        txt = "Achieved 95% coverage using Python, AWS, Kubernetes, Terraform and Java."
        self.assertEqual(RA.score_keywords(kw, self.JD, {"B": txt})["score"], 1.0)

    def test_detex_matches_latex_source(self):
        kw = {"must_have": ["Python", "AWS", "Kubernetes", "Terraform"], "nice_to_have": ["Go"]}
        tex = PRE + "\\begin{document}\n\\textbf{Skills:} Python, AWS \\& Terraform\n\\end{document}"
        self.assertAlmostEqual(RA.score_keywords(kw, self.JD, {"B": tex})["score"], 3 / 4.5)


class TestVerifyAndVerdict(unittest.TestCase):
    def test_verify_critical_vs_advisory(self):
        d = Path(tempfile.mkdtemp())
        try:
            pdf, tex = d / "r.pdf", d / "r.tex"
            pdf.write_bytes(b"%PDF"); tex.write_text(make_tex())
            kw = {"must_have": ["Python", "AWS"], "nice_to_have": ["Go"]}
            with patch.object(RA, "pdf_text", return_value=make_text() + "\nPython AWS"):
                r = RA.verify_tailored_pdf(pdf, tex, kw)
            self.assertTrue(r["ok"])
            self.assertEqual((r["kw_in_final"], r["kw_total"]), (2, 3))
            with patch.object(RA, "pdf_text", return_value=make_text(contact=False)):
                self.assertFalse(RA.verify_tailored_pdf(pdf, tex, kw)["ok"])
            with patch.object(RA, "pdf_text", return_value=make_text(words=1500)):
                r = RA.verify_tailored_pdf(pdf, tex, kw)
            self.assertTrue(r["ok"]); self.assertIn("word_count_sane", r["notes"])
            # Haiku failed → fall back to the tailor's own labeled keyword line
            (d / "claude_tailor.log").write_text("**Extracted Keywords:** Python, Rust, AWS\n")
            with patch.object(RA, "pdf_text", return_value=make_text() + " Python AWS"):
                r = RA.verify_tailored_pdf(pdf, tex, None)
            self.assertEqual((r["kw_in_final"], r["kw_total"]), (2, 3))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_verdict_full(self):
        v = RA.build_verdict(ats_status="green",
                             match={"score": 0.72, "must_hit": 13, "must_total": 18,
                                    "py_variant_guess": "Resume_SWE_General.tex"},
                             base="Resume_SWE_General.tex",
                             post={"ok": True, "notes": [], "kw_in_final": 22, "kw_total": 25},
                             xyz=(6, 11), model="claude-sonnet-4-6", attempts=2)
        self.assertEqual(v, "ATS ✓ · Match 72% (13/18 must) · Base: SWE (py: SWE) · "
                            "Keywords in final: 22/25 · XYZ 6/11 · Sonnet, 2 tries · Post-check ✓")

    def test_verdict_variants_and_cap(self):
        v = RA.build_verdict(ats_status="yellow", match=None, base="Resume_AI-ML_General.tex",
                             post={"ok": True, "notes": [], "kw_in_final": 1, "kw_total": 2},
                             model="claude-opus-4-8", attempts=7, escalated=True)
        self.assertIn("ATS ✓ (repaired)", v)
        self.assertIn("Match n/a", v)
        self.assertIn("Opus (escalated)", v)
        long = RA.build_verdict(ats_status="green", match=None, base="X" * 600, post=None)
        self.assertLessEqual(len(long), 400)

    def test_detect_base(self):
        a = "line one of the SWE base resume here\nanother long SWE specific line here"
        b = "line one of the AIML base resume here\nanother long AIML specific line here"
        self.assertEqual(RA.detect_base(a + "\nnew tailored line here ok", {"A": a, "B": b}), "A")


# ── Phase 2: tailor ──────────────────────────────────────────────────────────
import tailor as T  # noqa: E402

TODAY_PROMPT_HEAD = "Read job_description.txt in this directory. Following your resume "
TODAY_PROMPT_TAIL = ("compilation and one-page verification are handled automatically "
                     "after you finish.")


class TailorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tpl = self.tmp / "tpl"
        self.tpl.mkdir()
        for b in T.BASE_VARIANTS:
            (self.tpl / b).write_text(make_tex())
        (self.tpl / "system_instructions.md").write_text("SYS")
        self._saved = (T.RESUMEBOT, T.APPS, T.RESUMES, T.INSTRUCTIONS, dict(T.BASE_OVERRIDES))
        T.RESUMEBOT, T.APPS, T.RESUMES = self.tpl, self.tmp / "apps", self.tmp / "resumes"
        T.INSTRUCTIONS = self.tpl / "system_instructions.md"
        T.BASE_OVERRIDES.clear()
        self.job = {"job_id": "abcdef123", "title": "ML Intern", "company": "Foo",
                    "description": "Python AWS", "location": "Boston, MA, United States"}
        self.prompts = []
        p1 = patch.object(T, "compile_tex", side_effect=self.fake_compile)
        p2 = patch.object(T.time, "sleep", lambda *_: None)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def tearDown(self):
        T.RESUMEBOT, T.APPS, T.RESUMES, T.INSTRUCTIONS, ov = self._saved
        T.BASE_OVERRIDES.clear(); T.BASE_OVERRIDES.update(ov)
        shutil.rmtree(self.tmp, ignore_errors=True)

    pages = 1

    def fake_compile(self, tex):
        pdf = Path(tex).with_suffix(".pdf")
        pdf.write_bytes(b"%PDF")
        return pdf, self.pages


class TestTailor(TailorBase):
    def writer(self, prompt, cwd, **kw):
        self.prompts.append(prompt)
        (Path(cwd) / f"{T.expected_basename(self.job)}.tex").write_text(make_tex("tailored\n"))
        return ok_run()

    def test_keywords_none_prompt_is_todays(self):
        with patch.object(T.claude_runner, "run_claude_p", side_effect=self.writer):
            r = T.tailor_with_claude(self.job)
        self.assertTrue(r["ok"])
        p = self.prompts[0]
        self.assertTrue(p.startswith(TODAY_PROMPT_HEAD))
        self.assertTrue(p.endswith(TODAY_PROMPT_TAIL))
        self.assertNotIn("KEYWORDS", p)
        self.assertNotIn("XYZ", p)
        self.assertEqual(p, T._GEN_PROMPT.format(tex_name=T.expected_basename(self.job) + ".tex"))

    def test_keywords_prompt_block(self):
        kw = {"must_have": ["Python", "AWS", "Kubernetes"], "nice_to_have": ["Go"],
              "covered": ["Python", "AWS"]}
        with patch.object(T.claude_runner, "run_claude_p", side_effect=self.writer):
            T.tailor_with_claude(self.job, keywords=kw)
        p = self.prompts[0]
        self.assertIn("KEYWORDS (pre-extracted — use these, do NOT run your own extraction):", p)
        self.assertIn("Must-have: Python, AWS, Kubernetes", p)
        self.assertIn("Nice-to-have: Go", p)
        self.assertIn("Already in base: Python, AWS", p)
        self.assertIn("Missing from base: Kubernetes, Go", p)
        self.assertIn("NEVER invent a number", p)
        self.assertIn("TOP 2 bullets", p)

    def test_stage_uses_repaired_base(self):
        fixed = self.tmp / "fixed.tex"
        fixed.write_text("REPAIRED")
        T.BASE_OVERRIDES[T.BASE_VARIANTS[0]] = str(fixed)
        d = T.stage(self.job)
        self.assertEqual((d / T.BASE_VARIANTS[0]).read_text(), "REPAIRED")
        self.assertNotEqual((d / T.BASE_VARIANTS[1]).read_text(), "REPAIRED")
        self.assertEqual((self.tpl / T.BASE_VARIANTS[0]).read_text(), make_tex())  # template untouched


class TestXyzStats(unittest.TestCase):
    def test_counts(self):
        tex = (PRE + "\\begin{document}\n\\begin{itemize}\n\\item Cut latency 40\\% by caching\n"
               "\\item Built API with Flask\n\\item Saved \\$2K\n\\item 3x faster\n"
               "\\end{itemize}\n\\end{document}")
        self.assertEqual(T.xyz_bullet_stats(tex), (3, 4))
        tex2 = (PRE + "\\begin{document}\n\\begin{itemize}\n\\item Built API % 2024 note\n"
                "\\vspace{-1mm}\n\\item Cut cost 30\\%\n\\end{itemize}\n\\end{document}")
        self.assertEqual(T.xyz_bullet_stats(tex2), (1, 2))


class TestRetailor(TailorBase):
    def setUp(self):
        super().setUp()
        self.d = T.workdir_for(self.job)
        (self.d / "job_description.txt").write_text("JD")
        self.v1 = self.d / "Resume_MLIntern_Foo.tex"
        self.v1.write_text(make_tex())

    def editor(self, change):
        def run(prompt, cwd, **kw):
            self.prompts.append(prompt)
            p = Path(cwd) / "Resume_MLIntern_Foo.tex"
            p.write_text(change(p.read_text()))
            return ok_run()
        return run

    def test_copies_vN_to_vN1_with_only_picked_items(self):
        ed = self.editor(lambda t: t.replace("Text for Skills", "Kubernetes"))
        with patch.object(T.claude_runner, "run_claude_p", side_effect=ed):
            r = T.retailor(self.job, self.v1, ["Kubernetes"], ["Quantify impact"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["version"], 2)
        self.assertEqual(Path(r["tex"]).parent.name, "v2")
        self.assertEqual(self.v1.read_text(), make_tex())          # v1 kept unchanged
        p = self.prompts[0]
        self.assertIn("current GPA 3.93/4", p)
        self.assertIn("location for the header line: Boston, MA", p)
        self.assertIn("Keywords to integrate", p)
        self.assertIn("Kubernetes", p)
        self.assertIn("Quantify impact", p)
        self.assertNotIn("Terraform", p)
        # v2 → v3 lands next to v2, not nested
        with patch.object(T.claude_runner, "run_claude_p",
                          side_effect=self.editor(lambda t: t.replace("Kubernetes", "Kubernetes, Go"))):
            r3 = T.retailor(self.job, r["tex"], ["Go"], [])
        self.assertEqual(Path(r3["tex"]).parent, self.d / "v3")

    def test_preamble_edit_rejected(self):
        ed = self.editor(lambda t: t.replace("margin=0.5in", "margin=0.2in"))
        with patch.object(T.claude_runner, "run_claude_p", side_effect=ed) as cl:
            r = T.retailor(self.job, self.v1, ["Kubernetes"], [])
        self.assertFalse(r["ok"])
        self.assertEqual(r["fail_code"], "edit_rejected")
        self.assertEqual(cl.call_count, T.max_total_attempts() - T.ESCALATION_ATTEMPTS)
        self.assertEqual(self.v1.read_text(), make_tex())

    def test_header_location(self):
        self.assertEqual(T.header_location({"location": "Boston, Massachusetts, United States"}),
                         "Boston, Massachusetts")
        for loc in ("Multiple Locations", "", "Remote"):
            self.assertIn("unchanged", T.header_location({"location": loc}))

    def test_latest_pdf_restores_newest_validated_version(self):
        for v in ("v2", "v3"):
            (self.d / v).mkdir()
            (self.d / v / "Resume_MLIntern_Foo.pdf").write_bytes(b"%PDF")
        (self.d / "Resume_MLIntern_Foo.pdf").write_bytes(b"%PDF")
        (self.d / "v2" / T.VALIDATED).touch()     # v3 never passed the post-check
        one = unittest.mock.Mock(stdout="Pages:          1\n")
        with patch.object(T.subprocess, "run", return_value=one):
            got = T.latest_pdf(self.job)
        self.assertEqual(Path(got), self.d / "v2" / "Resume_MLIntern_Foo.pdf")
        self.assertEqual(T.version_of(got), 2)

    def test_nothing_picked(self):
        with patch.object(T.claude_runner, "run_claude_p") as cl:
            r = T.retailor(self.job, self.v1, [], [])
        self.assertEqual(r["fail_code"], "nothing_picked")
        cl.assert_not_called()


class TestSystemInstructions(unittest.TestCase):
    def test_sections(self):
        s = (ROOT / "resume_templates" / "system_instructions.md").read_text()
        for h in ("### 4C — XYZ Bullets", "### 4D — Work Experience", "### 4E — Projects",
                  "### 4F — Education"):
            self.assertIn(h, s)
        self.assertIn("MUST integrate every keyword", s)          # D12 kept as-is
        self.assertIn("use it as the extracted list and skip extraction", s)

    def test_no_summary_and_project_bank(self):
        s = (ROOT / "resume_templates" / "system_instructions.md").read_text()
        self.assertIn("### 4A — Headline (NO Summary section)", s)
        self.assertNotIn("Summary is under 50 words", s)
        self.assertIn("NO Summary section", T.gen_prompt("R.tex"))
        self.assertNotIn("trim the Summary", T._trim_prompt("R.tex", 2))
        with tempfile.TemporaryDirectory() as td:
            ins, bank = Path(td) / "i.md", Path(td) / "b.md"
            ins.write_text("INSTR")
            with patch.object(T, "INSTRUCTIONS", ins), patch.object(T, "PROJECT_BANK", bank):
                self.assertTrue(T.load_instructions().startswith("INSTR"))  # no bank
                self.assertNotIn("Project Bank", T.load_instructions())
                self.assertIn("ignore every compile", T.load_instructions())
                bank.write_text("## APPENDIX: Project Bank")
                self.assertIn("## APPENDIX: Project Bank", T.load_instructions())

    @unittest.skipUnless(shutil.which("pdflatex") and shutil.which("pdftotext"), "needs TeX")
    def test_page2_spill_feeds_trim_prompt(self):
        base = ROOT / "resume_templates" / "Resume_AI-ML_General.tex"
        if not base.exists():
            self.skipTest("personal template not present")
        with tempfile.TemporaryDirectory() as td:
            ok = Path(td) / "ok.tex"; ok.write_text(base.read_text())
            pdf, pages = T.compile_tex(ok)
            self.assertEqual((pages, T.page2_spill(pdf)[0]), (1, 0))
            over = Path(td) / "over.tex"
            over.write_text(base.read_text().replace(
                "\\end{document}", "\\vspace*{40mm}SPILLMARK\n\\end{document}"))
            pdf, pages = T.compile_tex(over)
            n, text = T.page2_spill(pdf)
            self.assertEqual(pages, 2)
            self.assertGreater(n, 0)
            self.assertIn("SPILLMARK", text)
            prompt = T._trim_prompt("R.tex", 2, n, text)
            self.assertIn(f"free AT LEAST {n} whole", prompt)
            self.assertIn("SPILLMARK", prompt)


# ── Phase 3: Telegram ────────────────────────────────────────────────────────
import telegram_bot as TB  # noqa: E402

JOB = {"job_id": "a" * 32, "title": "ML Co-op", "company": "Acme",
       "salary": "500–500 per week", "posted": "2026-10-10", "deadline": "2026-10-20",
       "apply_urls": "", "location": "Boston"}


def cq(data, i=1):
    return {"id": f"cq{i}", "data": data}


class TestPostingCard(unittest.TestCase):
    def test_all_fields(self):
        t = TB.posting_card_text(JOB)
        for frag in ("🆕 New posting", "<b>ML Co-op</b>", "Acme", "💵 Pay: $500 per week",
                     "📅 Posted: 2026-10-10", "🗓 Apply by: 2026-10-20",
                     "🌐 External apply: No", "/students/app/jobs/detail/" + "a" * 32):
            self.assertIn(frag, t)
        self.assertNotIn("Boston", t)                     # location dropped
        t2 = TB.posting_card_text({**JOB, "apply_urls": "https://x.io/a; https://y.io/b",
                                   "salary": "", "posted": ""})
        self.assertIn("External apply: Yes — https://x.io/a  https://y.io/b", t2)
        self.assertIn("Pay: Not listed", t2)
        self.assertIn("Posted: —", t2)

    def test_posting_url_is_detail_page(self):
        self.assertEqual(TB.job_posting_url("123"),
                         "https://northeastern-csm.symplicity.com/students/app/jobs/detail/123")

    def test_callback_data_fits_64_bytes(self):
        n = "abcd1234"
        for a in ("approve", "reject", "lowok", "lowno", "scan", "retailor", "back"):
            self.assertLessEqual(len(f"{a}:{JOB['job_id']}:{n}".encode()), 64)
        self.assertLessEqual(len(f"tg:{JOB['job_id']}:{n}:19".encode()), 64)


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.d = TB.CallbackDispatcher()
        self.acks = []
        p = patch.object(TB, "answer_callback", lambda i, t="": self.acks.append((i, t)))
        p.start(); self.addCleanup(p.stop)

    def test_routes_two_jobs_to_right_queue(self):
        qa, qb = self.d.expect("A", "n1"), self.d.expect("B", "n2")
        self.d.route(cq("approve:B:n2", 1))
        self.d.route(cq("reject:A:n1", 2))
        self.assertEqual(qa.get_nowait()[0], "reject")
        self.assertEqual(qb.get_nowait()[0], "approve")

    def test_stale_nonce_acked_expired(self):
        q = self.d.expect("A", "new")
        self.d.route(cq("approve:A:old", 9))
        self.assertTrue(q.empty())
        self.assertEqual(self.acks, [])                         # held for the grace period
        self.d.sweep(now=TB.time.time() + 11)
        self.assertEqual(self.acks, [("cq9", TB.EXPIRED)])
        self.d.expect("A", "old")                               # too late — not delivered
        self.assertTrue(self.d._queues[("A", "old")].empty())

    def test_tap_before_registration_is_delivered(self):
        self.d.route(cq("approve:X:n", 3))                      # card sent w/o _expect
        q = self.d.expect("X", "n")
        self.assertEqual(q.get_nowait()[0], "approve")
        self.assertEqual(self.acks, [])

    def test_toggle_goes_to_handler(self):
        got = []
        self.d.on_toggle("A", "n", lambda c, i: got.append(i))
        self.d.route(cq("tg:A:n:3"))
        self.assertEqual(got, [3])
        self.d.release("A", "n")
        self.d.route(cq("tg:A:n:3", 5))
        self.assertEqual(self.acks[-1], ("cq5", TB.EXPIRED))

    def test_wait_for_decision_reads_dispatcher(self):
        with patch.object(TB, "DISPATCHER", self.d), \
             patch.object(type(self.d), "running", property(lambda s: True)), \
             patch.object(TB, "configured", lambda: True), \
             patch.object(TB, "_require", lambda: None):
            q = self.d.expect("A", "n")
            q.put(("approve", cq("approve:A:n", 7)))
            self.assertEqual(TB.wait_for_decision("A", "n", timeout=1), "approve")
            self.assertNotIn(("A", "n"), self.d._queues)          # released after decision
            self.assertEqual(TB.wait_for_decision("B", "n", timeout=0.05), "timeout")
            # refuse() vetoes a tap and keeps waiting
            q = self.d.expect("C", "n")
            q.put(("retailor", cq("retailor:C:n", 1)))
            q.put(("back", cq("back:C:n", 2)))
            r = TB.wait_for_decision("C", "n", timeout=1,
                                     refuse=lambda a: "Select at least one item" if a == "retailor" else None)
            self.assertEqual(r, "back")
            self.assertIn(("cq1", "Select at least one item"), self.acks)


FB = {"medal": "silver", "partial": False,
      "categories": {
          "readability": {"badge": "gold", "criteria": [
              {"name": "Length", "status": "needs_work", "detail": "too long", "suggestion": "Shorten"}]},
          "credibility": {"badge": "bronze", "criteria": [
              {"name": "Impact", "status": "needs_work", "detail": "d", "suggestion": "Quantify impact"},
              {"name": "Verbs", "status": "perfect", "detail": "", "suggestion": None}]},
          "ats_fit": {"badge": "silver", "criteria": [
              {"name": "Title", "status": "warning", "detail": "missing", "suggestion": "Add job title to summary"}],
              "keywords_matched": ["Python", "AWS"], "keywords_unmatched": ["Kubernetes", "Terraform"]},
          "format": {"badge": "gold", "criteria": [
              {"name": "Fonts", "status": "warning", "detail": "x", "suggestion": "Use one font"}]}},
      "action_items": [{"category": "format", "item": "Fix margins", "priority": "low"},
                       {"category": "credibility", "item": "Add metrics", "priority": "high"}]}


class TestScoreCard(unittest.TestCase):
    def setUp(self):
        self.acks, self.edits = [], []
        for name, fn in (("answer_callback", lambda i, t="": self.acks.append(t)),
                         ("edit_reply_markup", lambda m, kb: self.edits.append(kb))):
            p = patch.object(TB, name, fn); p.start(); self.addCleanup(p.stop)

    def test_items_only_from_credibility_and_ats_fit(self):
        texts = [i["text"] for i in TB.score_card_items(FB)]
        self.assertEqual(texts, ["Kubernetes", "Terraform", "Quantify impact",
                                 "Add job title to summary", "Add metrics"])
        self.assertNotIn("Shorten", texts)
        self.assertNotIn("Use one font", texts)
        self.assertNotIn("Fix margins", texts)

    def test_text_badges_and_keywords(self):
        c = TB.ScoreCard(JOB, "n", FB, version=2, scans_left=2)
        t = c.text()
        for frag in ("Overall: 🥈 Silver", "Readability  🥇 Gold", "Credibility  🥉 Bronze",
                     "ATS Fit      🥈 Silver", "Format       🥇 Gold", "Keywords matched: 2/4",
                     "Scans left: 2/5", "(v2)"):
            self.assertIn(frag, t)

    def test_toggle_flips_and_refuse_zero(self):
        c = TB.ScoreCard(JOB, "n", FB)
        c.message_id = 1
        self.assertEqual(c.refuse("retailor"), "Select at least one item")
        c.toggle(cq("tg"), 0)
        self.assertEqual(self.edits[-1]["inline_keyboard"][0][0]["text"][0], "☑")
        self.assertIn("Retailor (1 selected)", json.dumps(self.edits[-1], ensure_ascii=False))
        self.assertIsNone(c.refuse("retailor"))
        c.toggle(cq("tg"), 0)
        self.assertEqual(self.edits[-1]["inline_keyboard"][0][0]["text"][0], "☐")
        c.toggle(cq("tg"), 2); c.toggle(cq("tg"), 0)
        self.assertEqual(c.picked(), (["Kubernetes"], ["Quantify impact"]))

    def test_long_corrections_listed_in_full_and_numbered(self):
        fb = json.loads(json.dumps(FB))
        long = "Quantify the impact of the RAG evaluation pipeline with latency and accuracy numbers"
        fb["action_items"].append({"category": "credibility", "item": long})
        c = TB.ScoreCard(JOB, "n", fb)
        n = [i["text"] for i in c.selectable].index(long) + 1
        self.assertIn(f"{n}. ✎ {long}", c.text())
        btn = c.keyboard()["inline_keyboard"][n - 1][0]["text"]
        self.assertTrue(btn.startswith(f"☐ {n}. ✎ "))
        self.assertLess(len(btn), len(long))  # button stays truncated; message has it all

    def test_overflow_over_20(self):
        fb = json.loads(json.dumps(FB))
        fb["categories"]["ats_fit"]["keywords_unmatched"] = [f"kw{i}" for i in range(25)]
        c = TB.ScoreCard(JOB, "n", fb, max_toggles=20)
        toggles = [r for r in c.keyboard()["inline_keyboard"] if r[0]["callback_data"].startswith("tg:")]
        self.assertEqual(len(toggles), 20)
        self.assertIn("(not selectable — too many)", c.text())
        self.assertIn("kw24", c.text())

    def test_overflow_text_bounded(self):
        fb = json.loads(json.dumps(FB))
        fb["categories"]["ats_fit"]["keywords_unmatched"] = [f"keyword number {i} " * 6 for i in range(300)]
        c = TB.ScoreCard(JOB, "n", fb)
        self.assertLess(len(c.text()), 4096)
        self.assertIn("more", c.text().splitlines()[-1] + c.text())

    def test_send_requires_dispatcher(self):
        with patch.object(TB, "_require", lambda: None), \
             patch.object(type(TB.DISPATCHER), "running", property(lambda s: False)):
            with self.assertRaises(RuntimeError):
                TB.send_score_card(JOB, FB, "n")

    def test_partial_no_toggles(self):
        c = TB.ScoreCard(JOB, "n", {**FB, "partial": True})
        kb = c.keyboard()["inline_keyboard"]
        self.assertEqual(len(kb), 1)
        self.assertEqual([b["callback_data"].split(":")[0] for b in kb[0]], ["back"])
        self.assertIn("Feedback incomplete — retailor unavailable", c.text())

    def test_round_limit(self):
        c = TB.ScoreCard(JOB, "n", FB, round_no=3, max_rounds=3)
        self.assertNotIn("retailor:", json.dumps(c.keyboard()))
        self.assertIn("Retailor limit reached (3/3)", c.text())


class TestResumeCard(unittest.TestCase):
    def send(self, **kw):
        sent = {}

        def fake_post(url, data=None, files=None, timeout=None):
            sent.update(data)

            class R:
                def json(self_inner):
                    return {"ok": True, "result": {"message_id": 1}}
            return R()

        d = Path(tempfile.mkdtemp())
        pdf = d / "r.pdf"; pdf.write_bytes(b"%PDF")
        with patch.object(TB, "_require", lambda: None), patch.object(TB.requests, "post", fake_post):
            TB.send_resume_for_approval(str(pdf), JOB, summary="ATS ✓ · Match 72%", nonce="n", **kw)
        shutil.rmtree(d)
        return sent["caption"], json.loads(sent["reply_markup"])

    def test_scan_button_and_verdict(self):
        cap, kb = self.send(scan={"available": True, "left": 3}, version=2, round_no=1,
                            prev_scan="🥈 Silver · ATS Fit: needs work")
        self.assertIn("ATS ✓ · Match 72%", cap)
        self.assertIn("(v2 · retailor 1/3)", cap)
        self.assertIn("Prev scan: 🥈 Silver", cap)
        self.assertEqual(kb["inline_keyboard"][0][2]["text"], "🔍 Scan (3/5 left)")

    def test_scan_hidden_when_logged_out_and_legacy(self):
        cap, kb = self.send(scan={"available": False, "left": None})
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)
        self.assertIn("Scan unavailable: log in via scripts/login.py", cap)
        cap, kb = self.send()
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)
        self.assertNotIn("Scan unavailable", cap)
        self.assertLessEqual(len(cap), 1024)

    def test_caption_fits_without_slicing_entities(self):
        long_job = {**JOB, "apply_urls": "; ".join(f"https://ex.com/{i}?a=1&b=2" for i in range(30)),
                    "company": "A&B " * 30}
        sent = {}

        def fake_post(url, data=None, files=None, timeout=None):
            sent.update(data)

            class R:
                def json(self_inner):
                    return {"ok": True, "result": {"message_id": 1}}
            return R()

        d = Path(tempfile.mkdtemp())
        pdf = d / "r.pdf"; pdf.write_bytes(b"%PDF")
        with patch.object(TB, "_require", lambda: None), patch.object(TB.requests, "post", fake_post):
            TB.send_resume_for_approval(str(pdf), long_job, summary="R&D " * 100, nonce="n",
                                        prev_scan="x&y " * 40, scan={"available": False})
        shutil.rmtree(d)
        cap = sent["caption"]
        self.assertLessEqual(len(cap), 1024)
        self.assertTrue(cap.endswith("Reject to skip."))
        import re as _re
        self.assertFalse(_re.search(r"&(?!amp;|lt;|gt;|quot;|#x27;)", cap))   # no broken entity


class TestLowMatchCard(unittest.TestCase):
    def test_text(self):
        sent = {}
        with patch.object(TB, "_require", lambda: None), \
             patch.object(TB, "_send_text", lambda t, kb, j, n, w: sent.update(t=t, kb=kb) or 1):
            TB.send_low_match_card(JOB, {"score": 0.32, "must_hit": 3, "must_total": 9,
                                         "missing_must": ["Kubernetes", "Terraform"]}, nonce="n")
        self.assertIn("⚠️ Low match: 32% (3/9 must-have)", sent["t"])
        self.assertIn("Missing: Kubernetes, Terraform", sent["t"])
        self.assertEqual([b["callback_data"].split(":")[0] for b in sent["kb"]["inline_keyboard"][0]],
                         ["lowok", "lowno"])


# ── Phase 4: pipeline wiring ─────────────────────────────────────────────────
import threading  # noqa: E402
import pipeline as P  # noqa: E402


_REAL_LOG_DIR = P.LOG_DIR


def setUpModule():
    """Keep pipeline.log() out of the real logs/run_<date>.log."""
    P.LOG_DIR = Path(tempfile.mkdtemp())


def tearDownModule():
    shutil.rmtree(P.LOG_DIR, ignore_errors=True)
    P.LOG_DIR = _REAL_LOG_DIR


class PipeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._files = (P.PENDING_FILE, P.SKIP_FILE, P.SCANS_FILE, P.SCAN_RESET_STAMP)
        P.PENDING_FILE = self.tmp / "pending.json"
        P.SKIP_FILE = self.tmp / "skip.txt"
        P.SCANS_FILE = self.tmp / "scans.json"
        P.SCAN_RESET_STAMP = self.tmp / "reset.txt"
        self.msgs = []
        for target, attr, val in ((P, "log", lambda m: None),
                                  (P, "_msg", lambda t: self.msgs.append(t)),
                                  (P.time, "sleep", lambda s: self.sleeps.append(s)),
                                  (P, "bump_watchdog", lambda x: None)):
            pt = patch.object(target, attr, val); pt.start(); self.addCleanup(pt.stop)
        self.sleeps = []
        self.job = {"job_id": "j1", "title": "ML Co-op", "company": "Acme",
                    "description": "Python AWS", "apply_urls": ""}

    def tearDown(self):
        P.PENDING_FILE, P.SKIP_FILE, P.SCANS_FILE, P.SCAN_RESET_STAMP = self._files
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRunPostings(PipeBase):
    def test_send_all_then_approve_starts_while_other_pending(self):
        sent, order = [], []
        b_released = threading.Event()

        def wait(jid, nonce="", timeout=0, refuse=None):
            if jid == "A":
                return "approve"
            b_released.wait(5)          # B's card stays pending until A's job has started
            order.append("B-decided")
            return "timeout"

        def run_job(job, ctx, dry):
            order.append(f"{job['job_id']}-started")
            b_released.set()
            return P._result(job, "applied")

        with patch.object(P.tg.DISPATCHER, "start", lambda: None), \
             patch.object(P.tg.DISPATCHER, "stop", lambda: None), \
             patch.object(P.tg, "send_job_for_approval", lambda j, nonce="": sent.append(j["job_id"])), \
             patch.object(P.tg, "wait_for_decision", wait), \
             patch.object(P, "_run_job", run_job):
            res = P.run_postings([{"job_id": "A", "title": "a"}, {"job_id": "B", "title": "b"}], {}, True)
        self.assertEqual(sent, ["A", "B"])                       # all cards sent up front
        self.assertEqual(order, ["A-started", "B-decided"])
        self.assertEqual(sorted(r["result"] for r in res), ["applied", "timeout"])
        self.assertEqual([p["job_id"] for p in P.load_pending()], ["B"])

    def test_reject_skiplists(self):
        with patch.object(P.tg.DISPATCHER, "start", lambda: None), \
             patch.object(P.tg.DISPATCHER, "stop", lambda: None), \
             patch.object(P.tg, "send_job_for_approval", lambda j, nonce="": 1), \
             patch.object(P.tg, "wait_for_decision", lambda *a, **k: "reject"), \
             patch.object(P, "_run_job") as rj:
            res = P.run_postings([{"job_id": "A", "title": "a"}], {}, True)
        rj.assert_not_called()                                  # no LLM work on reject
        self.assertEqual(res[0]["result"], "reject")
        self.assertIn("A", P.load_skiplist())


class TestAuditGate(PipeBase):
    def test_red_stops_with_one_alert(self):
        red = {"A.tex": {"status": "red", "usable_tex": None, "report": {"failed": ["contact_found"]}},
               "B.tex": {"status": "green", "usable_tex": "x", "report": {"failed": []}}}
        with patch.object(P.ra, "audit_bases_cached", return_value=red):
            self.assertIsNone(P.audit_or_stop())
        self.assertEqual(len(self.msgs), 1)
        self.assertIn("contact_found", self.msgs[0])

    def test_yellow_sets_override(self):
        fixed = self.tmp / "fixed.tex"; fixed.write_text("REPAIRED")
        green = self.tmp / "g.tex"; green.write_text("G")
        audit = {"A.tex": {"status": "yellow", "usable_tex": str(fixed), "report": {}},
                 "B.tex": {"status": "green", "usable_tex": str(green), "report": {}}}
        saved = dict(P.tailor.BASE_OVERRIDES)
        try:
            with patch.object(P.ra, "audit_bases_cached", return_value=audit):
                ctx = P.audit_or_stop()
            self.assertEqual(P.tailor.BASE_OVERRIDES, {"A.tex": str(fixed)})
            self.assertEqual(ctx["base_texts"], {"A.tex": "REPAIRED", "B.tex": "G"})
        finally:
            P.tailor.BASE_OVERRIDES.clear(); P.tailor.BASE_OVERRIDES.update(saved)


class TestRunJob(PipeBase):
    def setUp(self):
        super().setUp()
        d = self.tmp / "work"; d.mkdir()
        (d / "job_description.txt").write_text("Python AWS Kubernetes Terraform Go Java")
        self.pdf = self.tmp / "r.pdf"; self.pdf.write_bytes(b"%PDF")
        self.tex = self.tmp / "r.tex"; self.tex.write_text(make_tex())
        self.ctx = {"audit": {}, "base_texts": {"B": "Python"}}
        self.calls = {}
        for target, attr, val in (
                (P.tailor, "stage", lambda j: d),
                (P.tailor, "latest_pdf", lambda j: None),
                (P, "_scan_button", lambda: None),
                (P.ra, "verify_tailored_pdf",
                 lambda pdf, tex, kw: {"ok": True, "notes": [], "failed": [], "kw_in_final": 1, "kw_total": 1}),
                (P, "_apply", lambda job, pdf, dry: P._result(job, "applied", extra={"resume": pdf})),
                (P.tg, "send_resume_for_approval", lambda *a, **k: 1),
                (P.tg, "send_low_match_card", lambda *a, **k: self.calls.setdefault("low", True))):
            pt = patch.object(target, attr, val); pt.start(); self.addCleanup(pt.stop)

    def tailor_ok(self, job, keywords=None):
        self.calls["kw"] = keywords
        return {"ok": True, "pdf": str(self.pdf), "tex": str(self.tex), "attempts": 1,
                "model": "claude-sonnet-4-6", "version": 1}

    def kw(self, must):
        return {"must_have": must, "nice_to_have": []}

    def test_low_match_skip(self):
        with patch.object(P.ra, "extract_keywords",
                          return_value=self.kw(["Python", "AWS", "Kubernetes", "Terraform", "Go", "Java"])), \
             patch.object(P.tg, "wait_for_decision", return_value="lowno"), \
             patch.object(P.tailor, "tailor_with_claude") as tw:
            r = P._run_job(self.job, {"audit": {}, "base_texts": {"B": "nothing here"}}, True)
        self.assertEqual(r["result"], "skipped_low_match")
        self.assertTrue(self.calls.get("low"))
        tw.assert_not_called()                                 # no tailor spend
        self.assertIn("j1", P.load_skiplist())

    def test_low_match_timeout_pending(self):
        with patch.object(P.ra, "extract_keywords",
                          return_value=self.kw(["Python", "AWS", "Kubernetes", "Terraform", "Go", "Java"])), \
             patch.object(P.tg, "wait_for_decision", return_value="timeout"):
            r = P._run_job(self.job, {"audit": {}, "base_texts": {"B": "nothing"}}, True)
        self.assertEqual(r["result"], "timeout")
        self.assertEqual(P.load_pending()[0]["_last_detail"], "no low-match tap in time")
        self.assertNotIn("system_failure", r)

    def test_haiku_failure_degrades_and_approve_applies(self):
        with patch.object(P.ra, "extract_keywords", return_value={"error": "x"}), \
             patch.object(P.tailor, "tailor_with_claude", side_effect=self.tailor_ok), \
             patch.object(P.tg, "wait_for_decision", return_value="approve"):
            r = P._run_job(self.job, self.ctx, True)
        self.assertIsNone(self.calls["kw"])                    # tailor extracts its own
        self.assertNotIn("low", self.calls)
        self.assertEqual(r["result"], "applied")
        self.assertEqual(r["resume"], str(self.pdf))

    def test_keywords_passed_to_tailor(self):
        with patch.object(P.ra, "extract_keywords",
                          return_value=self.kw(["Python", "AWS", "Kubernetes", "Terraform", "Go"])), \
             patch.object(P.tailor, "tailor_with_claude", side_effect=self.tailor_ok), \
             patch.object(P.tg, "wait_for_decision", side_effect=["lowok", "approve"]):
            P._run_job(self.job, self.ctx, True)
        self.assertEqual(self.calls["kw"]["must_have"], ["Python", "AWS", "Kubernetes", "Terraform", "Go"])
        self.assertEqual(self.calls["kw"]["covered"], ["Python"])

    def test_postcheck_failed_is_system_failure(self):
        with patch.object(P.ra, "extract_keywords", return_value={"error": "x"}), \
             patch.object(P.tailor, "tailor_with_claude", side_effect=self.tailor_ok), \
             patch.object(P.ra, "verify_tailored_pdf",
                          return_value={"ok": False, "failed": ["contact_found"], "notes": []}), \
             patch.object(P, "_alert_failure") as al, \
             patch.object(P.tg, "send_resume_for_approval") as send:
            r = P._run_job(self.job, self.ctx, True)
        self.assertEqual(r["result"], "postcheck_failed")
        self.assertTrue(r["system_failure"])
        self.assertFalse(self.pdf.exists())                   # quarantined → not reused
        self.assertTrue((self.tmp / "POSTCHECK_FAILED_r.pdf").exists())
        al.assert_called_once()
        send.assert_not_called()                               # never sent for approval

    def test_tailor_failure_alerts(self):
        with patch.object(P.ra, "extract_keywords", return_value={"error": "x"}), \
             patch.object(P.tailor, "tailor_with_claude",
                          return_value={"ok": False, "fail_code": "timeout", "fail_reason": "r"}), \
             patch.object(P, "_alert_failure") as al:
            r = P._run_job(self.job, self.ctx, True)
        self.assertEqual(r["result"], "tailor_failed")
        self.assertTrue(r["system_failure"])
        al.assert_called_once()

    def test_scan_loop_then_approve_submits_new_version(self):
        v2 = {"pdf": str(self.tmp / "v2.pdf"), "tex": str(self.tex), "version": 2, "round": 1,
              "prev_scan": "x", "verdict": "v", "match": None, "kw": None}
        with patch.object(P.ra, "extract_keywords", return_value={"error": "x"}), \
             patch.object(P.tailor, "tailor_with_claude", side_effect=self.tailor_ok), \
             patch.object(P.tailor, "consolidate") as cons, \
             patch.object(P, "_scan_and_retailor", return_value=v2), \
             patch.object(P.tg, "wait_for_decision", side_effect=["scan", "approve"]):
            r = P._run_job(self.job, self.ctx, True)
        self.assertEqual(r["resume"], v2["pdf"])               # approved card's version
        cons.assert_called_once()


def env(data=None, err=None):
    return {"ok": False, "error": err} if err else {"ok": True, "data": data or {}}


class FakeBI:
    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        v = self.script[cmd]
        return v.pop(0) if isinstance(v, list) else v


class TestScan(PipeBase):
    ST = {"pdf": "/tmp/r.pdf", "tex": "/tmp/r.tex", "version": 1, "round": 0, "prev_scan": "",
          "verdict": "v", "match": None, "kw": None}
    AUTH = env({"logged_in": True, "scans_remaining": 3})

    def test_poll_interval_at_least_20s_and_feedback(self):
        bi = FakeBI({"auth": self.AUTH,
                     "start": env({"scan_id": "s1", "state": "queued", "scans_remaining": 2}),
                     "status": [env({"state": "scanning"}), env({"state": "complete", "scans_remaining": 2})],
                     "feedback": env({"scan_id": "s1", "medal": "gold"})})
        with patch.object(P, "_bi", bi), patch.object(P.ra, "agent_cfg",
                                                      lambda k: 1 if k == "bi_poll_s" else RA.AGENT_DEFAULTS[k]):
            fb, left, err = P._run_scan(self.job, self.ST)
        self.assertIsNone(err)
        self.assertEqual(fb["medal"], "gold")
        self.assertTrue(self.sleeps and all(s >= 20 for s in self.sleeps))

    def test_timeout_after_10_min(self):
        bi = FakeBI({"auth": self.AUTH, "start": env({"scan_id": "s1", "state": "scanning"}),
                     "status": env({"state": "scanning"})})
        clock = [0.0]
        with patch.object(P, "_bi", bi), patch.object(P.time, "time", lambda: clock[0]), \
             patch.object(P.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            fb, left, err = P._run_scan(self.job, self.ST)
        self.assertIn("timed out", err)
        self.assertIn("s1", err)
        self.assertLessEqual(bi.calls.count("status"), 600 // 20)

    def test_unknown_not_retried(self):
        bi = FakeBI({"auth": self.AUTH, "start": env({"scan_id": "s1", "state": "scanning"}),
                     "status": [env({"state": "unknown", "hint": "snapshot at /x"})]})
        with patch.object(P, "_bi", bi):
            fb, left, err = P._run_scan(self.job, self.ST)
        self.assertEqual(bi.calls.count("status"), 1)
        self.assertIn("not retrying", err)
        self.assertIn("snapshot at /x", err)

    def test_login_expired_and_mcp_error_hint(self):
        with patch.object(P, "_bi", FakeBI({"auth": env({"logged_in": False})})):
            self.assertIn("login expired", P._run_scan(self.job, self.ST)[2])
        err = {"code": "profile_in_use", "message": "busy", "hint": "close the other app"}
        bi = FakeBI({"auth": self.AUTH, "start": env(err=err)})
        with patch.object(P, "_bi", bi):
            msg = P._run_scan(self.job, self.ST)[2]
        self.assertIn("profile_in_use", msg)
        self.assertIn("close the other app", msg)
        self.assertEqual(bi.calls, ["auth", "start"])           # never retried

    def test_scan_failure_returns_same_card_state(self):
        with patch.object(P, "_run_scan", return_value=(None, None, "boom")):
            self.assertIs(P._scan_and_retailor(self.job, {}, self.ST), self.ST)
        self.assertIn("boom", self.msgs)

    def test_scan_lock_no_overlap(self):
        live, peak = [0], [0]
        lock = threading.Lock()

        def slow_scan(job, st):
            with lock:
                live[0] += 1; peak[0] = max(peak[0], live[0])
            import time as _t; _t.sleep(0.05)
            with lock:
                live[0] -= 1
            return None, None, "done"

        with patch.object(P, "_run_scan", slow_scan):
            ts = [threading.Thread(target=P._scan_and_retailor, args=(self.job, {}, self.ST))
                  for _ in range(3)]
            [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(peak[0], 1)


class TestLimitReset(PipeBase):
    SCANS = [{"scan_id": f"s{i}", "scanned_at": f"2026-10-{10 - i:02d}T00:00:00"} for i in range(8)] + \
            [{"scan_id": "undated", "scanned_at": None}]

    def bi(self, restored):
        return FakeBI({"list": env({"scans": self.SCANS, "next_cursor": None}),
                       "delete": env({"deleted": True, "backup_path": "/b"}),
                       "auth": env({"logged_in": True, "scans_remaining": 5 if restored else 0})})

    def test_deletes_exactly_5_oldest(self):
        bi = self.bi(True)
        deleted = []
        orig = bi.__call__

        def rec(cmd, **kw):
            if cmd == "delete":
                deleted.append(kw["scan_id"])
            return orig(cmd, **kw)

        with patch.object(P, "_bi", rec):
            ok, msg, left = P._limit_reset()
        self.assertTrue(ok)
        self.assertEqual(left, 5)
        self.assertEqual(deleted, ["s7", "s6", "s5", "s4", "s3"])   # oldest first; undated never

    def test_not_restored_stops_and_never_twice_a_day(self):
        bi = self.bi(False)
        with patch.object(P, "_bi", bi):
            ok, msg, left = P._limit_reset()
            self.assertFalse(ok)
            self.assertIn("did not reset", msg)
            ok2, msg2, _ = P._limit_reset()
        self.assertFalse(ok2)
        self.assertIn("already ran", msg2)
        self.assertEqual(bi.calls.count("delete"), 5)            # second call deleted nothing

    def test_latest_refusal_skipped_and_does_not_burn_stamp(self):
        refuse = env(err={"code": "invalid_input", "message": "latest loop result",
                          "hint": "Pass confirm=true to delete it anyway."})
        # Only refusals → nothing deleted, today's reset stays available.
        bi = FakeBI({"list": env({"scans": self.SCANS[:1], "next_cursor": None}),
                     "delete": refuse, "auth": env({"logged_in": True, "scans_remaining": 0})})
        with patch.object(P, "_bi", bi):
            ok, _, _ = P._limit_reset()
        self.assertFalse(ok)
        self.assertFalse(P.SCAN_RESET_STAMP.exists())
        # A refused latest is skipped (never forced), the next oldest still deleted.
        deleted = []

        def bi2(cmd, **kw):
            if cmd == "delete":
                self.assertNotIn("confirm", kw)
                if kw["scan_id"] == "s7":
                    return refuse
                deleted.append(kw["scan_id"])
                return env({"deleted": True})
            return self.bi(True)(cmd, **kw)

        with patch.object(P, "_bi", bi2):
            ok, _, left = P._limit_reset()
        self.assertTrue(ok)
        self.assertEqual(deleted, ["s6", "s5", "s4", "s3", "s2"])
        self.assertTrue(P.SCAN_RESET_STAMP.exists())


class TestRetailorFlow(PipeBase):
    ST = TestScan.ST

    def card_patch(self, picked=(["Kubernetes"], [])):
        class Card:
            selectable = [{"kind": "keyword", "text": "Kubernetes"}]

            def picked(self_inner):
                return picked

            def refuse(self_inner, a):
                return None
        return patch.object(P.tg, "send_score_card", lambda *a, **k: Card())

    def test_retailor_failure_falls_back(self):
        tex = self.tmp / "r.tex"; tex.write_text("x")
        st = {**self.ST, "tex": str(tex)}
        with patch.object(P, "_run_scan", return_value=({"scan_id": "s", "medal": "silver"}, 2, None)), \
             self.card_patch(), patch.object(P.tg, "wait_for_decision", return_value="retailor"), \
             patch.object(P.tailor, "retailor", return_value={"ok": False, "fail_code": "timeout",
                                                               "fail_reason": "slow", "version": 2}):
            out = P._scan_and_retailor(self.job, {}, st)
        self.assertIs(out, st)
        self.assertTrue(any("Retailor failed" in m for m in self.msgs))

    def test_retailor_success_new_version_and_round(self):
        tex = self.tmp / "r.tex"; tex.write_text("x")
        st = {**self.ST, "tex": str(tex)}
        new_res = {"ok": True, "pdf": str(self.tmp / "v2.pdf"), "tex": str(tex), "version": 2, "attempts": 1,
                   "model": "claude-sonnet-4-6"}
        fb = {"scan_id": "s", "medal": "silver", "categories": {"ats_fit": {"badge": "bronze"}}}
        with patch.object(P, "_run_scan", return_value=(fb, 2, None)), self.card_patch(), \
             patch.object(P.tg, "wait_for_decision", return_value="retailor"), \
             patch.object(P.tailor, "retailor", return_value=new_res) as rt, \
             patch.object(P.ra, "verify_tailored_pdf", return_value={"ok": True, "notes": [], "failed": []}):
            out = P._scan_and_retailor(self.job, {"audit": {}, "base_texts": {}}, st)
        self.assertEqual((out["version"], out["round"]), (2, 1))
        self.assertTrue((self.tmp / T.VALIDATED).exists())          # retry may restore v2
        self.assertEqual(out["prev_scan"], "🥈 Silver · ATS Fit: 🥉 Bronze")
        self.assertEqual(rt.call_args.args[2:], (["Kubernetes"], []))   # only ticked items
        rows = json.loads(P.SCANS_FILE.read_text())
        self.assertEqual(rows[0]["items"], [{"kind": "keyword", "text": "Kubernetes"}])

    def test_back_and_timeout(self):
        with patch.object(P, "_run_scan", return_value=({"scan_id": "s"}, 2, None)), self.card_patch():
            with patch.object(P.tg, "wait_for_decision", return_value="back"):
                self.assertIs(P._scan_and_retailor(self.job, {}, self.ST), self.ST)
            with patch.object(P.tg, "wait_for_decision", return_value="timeout"):
                self.assertEqual(P._scan_and_retailor(self.job, {}, self.ST), "timeout")


class TestPendingConcurrency(PipeBase):
    def test_concurrent_adds_not_lost(self):
        jobs = [{"job_id": f"j{i}", "title": "t"} for i in range(20)]
        ts = [threading.Thread(target=P.add_pending, args=(j,)) for j in jobs]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(P.load_pending()), 20)

    def test_external_autofill_shares_telegram_module(self):
        self.assertIs(P.external_autofill.tg, P.tg)


class TestSupervisorKnowsNewResults(unittest.TestCase):
    def test_sets(self):
        import supervisor as S
        self.assertIn("skipped_low_match", S.BENIGN)
        self.assertEqual(S.pre_classify("postcheck_failed", 0), "transient")


# ── Phase 5: bi_scan.py (MCP mocked) ─────────────────────────────────────────
import io  # noqa: E402
import contextlib  # noqa: E402
import subprocess  # noqa: E402
import bi_scan as BS  # noqa: E402


class FakeProc:
    def __init__(self, out="", err="", rc=0, hang=False):
        self.out, self.err, self.returncode, self.hang, self.pid = out, err, rc, hang, 1

    def communicate(self, timeout=None):
        if self.hang:
            self.hang = False
            raise subprocess.TimeoutExpired("uv", timeout)
        return self.out, self.err


class TestBiScanClient(unittest.TestCase):
    def setUp(self):
        self.mcp = Path(tempfile.mkdtemp())
        self.argv = []
        for target, attr, val in ((RA, "agent_cfg", lambda k: str(self.mcp) if k == "bi_mcp_dir"
                                   else RA.AGENT_DEFAULTS[k]),
                                  (BS.shutil, "which", lambda n: "/usr/bin/uv")):
            p = patch.object(target, attr, val); p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.mcp, ignore_errors=True)

    def popen(self, proc):
        def f(argv, **kw):
            self.argv.append(argv)
            self.assertTrue(kw.get("start_new_session"))
            return proc
        return patch.object(BS.subprocess, "Popen", f)

    def test_json_in_out_per_command(self):
        for cmd, args, tool_out in (
                ("auth", {}, {"ok": True, "data": {"logged_in": True, "scans_remaining": 4}}),
                ("start", {"resume_path": "/r.pdf", "job_title": "T", "company": "C",
                           "job_description": "JD", "scoring_guide": "Graduate - STEM Focus"},
                 {"ok": True, "data": {"scan_id": "1", "state": "queued"}}),
                ("status", {"scan_id": "1"}, {"ok": True, "data": {"state": "complete"}}),
                ("feedback", {"scan_id": "1"}, {"ok": True, "data": {"medal": "gold"}}),
                ("list", {"limit": 50, "cursor": None}, {"ok": True, "data": {"scans": []}}),
                ("delete", {"scan_id": "1"}, {"ok": True, "data": {"deleted": True}})):
            with self.popen(FakeProc(out="noise\n" + json.dumps(tool_out) + "\n")):
                self.assertEqual(BS.call(cmd, **args), tool_out)
            argv = self.argv[-1]
            self.assertEqual(argv[:4], ["/usr/bin/uv", "--directory", str(self.mcp), "run"])
            self.assertEqual(argv[6], cmd)
            self.assertEqual(float(argv[argv.index("--timeout") + 1]),
                             BS.TIMEOUT_S.get(cmd, BS.DEFAULT_TIMEOUT_S) - BS.INNER_MARGIN_S)
            sent = json.loads(argv[argv.index("--json-args") + 1])
            self.assertEqual(sent, {k: v for k, v in args.items() if v is not None})

    def test_error_passthrough_with_hint(self):
        e = {"ok": False, "error": {"code": "auth_expired", "message": "m", "hint": "run scripts/login.py"}}
        with self.popen(FakeProc(out=json.dumps(e))):
            self.assertEqual(BS.call("auth"), e)

    def test_no_json_timeout_unknown_unavailable(self):
        with self.popen(FakeProc(out="", err="Traceback\nModuleNotFoundError: mcp", rc=1)):
            r = BS.call("auth")
        self.assertEqual(r["error"]["code"], "internal_error")
        self.assertIn("ModuleNotFoundError", r["error"]["hint"])
        with self.popen(FakeProc(hang=True)), patch("claude_runner._kill_group") as kg:
            r = BS.call("start", resume_path="/r.pdf")
        self.assertEqual(r["error"]["code"], "timeout")
        kg.assert_called_once()
        self.assertEqual(BS.call("bogus")["error"]["code"], "invalid_input")
        with patch.object(BS.subprocess, "Popen", side_effect=OSError("EAGAIN")):
            r = BS.call("auth")
        self.assertEqual(r["error"]["code"], "unavailable")
        self.assertIn("EAGAIN", r["error"]["message"])
        shutil.rmtree(self.mcp)
        self.assertEqual(BS.call("auth")["error"]["code"], "unavailable")


class TestBiScanServer(unittest.TestCase):
    def run_main(self, argv, runner):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = BS.main(argv, runner=runner)
        return rc, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_maps_commands_to_tools(self):
        seen = []
        rc, out = self.run_main(["status", "--json-args", '{"scan_id": "9"}'],
                                lambda tool, args: seen.append((tool, args)) or {"ok": True, "data": {}})
        self.assertEqual((rc, out["ok"]), (0, True))
        self.assertEqual(seen, [("get_scan_status", {"scan_id": "9"})])
        self.assertEqual(set(BS.TOOLS.values()), {"auth_status", "start_scan", "get_scan_status",
                                                  "get_scan_feedback", "list_scans", "delete_scan"})

    def test_exception_and_bad_args(self):
        def boom(tool, args):
            raise RuntimeError("no browser")
        rc, out = self.run_main(["auth"], boom)
        self.assertEqual(out["error"]["code"], "internal_error")
        self.assertTrue(out["error"]["hint"])
        rc, out = self.run_main(["auth", "--json-args", "[1]"], boom)
        self.assertEqual((rc, out["error"]["code"]), (2, "invalid_input"))

    def test_inner_timeout_shuts_down(self):
        import asyncio

        async def hang(tool, args):
            await asyncio.sleep(10)

        buf = io.StringIO()
        with patch.object(BS, "_run_tool", hang), contextlib.redirect_stdout(buf):
            BS.main(["auth", "--timeout", "0.05"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["error"]["code"], "timeout")

    def test_unwrap(self):
        class R:
            structuredContent = {"ok": True, "data": {"x": 1}}
        self.assertEqual(BS._unwrap(R()), {"ok": True, "data": {"x": 1}})

        class W:
            structuredContent = {"result": {"ok": False, "error": {"code": "c"}}}
        self.assertEqual(BS._unwrap(W())["error"]["code"], "c")

        class C:
            structuredContent = None
            content = [type("T", (), {"text": '{"ok": true, "data": {}}'})()]
        self.assertEqual(BS._unwrap(C()), {"ok": True, "data": {}})


if __name__ == "__main__":
    unittest.main()
