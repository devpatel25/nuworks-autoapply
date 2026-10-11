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


if __name__ == "__main__":
    unittest.main()
