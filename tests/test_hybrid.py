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
                    "description": "Python AWS"}
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
                     "🌐 External apply: No", "currentJobId=" + "a" * 32):
            self.assertIn(frag, t)
        self.assertNotIn("Boston", t)                     # location dropped
        t2 = TB.posting_card_text({**JOB, "apply_urls": "https://x.io/a; https://y.io/b",
                                   "salary": "", "posted": ""})
        self.assertIn("External apply: Yes — https://x.io/a  https://y.io/b", t2)
        self.assertIn("Pay: Not listed", t2)
        self.assertIn("Posted: —", t2)

    def test_posting_url_matches_apply(self):
        try:
            import auto_apply
        except ImportError:
            self.skipTest("playwright not installed")
        self.assertEqual(TB.job_posting_url("123"), auto_apply.job_url("123"))

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
        self.assertEqual(self.acks, [("cq9", TB.EXPIRED)])

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


if __name__ == "__main__":
    unittest.main()
