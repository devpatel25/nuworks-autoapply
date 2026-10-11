# NUworks Auto-Apply — Hybrid Resume Pipeline Plan

Status: **plan, not yet built** · Drafted 2026-10-11 · Branch to build on: `feat/hybrid-resume-pipeline` (off `main`)

This plan supersedes `nuworks-hybrid-resume-pipeline-handoff.md`. The handoff described code that does not exist in this repo (no `resume_agents.py`, no tests, no branch), so everything below is built fresh. Section 2 lists every decision taken in review and how it differs from the handoff.

---

## 1. Goal

Two goals, weighted equally:

1. **Better resumes** — ATS-safe output, strong keyword coverage, XYZ-format bullets.
2. **Spend nothing on jobs you don't want** — you approve each posting *before* any LLM sees its JD, and get a second chance to bail on low keyword-match jobs before the (expensive) tailor runs.

Human-in-the-loop is preserved and extended: nothing is tailored without a posting Approve, and nothing is submitted without a resume Approve.

---

## 2. Decision log (review outcomes)

| # | Topic | Decision | vs. handoff |
|---|---|---|---|
| D1 | Where the code lives | Build fresh in this repo | Handoff claimed it existed |
| D2 | Main goal | Quality and filtering, equally | — |
| D3 | **Posting approval gate** | New first stage: Telegram card per new posting; only Approve sends the JD onward | **New** (not in handoff) |
| D4 | Posting card delivery | Send **all cards at once**; each Approve starts that job's pipeline immediately, in parallel | Existing uncommitted gate is one-at-a-time — replaced |
| D5 | Keyword extraction | **Haiku** (`claude -p`, cheap model) extracts must-have / nice-to-have keywords | Handoff used Sonnet matcher |
| D6 | Keyword reuse | Tailor **reuses Haiku's list** and skips its own extraction phase | Handoff: two extractions (conflicting) |
| D7 | Keyword verification | Python drops any keyword not literally present in the JD | New guard |
| D8 | Low match (< 40%) | **Second Telegram card**: "Low match (32%) — tailor anyway?" | Handoff: silent auto-skip |
| D9 | Low-match card, no reply | Wait ~10 min → skip for this run, re-ask next run | — |
| D10 | Base variant choice | **LLM keeps choosing** (per `system_instructions.md`); Python logs its own guess in the caption for comparison | Handoff: Python pins variant |
| D11 | XYZ bullets | Apply to the **top 1–2 bullets per role/project** only | Handoff: every bullet |
| D12 | "MUST integrate every keyword" rule | **Kept as-is** | — |
| D13 | Base ATS failure | **LLM auto-repair** of a scratch copy (≤3 tries), Python re-verifies, plus a preamble-unchanged guard | Same as handoff + new guard |
| D14 | Three-agent chain (`run_agent_chain`) | **Not built** | Handoff kept it dormant |
| D15 | Keyword dictionary approach | **Dropped** in favour of Haiku (D5) | — |
| D16 | **Big Interview scan button** | Resume card gets a 🔍 Scan button → scans the tailored PDF with JD, company, title via the `resumeai` MCP → score card | **New** |
| D17 | Scan limit | Button shows remaining (`Scan (3/5 left)`); at 0/5 the pipeline **deletes the 5 oldest scans in My Scans (any scan, incl. manual ones)** and re-checks the allowance | **New** — overrides the MCP's own design (see §4A.4) |
| D18 | Score card contents | Medal + all 4 badges + keywords matched x/y; **correction toggles only from Credibility and ATS Fit** | **New** |
| D19 | What Retailor adds | **You pick** on Telegram: unmatched keywords and corrections as toggle buttons | **New** |
| D20 | After a retailor | New resume card (Approve / Reject / Scan), no auto re-scan | **New** |
| D21 | Retailor rounds | Max **3** per job | **New** |
| D22 | Retailor edit mode | Sonnet **edits the current tailored .tex** (not fresh from base), then the 1-page loop | **New** |

---

## 3. Pipeline at a glance

```
main()
 ├─ 0. Guards: pause flag · singleton lock · once-per-day · watchdog
 ├─ 1. Scrape NUworks (DUO retry)                         [existing]
 ├─ 2. eligible() + merge_pending()                       [existing]
 ├─ 3. Base ATS audit (once per run, sha256-cached)       [NEW]
 │     └─ red after repair → ONE alert, run stops, day unmarked
 ├─ 4. Send ALL posting cards at once                     [NEW / replaces gate_by_posting]
 │
 │   ── per job, in parallel, started the moment you tap Approve ──
 │
 ├─ 5. Haiku keyword extraction                           [NEW]
 ├─ 6. Python verify + score                              [NEW]
 ├─ 7. score < 40%? → low-match card                      [NEW]
 ├─ 8. Tailor (Sonnet → Opus escalation)                  [existing, modified]
 │       uses Haiku keywords · MUST integrate all · XYZ top 1–2 per role
 ├─ 9. Post-check (ATS + keyword coverage, Python only)   [NEW]
 ├─ 10. Verdict line                                      [NEW]
 ├─ 11. Resume approval card (with verdict)               [existing, caption + Scan button]
 │       [✅ Approve & Apply] [❌ Reject] [🔍 Scan (n/5 left)]
 │       └─ Scan → Big Interview scan → score card → pick items → Retailor
 │            → back to Step 9 with new version (max 3 rounds)   [NEW — §4A]
 ├─ 12. Cover letter + its own approval (if required)     [existing]
 ├─ 13. auto_apply on NUworks  (serialized — one at a time) [existing]
 ├─ 14. External apply links → external_autofill          [existing]
 │
 └─ 15. Summary · mark/unmark day · stats · supervisor report [existing]
```

---

## 4. Step-by-step

Each step: **In** → **Do** → **Out** → **Failure**.

### Step 0 — Run guards (existing, unchanged)

- **Do:** exit if `.autoapply_paused.nuworks` exists; acquire singleton lock; skip if already ran today (unless `--force`); start watchdog.
- **Failure:** n/a — these just exit quietly.

### Step 1 — Scrape (existing, unchanged)

- **Do:** `scrape_with_duo_retry()` → `job_scraper.py` subprocess → `data/jobs.csv`. DUO timeout → Telegram rerun/skip card.
- **Out:** flattened job rows: `job_id, title, company, location, type, posted, deadline, salary, remote, qualified, apply_urls, description`.
- **Failure:** existing alerts (scrape failed / hung), day unmarked.

### Step 2 — Eligibility (existing, unchanged)

- **Do:** `eligible()` keeps in-portal jobs not on skiplist / wontfix; `stats.announce_new_postings()`; `merge_pending()` re-adds jobs that timed out last run (posting, low-match, resume, or cover-letter timeouts); `--limit N` applied.
- **Out:** `todo` list.

### Step 3 — Base ATS audit (NEW) — `resume_agents.audit_bases_cached()`

Runs **once per run**, in the main thread, **before any posting card is sent** (no point asking you to approve jobs if the base resume is broken).

- **In:** both `tailor.BASE_VARIANTS` (`Resume_SWE_General.tex`, `Resume_AI-ML_General.tex`).
- **Do:**
  1. sha256 each base. If it matches `data/ats_audit.json` **and** the record is < 30 days old → cache hit, no work.
  2. Otherwise run `run_ats_checks()` on a scratch copy: `pdflatex` → `pdftotext -layout` → `evaluate_extraction()`, 7 checks:

     | Check | Pass condition |
     |---|---|
     | `tex_layout_lint` | no `tabular`, `multicols`, `includegraphics`, `minipage` in the body |
     | `compiles` | `pdflatex` produces a PDF |
     | `text_extractable` | > 200 chars extracted |
     | `contact_found` | email + phone regex both match |
     | `sections_present` | every `\section*{…}` header found in extracted text |
     | `reading_order` | headers appear in document order |
     | `word_count_sane` | 200–1200 words |

  3. On failure → repair loop: `claude -p` with `ATS_FIX_PROMPT` edits the **scratch copy**, max 3 attempts. After every edit Python:
     - re-runs all 7 checks, and
     - **rejects the edit if anything before `\begin{document}` changed** (preamble/geometry/fonts guard — enforces invariant I2 in code, not just in the prompt).
- **Out per variant:** `{status, usable_tex, report}`
  - `green` — clean; `usable_tex` = original template.
  - `yellow` — repaired; `usable_tex` = `data/ats_audit_cache/<sha>.tex`. **Your template file is never modified.**
  - `red` — unfixable or missing; `usable_tex = None`.
- **How `usable_tex` is used:** for a `yellow` base, `tailor.stage()` copies the repaired version into the job workdir in place of the original, so the tailor edits the repaired copy.
- **Failure (any red):** one Telegram alert ("Base resume X fails ATS checks: <failed checks>. Fix the template. Day left unmarked."). No posting cards sent, no LLM tailor calls, run stops, day unmarked.

### Step 4 — Posting approval cards (NEW, replaces current `gate_by_posting`)

The first time any job is shown to you. **No LLM sees the JD until you tap Approve.**

- **In:** `todo`.
- **Do:** send **every** card immediately (not one-at-a-time). Card format:

  ```
  🆕 New posting

  <b>[Job title]</b>
  [Company name]
  💵 Pay: [pay rate | Not listed]
  📅 Posted: [date posted | —]
  🗓 Apply by: [deadline | —]
  🌐 External apply: [No | Yes — <link> <link>]
  🔗 [Job posting link]

  [✅ Approve] [❌ Reject]
  ```

  - Pay uses the existing `_fmt_pay()`.
  - Posted / deadline come from scraper fields `posted` / `deadline` (already captured, just not shown today).
  - External apply is **always shown** (`No` when `apply_urls` is empty).
  - Job posting link: built from `job_id` on `https://northeastern-csm.symplicity.com`. **Exact detail-page URL to be verified against the live portal during build** (expected `…/students/app/jobs/detail/<job_id>`).
  - Location is dropped (not in your format).
- **Out (per tap, handled by the dispatcher — see §5):**
  - **Approve** → that job enters Step 5 immediately, in its own worker.
  - **Reject** → `add_to_skiplist(job_id)` (permanent), `clear_pending`.
  - **No tap within 1 h** → `add_pending(job, "transient", "no posting-approval tap")`; card re-sent next run.
- **Failure:** Telegram send error → existing `_post_with_retry`; if it still fails, the job is added to pending (not a system failure for the whole run).

### Step 5 — Haiku keyword extraction (NEW) — `resume_agents.extract_keywords(job, workdir)`

- **In:** JD text (`job_description.txt`, written by `tailor.stage()`).
- **Do:** `claude -p` with model `agents.keyword_model` (default `claude-haiku-5-5`) and `KEYWORD_EXTRACT_PROMPT`: return **strict JSON only**:

  ```json
  {"must_have": ["Python", "AWS", ...], "nice_to_have": ["Go", ...]}
  ```

  15–30 terms total; `must_have` = required / qualifications section; `nice_to_have` = preferred / bonus. Parsed with a tolerant parser (`_parse_keywords_json`, finds the first JSON object even if Haiku adds prose). Up to 2 attempts.
- **Out:** `{must_have[], nice_to_have[]}` or `{error}`.
- **Failure (both attempts fail):** graceful degradation — **no score, no low-match card**, tailor runs exactly as today (extracts its own keywords). Caption shows `Match: n/a`. Never a system failure.

### Step 6 — Verify + score (NEW, Python only) — `resume_agents.score_keywords(kw, jd_text, base_texts)`

- **In:** Haiku's lists, JD text, text of both bases (`usable_tex` from Step 3).
- **Do:**
  1. **Verify:** drop any keyword that does not appear in the JD (case-insensitive, whole-word). Haiku cannot invent requirements.
  2. **Normalize:** small synonym map to a canonical form (`k8s → Kubernetes`, `postgres → PostgreSQL`, `js → JavaScript`, `ml → Machine Learning`, …); dedupe.
  3. **Thin JD guard:** if fewer than 5 keywords survive → `score = None` (`n/a`), skip Step 7.
  4. **Match** each keyword against each base with a **word-boundary regex** (not raw substring — so `Java` ≠ `JavaScript`, and `R` / `Go` don't match random text).
  5. **Score** per base, take the **better** base (the tailor picks the base later, so you're not gated on the weaker one):

     ```
     score = (must_matched + 0.5 × nice_matched) / (must_total + 0.5 × nice_total)
     ```

     Example: must = Python, AWS, Kubernetes, Terraform; nice = Go. Resume has Python, AWS → (1+1)/(4+0.5) = **44%** → passes.
  6. **Python variant guess:** whichever base scored higher (logged only — D10).
- **Out:** `{score, must_hit, must_total, missing_must[], covered[], py_variant_guess, keywords}`.

### Step 7 — Low-match card (NEW, only if `score < 0.40`)

- **Do:** send:

  ```
  ⚠️ Low match: 32% (3/9 must-have)

  <b>[Job title]</b> — [Company]
  Missing: Kubernetes, Terraform, Go, …

  Tailor anyway?
  [✅ Tailor anyway] [❌ Skip]
  ```

- **Out:**
  - **Tailor anyway** → Step 8.
  - **Skip** → `add_to_skiplist`, job ends (`skipped_low_match`).
  - **No tap within 10 min** → `add_pending(job, "transient", "no low-match tap")`; job ends for this run; asked again next run (re-uses cached keywords if the workdir still has them).
- **Failure:** n/a (Telegram retry as usual).

### Step 8 — Tailor (existing, modified) — `tailor.tailor_with_claude(job, keywords=None)`

Sole writer of the resume. Structure unchanged (Sonnet 4.6 → compile → page-count trim loop → Opus 4.8 escalation → `consolidate()`).

**Changes:**

1. **New optional param `keywords`** (backward compatible — `None` = today's behavior). When given, the gen prompt includes:

   ```
   KEYWORDS (pre-extracted — use these, do NOT run your own extraction):
   Must-have: Python, AWS, Kubernetes, Terraform
   Nice-to-have: Go
   Already in base: Python, AWS
   Missing from base: Kubernetes, Terraform, Go
   ```

2. **`system_instructions.md` keyword phase:** add "If a keyword list is supplied in the task prompt, use it as the extracted list and skip extraction." The "**MUST integrate every keyword**" rule (line ~151) is **kept unchanged** (D12).
3. **XYZ guidance (D11)** — new constant `XYZ_BULLET_GUIDANCE` in the gen prompt + new §4C in `system_instructions.md` (old 4C→4D, 4D→4E, 4E→4F):
   - Rewrite the **top 1–2 bullets of each work entry and each project** as "Accomplished [X], as measured by [Y], by doing [Z]".
   - Only use metrics that already exist in the base. A bullet with no real metric becomes "Did X by doing Z" — **never invent a number**.
   - Other bullets: keep current style. ~29-word cap and all density rules unchanged.
4. Base variant choice stays with the LLM (D10); the prompt is unchanged on that point.
5. `xyz_bullet_stats(tex)` — Python counts bullets containing a metric (digit, %, $, ×, "fold"); advisory only.

- **Out:** existing `{ok, pdf, tex, pages, attempts, model, escalated}` or `{fail_code, fail_reason, log}`.
- **Failure:** existing — `_fail` → Telegram alert with probable cause → day unmarked.

### Step 9 — Post-check (NEW, Python only) — `resume_agents.verify_tailored_pdf(pdf, tex, keywords)`

- **Do:**
  1. Re-run `evaluate_extraction()` on the tailored PDF.
  2. **Keyword coverage:** count how many of the keywords (Haiku's list, or the tailor's own labeled keyword line from `claude_tailor.log` when Haiku failed) appear in the final PDF text.
- **Out:** `{ok, notes, kw_in_final, kw_total}`.
  - Critical checks (`text_extractable`, `contact_found`) fail → `ok = False`.
  - Others → advisory notes only.
- **Failure (`ok = False`):** `postcheck_failed` → Telegram alert, resume **not** sent for approval, day unmarked.

### Step 10 — Verdict line (NEW) — `resume_agents.build_verdict(...)`

≤ 400 chars, appended to the resume card caption:

```
ATS ✓ · Match 72% (13/18 must) · Base: SWE (py: SWE) · Keywords in final: 22/25 · XYZ 6/11 · Sonnet, 2 tries · Post-check ✓
```

Variants: `ATS ✓ (repaired)` for a yellow base; `Match n/a` when Haiku failed or JD was thin; `Opus (escalated)` when escalation happened.

### Step 11 — Resume approval card (existing, caption + Scan button)

- **Do:** `tg.send_resume_for_approval(pdf, job, summary=verdict, nonce=…, scans_left=n, round=r)` — `summary` already exists; `scans_left` / `round` are new.

  ```
  📄 Tailored resume ready  (v2 · retailor 1/3)
  [title] · [company] · pay · deadline · external links
  ATS ✓ · Match 72% · … · Post-check ✓
  Prev scan: 🥈 Silver · ATS Fit: needs work        ← only after a retailor

  [✅ Approve & Apply] [❌ Reject] [🔍 Scan (3/5 left)]
  ```

  - Scan button label always shows the remaining allowance (from `auth_status`, read once when the card is built).
  - Scan button is hidden only if the Big Interview login is invalid (`logged_in=false`) — the card then says "Scan unavailable: log in via scripts/login.py".
- **Out:** Approve → Step 12; Reject → skiplist; **Scan → §4A**; no tap within 1 h → pending. Each new card (after scan/retailor) gets a fresh nonce and a fresh 1 h window.

### Step 11A — Big Interview scan & retailor loop (NEW) — `bi_scan.py`, `pipeline._scan_and_retailor()`

#### 4A.1 How the pipeline talks to the MCP

The pipeline does **not** import the MCP's internals. A small client script, `bi_scan.py` (in this repo), is run inside the MCP's own environment:

```
uv --directory ~/Desktop/Claude-env/Biginterview-MCP/Biginterview-MCP run python <repo>/bi_scan.py <cmd> --json-args …
```

It starts the `resumeai-mcp` server over stdio (MCP Python SDK, already in that env), calls one tool, and prints the JSON result. So every call goes through the MCP's public tools and keeps its safety rules: one tool at a time, 2–5 s human-like delays, daily budget, backup-before-delete. No new dependency in this repo.

Commands: `auth`, `start`, `status`, `feedback`, `list`, `delete`.

#### 4A.2 Scan lock

Big Interview allows **one scan in flight** and **no parallel tool calls**, but up to 6 job workers may be live. All `bi_scan.py` calls go through one `threading.Lock` (`SCAN_LOCK`). A second Scan tap while another job is scanning gets the reply "Queued — another scan is running" and waits its turn.

#### 4A.3 Scan flow (on 🔍 Scan tap)

1. Ack the tap: "Scanning… (takes up to 10 min)".
2. `auth` → if `logged_in=false`: message "Big Interview login expired — run scripts/login.py", resend the resume card without the Scan button. Stop.
3. If `scans_remaining == 0` → **limit reset (§4A.4)**.
4. `start` with:
   - `resume_path` = absolute path of the tailored PDF (current version),
   - `job_title`, `company`, `job_description` from the job,
   - `scoring_guide` = `"Graduate - STEM Focus"` (default).
   Reused scans (`reused=true`, identical inputs) cost nothing.
5. `status` every **20 s** (never faster), give up after **10 min** → message "Scan timed out — scan_id kept, try again later"; resend resume card.
6. State `failed` → message with the site's reason; resend resume card. State `unknown` → message with the MCP's hint (snapshot path); **do not retry**; resend resume card.
7. `complete` → `feedback` → record the scan in `data/scans.json` (`job_id, scan_id, version, medal, date`) → send **score card (§4A.5)**.

MCP errors `auth_expired`, `site_changed`, `unknown_state`, `internal_error`, `profile_in_use` are **never retried blindly** — the user gets the error + the MCP's `hint`, and the resume card comes back so Approve/Reject still work. A scan failure is **never a system failure** (day stays marked); it only means no score this time.

#### 4A.4 Limit reset — delete the 5 oldest scans (D17)

When `scans_remaining == 0`:

1. `list` all scans (paged), oldest first.
2. `delete` the **5 oldest scans in My Scans — any scan, including ones you ran manually**. Each delete backs up its feedback first (MCP built-in, under the MCP's data dir).
3. Read `allowance_restored` from each delete and `auth` again.
4. If `scans_remaining > 0` → continue to scan.
   If still 0 (deletion did not restore allowance) → message "Deleted 5 old scans but the allowance did not reset (0/5). Scans resume at midnight." → resend resume card. **No further deletes that day.**

Constraints / required changes:

- **MCP op budget:** `scans.py:110` `DAILY_OP_BUDGET = 12` (starts + deletes) blocks this — 5 scans + 5 deletes + 5 scans = 15. **Change in the Biginterview-MCP repo:** raise to **25** (allows two reset cycles a day), ideally via env var `RESUMEAI_DAILY_OP_BUDGET`.
- **Override of MCP design:** the MCP's `PLAN.md` §14 and fact #7 treat "deletion restores allowance" as *user-observed, unverified, not depended upon*. This plan depends on it by your choice; step 4 is the safety net if it turns out false.
- **ToS risk:** deleting scans to bypass the daily limit may violate Big Interview / NEU terms and could flag the account. Accepted by you.
- Your manual scans can be deleted by this step (backed up locally first, but gone from My Scans).

#### 4A.5 Score card

```
🔍 Big Interview scan — [title] @ [company]  (v2)
Overall: 🥈 Silver

Readability  🥇 Gold
Credibility  🥉 Bronze
ATS Fit      🥈 Silver
Format       🥇 Gold

Keywords matched: 14/20

Pick what to fix, then Retailor:
Missing keywords:
[☐ Kubernetes] [☐ Terraform] [☐ CI/CD] …
Corrections (Credibility / ATS Fit):
[☐ Quantify impact in experience bullets]
[☐ Add job title keywords to summary] …

[🔁 Retailor (0 selected)]  [⬅️ Back to resume]
Scans left: 2/5
```

- **Badges:** all 4 shown.
- **Keyword toggles:** every *unmatched* ATS keyword from the scan.
- **Correction toggles:** criteria with status `needs_work` / `warning` and action items, **only from Credibility and ATS Fit** (D18). Readability / Format advice is not offered.
- Toggling edits the message in place (`editMessageReplyMarkup`) — ☐ ↔ ☑, and the Retailor button shows the count. Toggle `callback_data` = `tg:<job_id>:<nonce>:<item_idx>` (fits 64 bytes; the item list lives in `data/scans.json`, not in the callback).
- Long lists: max ~20 toggle buttons; if more, the extra items go in the message text with "(not selectable — too many)".
- `partial=true` feedback (only badges readable) → card shows badges only, no toggles, and "Feedback incomplete — retailor unavailable for this scan".
- **Back to resume** → resend the resume card (same version).
- **Retailor with 0 selected** → ack "Select at least one item".
- **Round 3 done** → Retailor button replaced by "Retailor limit reached (3/3)".
- **No tap within 1 h** → job goes to pending like any approval timeout.

#### 4A.6 Retailor (D19, D21, D22) — `tailor.retailor(job, tex, picked_keywords, picked_corrections)`

1. Copy current `vN.tex` → `vN+1.tex` in the job workdir (old version kept).
2. Sonnet (`claude -p`, `TAILOR_MODEL`, same system instructions) **edits `vN+1.tex` in place** with `RETAILOR_PROMPT`:
   - integrate exactly the picked keywords (MUST rule applies to the picked ones);
   - apply exactly the picked corrections;
   - change nothing else; keep density, XYZ bullets, preamble.
3. Same compile → 1-page trim loop → Opus escalation as Step 8.
4. Steps 9 (post-check) and 10 (verdict) re-run on the new version.
5. New resume card (Step 11) with `round = n/3` and "Prev scan: <medal>". **No auto re-scan** (D20) — you tap Scan again if you want.
6. Retailor failure (tailor `fail_code`) → message with the cause, and the **previous version's** resume card comes back (you can still approve it). Not a system failure.

The PDF that gets submitted is always the version on the card you tapped Approve on.

### Steps 12–14 — Cover letter, apply, external links (existing, unchanged)

- Cover letter generated only if required, with its own approval card.
- `auto_apply.apply_to_job()` — **serialized behind a lock** (see §5): one Playwright session at a time, human-like pace on your account.
- External links → `external_autofill` with its own Submit gate. Never solves captchas, creates accounts, or guesses EEO/visa answers.

### Step 15 — Wrap-up (existing, unchanged)

Summary log → mark day only if no system failures → `stats.record_run()` → `supervisor.run_supervisor()` consolidated report.

---

## 5. Concurrency model (required by D4)

The current code waits on one card at a time, and `tg.wait_for_decision()` **dismisses taps for any other job** ("This card is no longer active"). That makes parallel cards impossible, so this changes.

**New design:**

1. **One Telegram poller thread** (`tg.CallbackDispatcher`) owns `getUpdates`. It routes each tap by `callback_data = action:job_id:nonce` into a per-`(job_id, nonce)` queue. Taps with an unknown/stale nonce are acked "This card expired" as today.
2. **`wait_for_decision(job_id, nonce, timeout)`** keeps its signature but reads from the dispatcher's queue instead of polling itself. All existing callers keep working.
3. **Per-job worker** (`ThreadPoolExecutor`, `TAILOR_PARALLELISM` = 6): on posting Approve, runs Steps 5 → 11 for that job. Waiting on a card blocks only that worker.
4. **Apply lock:** Steps 12–14 run under a single `threading.Lock` — only one browser session against NUworks at a time.
5. **Watchdog:** budget = `POSTING_WINDOW (1h) + HYBRID_BUDGET + APPROVAL_TIMEOUT + APPLY_BUDGET × n_jobs`, bumped as jobs progress. `HYBRID_BUDGET` = keyword attempts + `tailor.max_total_attempts()` × timeout.
6. **New callback actions:** `approve` / `reject` (posting, resume, cover letter — unchanged scheme), `lowok` / `lowno` (low-match card), `scan` (resume card), `tg` (score-card toggle), `retailor`, `back` (score card). All fit Telegram's 64-byte `callback_data` limit.
7. **Scan lock:** all Big Interview calls serialized behind `SCAN_LOCK` (§4A.2). Toggle taps on a score card are handled by the dispatcher directly (message edit), without waking the job worker until Retailor / Back.
8. **Watchdog:** each Scan tap bumps by 10 min (scan) + 5 min (delete/reset); each Retailor by `tailor.max_total_attempts() × timeout`; each new card by `APPROVAL_TIMEOUT`.

---

## 6. Failure posture matrix

| Event | Telegram | Job outcome | Day |
|---|---|---|---|
| Base ATS red after repair | 1 alert for whole run | all jobs untouched | **unmarked** |
| Posting Reject | — | skiplist | marked |
| Posting no tap (1 h) | — | pending → next run | marked |
| Haiku fails | — | continues, `Match n/a` | marked |
| Low-match Skip | — | skiplist | marked |
| Low-match no tap (10 min) | — | pending → next run | marked |
| Tailor fails | alert w/ cause | `tailor_failed` | **unmarked** |
| Post-check critical fail | alert | `postcheck_failed` | **unmarked** |
| Scan: login expired / MCP error / timeout / failed | message + hint | resume card resent (no score) | marked |
| Scan limit 0, delete-5 doesn't restore | message | resume card resent | marked |
| Score card no tap (1 h) | — | pending → next run | marked |
| Retailor fails | message w/ cause | previous version's card resent | marked |
| Resume / cover-letter Reject | — | skiplist / rejected | marked |
| Resume / cover-letter no tap | — | pending | marked |
| Apply error | supervisor report | `error` | **unmarked** |

Rule: **system failure → alert + day unmarked; a decision of yours (or silence) → quiet, day marked.**

---

## 7. Config (`config.json` → new `agents` section)

| Key | Default | Meaning |
|---|---|---|
| `keyword_model` | `claude-haiku-5-5` | Haiku model for Step 5 |
| `keyword_attempts` | 2 | Haiku retries before degrading |
| `min_match_score` | 0.40 | below → low-match card |
| `min_keywords` | 5 | fewer surviving keywords → `Match n/a` |
| `low_match_timeout_s` | 600 | low-match card window |
| `posting_timeout_s` | 3600 | posting card window |
| `ats_fix_attempts` | 3 | base repair loop bound |
| `ats_reaudit_days` | 30 | forced base re-audit interval |
| `xyz_bullets_per_role` | 2 | XYZ scope (D11) |
| `bi_mcp_dir` | `~/Desktop/Claude-env/Biginterview-MCP/Biginterview-MCP` | where `uv run` finds the MCP env |
| `bi_scoring_guide` | `Graduate - STEM Focus` | scan scoring guide |
| `bi_poll_s` / `bi_scan_timeout_s` | 20 / 600 | poll interval / give-up |
| `bi_reset_delete_count` | 5 | scans deleted on limit reset (D17) |
| `retailor_max_rounds` | 3 | D21 |
| `bi_correction_categories` | `["credibility", "ats_fit"]` | which categories offer correction toggles (D18) |
| `bi_max_toggles` | 20 | button cap on the score card |

Tailor models stay where they are (`tailor.TAILOR_MODEL = claude-sonnet-4-6`, `ESCALATION_MODEL = claude-opus-4-8`).

---

## 8. File changes

| File | Change |
|---|---|
| `resume_agents.py` | **New.** `run_ats_checks`, `evaluate_extraction`, `audit_bases_cached` (+ repair loop + preamble guard), `extract_keywords`, `_parse_keywords_json`, `score_keywords`, `SYNONYMS`, `verify_tailored_pdf`, `build_verdict` |
| `telegram_bot.py` | Reformat `send_job_for_approval` to the §4 Step 4 card; add `job_posting_url()`; add `send_low_match_card`; add `CallbackDispatcher`; `wait_for_decision` reads from dispatcher |
| `pipeline.py` | Replace serial `gate_by_posting` with send-all + per-job workers; call `audit_bases_cached` before cards; new `_run_job(job)` (Steps 5–14); apply lock; new results `skipped_low_match`, `postcheck_failed`; watchdog budget |
| `tailor.py` | `tailor_with_claude(..., keywords=None)`; `stage()` uses ATS-repaired base when yellow; `XYZ_BULLET_GUIDANCE`; `xyz_bullet_stats` |
| `resume_templates/system_instructions.md` | "use supplied keyword list" rule; new §4C XYZ (top 1–2 per role); 4C–4E renumbered |
| `config.json` | `agents` section (§7) |
| `bi_scan.py` | **New.** Tiny MCP stdio client (`auth/start/status/feedback/list/delete`), run via the MCP's `uv` env, prints JSON |
| `telegram_bot.py` (more) | Scan button + `scans_left` on resume card; `send_score_card`; toggle handling via `editMessageReplyMarkup`; `scan`/`tg`/`retailor`/`back` actions |
| `pipeline.py` (more) | `_scan_and_retailor()` loop, `SCAN_LOCK`, limit-reset logic, `data/scans.json` records, version tracking per job |
| `tailor.py` (more) | `retailor(job, tex, picked_keywords, picked_corrections)` + `RETAILOR_PROMPT`; versioned `vN.tex` |
| **Biginterview-MCP repo** | `src/resumeai_mcp/scans.py:110` — `DAILY_OP_BUDGET` 12 → 25, read from env `RESUMEAI_DAILY_OP_BUDGET` |
| `tests/test_hybrid.py` | **New.** Offline, LLM mocked (see §10) |
| `data/ats_audit.json`, `data/ats_audit_cache/` | Runtime; already covered by `data/*` in `.gitignore` |

Not touched: `job_scraper.py`, `apply_type.py`, `auto_apply.py`, `cover_letter.py`, `external_*`, `supervisor.py`, `stats.py`, `digest.py`.

Starting point: the current **uncommitted** changes in `pipeline.py` / `telegram_bot.py` (pause flag, scrape pgid kill, `gate_by_posting`, `send_job_for_approval`, Telegram retry) carry into the branch. Untracked scripts (`duo_probe_tmp.py`, `resubmit_teradar.py`, `supporting_doc_apply.py`) are left alone.

---

## 9. Invariants — must not break

1. **No fabrication** — XYZ metrics only from real base content; never invent numbers. (Note: D12 keeps "MUST integrate every keyword"; review must-haves you lack carefully at the resume approval tap.)
2. **Preamble / geometry / fonts / `\vspace` never modified** — enforced by Python in the ATS repair loop, and by prompt in the tailor (existing).
3. **LLM proposes, Python verifies** — keywords checked against JD; ATS checks re-run after every repair and after tailoring.
4. **One writer** — only `tailor_with_claude` writes the tailored resume.
5. **Human-in-the-loop** — posting Approve before any LLM call; resume Approve before any submit; cover letter has its own approval; nonces block stale taps.
6. **Failure posture** — §6.
7. **Never** solve captchas, create accounts, or guess EEO/visa answers; one browser session at a time, human pace.
8. **Your base templates are never written to** — repairs live in `data/ats_audit_cache/` only.
9. **Big Interview pacing** — one scan in flight, no parallel tool calls, poll ≥ 20 s, never blindly retry `auth_expired` / `site_changed` / `unknown_state` / `internal_error` / `profile_in_use`.
10. **Nothing is deleted without a backup** — scan deletes go through `delete_scan`, which backs up feedback first.
11. **Retailor only adds what you ticked** — the scan's unmatched keywords are never added automatically.

---

## 10. Tests — `tests/test_hybrid.py` (offline, `claude -p` mocked)

- `evaluate_extraction`: each of the 7 checks pass + fail.
- Preamble guard rejects a repair that edits the preamble.
- `audit_bases_cached`: cache hit skips work; changed sha re-audits; 30-day expiry.
- `_parse_keywords_json`: clean JSON, JSON wrapped in prose, garbage → error.
- `score_keywords`: drops keywords not in JD; synonyms; word-boundary (`Java` vs `JavaScript`, `Go`, `R`); better-of-two-bases; thin-JD → `None`; the 44% worked example.
- `CallbackDispatcher`: two jobs' taps routed to the right waiter; stale nonce ignored.
- Posting card text contains all 7 fields; `External apply: No` when empty.
- `build_verdict` ≤ 400 chars; `n/a` and escalated variants.
- `tailor_with_claude(keywords=None)` prompt identical to today (backward compatible).
- `bi_scan.py` (MCP mocked): JSON in/out per command; error passthrough with `hint`.
- Scan flow: poll interval ≥ 20 s; 10-min timeout; `unknown` not retried; login-expired path hides button.
- Limit reset: deletes exactly 5 oldest; stops when allowance not restored; never runs twice a day.
- Score card: toggles only from Credibility / ATS Fit; toggle flips ☐/☑; 0-selected Retailor refused; >20 items overflow to text; `partial=true` → no toggles.
- Retailor: copies vN → vN+1; prompt contains only picked items; round cap 3; failure falls back to previous version's card.
- `SCAN_LOCK`: two jobs' scans never overlap.

Run: `python3 -m unittest tests.test_hybrid`, plus existing suites (`test_pipeline`, `test_supervisor`, `test_login_classify`, `test_telegram_retry`) on the Mac.

---

## 11. Rollout & verification

1. Build on `feat/hybrid-resume-pipeline`; unit tests green.
2. Verify the job posting URL format against the live portal.
3. Live trial: `python3 pipeline.py --limit 2 --dry-apply`. Check:
   - posting cards arrive together with all 7 fields; link opens;
   - Approve on one starts it while the other card is still pending;
   - `data/ats_audit.json` written; second run is a cache hit;
   - workdir has Haiku JSON + tailor log; tailor used the supplied keywords;
   - low-match card appears for a deliberately poor-fit job;
   - verdict line on the resume card;
   - only one browser session runs during apply;
   - Scan button shows correct `n/5 left`; a scan completes and the score card shows 4 badges + toggles from Credibility / ATS Fit only;
   - ticking items + Retailor produces v2 with only those changes, and a new resume card with "retailor 1/3";
   - Approve on v2 submits v2's PDF.
4. Limit-reset trial (separately, when you're at 0/5): confirm whether deleting 5 scans actually restores the allowance. If it doesn't, decide whether to keep the step.
5. One week of normal runs, then review open items (§12).
6. PR to `main` (this repo) + separate PR for the `DAILY_OP_BUDGET` change (Biginterview-MCP repo).

---

## 12. Known limitations / open items

- **40% threshold** is a starting guess — tune after a week of real scores.
- **Haiku keyword quality** now drives the resume. The JD-presence check guards against invented terms, not missed ones. Optional: for the first week, log the tailor's own keyword line next to Haiku's list for comparison.
- **"MUST integrate every keyword" (D12)** can push the tailor to claim must-haves you don't have — watch for this at the resume approval tap.
- **XYZ** may still increase page overflow / Opus escalations — watch the `escalated` rate in the verdict line.
- **Synonym map** is small and hand-written; extend it when a score looks wrong.
- **Posting link URL** format unverified until build.
- `pdflatex` + `pdftotext` must be installed where the pipeline runs (the Mac).
- **Scan-limit reset (D17) is unverified** — depends on deleting scans restoring allowance, which the MCP's own plan marks as not depended upon; also carries ToS / account-flag risk and deletes manual scans.
- **`profile_in_use`:** the MCP drives a persistent browser profile. If the Claude desktop app's `resumeai` server (or `scripts/login.py`) holds it while the pipeline scans, the scan fails with `profile_in_use` — handled as a non-retried error, but may happen often if both run together.
- Score-card text depends on Big Interview's UI; a site change surfaces as `site_changed` from the MCP.
- Base-variant agreement (LLM pick vs Python guess) is logged — if they agree almost always, Python could pin it later.
