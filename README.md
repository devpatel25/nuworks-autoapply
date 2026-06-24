# NUworks Auto-Apply

Automated job-application pipeline for Northeastern's **NUworks (Symplicity)** career
portal. Every day it scrapes new postings, tailors a one-page résumé per job, sends each
one to **Telegram for your approval**, and — only after you tap **Approve** — submits the
application through the portal's Apply flow.

> **Human-in-the-loop by design.** Nothing is ever submitted to an employer without an
> explicit Telegram approval, even on the scheduled run. A required screening question the
> bot can't answer from your profile blocks the submit.

This is a personal-automation project shared for others to adapt. It drives a real browser
session against your own school portal with your own credentials — use it responsibly and
review every application before approving.

---

## How it works

```
scrape (last 24h)  ─▶ classify apply-type ─▶ drop already-applied / skip-listed
   └▶ tailor ALL résumés in parallel  (SWE / AI-ML base + JD keywords → compiled to 1 page)
        └▶ Telegram: send each PDF with [✅ Approve] [❌ Reject]
              ├─ Approve ─▶ portal Apply → upload résumé → Submit → verify "Applied"
              └─ Reject / no response (1h) ─▶ skip (re-queued for next run)
```

- **Scrape** (`job_scraper.py`) logs in via SSO + DUO and pulls the last 24h of postings into `data/jobs.csv` (title, company, location, full description).
- **Classify** (`apply_type.py`) keeps only in-portal ("online") jobs and drops ones already applied to.
- **Tailor in parallel** (`tailor.py`, orchestrated in `pipeline.py`) runs the ResumeBot2.1 process via headless `claude -p`: pick the SWE or AI-ML base variant, weave in the job's keywords **without inventing experience**, compile with `pdflatex`, and enforce exactly one page. All eligible résumés are tailored **concurrently up front** rather than one at a time.
- **Approve** (`telegram_bot.py`) sends each résumé with Approve/Reject buttons and waits (up to 1h) for your tap.
- **Apply** (`auto_apply.py`) uploads the résumé, answers screening questions deterministically from `data/profile.json` (claude drafts the rest, shown for review), submits, and verifies. Cover letters (`cover_letter.py`) are generated only when a posting requires one. External-ATS links are auto-filled and gated separately (`external_apply.py` / `external_autofill.py`).

Watchdog timeouts, a single-instance lock, and a once-per-day guard make unattended runs safe; a failed run leaves the day unmarked so it retries.

---

## Requirements

- **Python 3.10+** with the packages in `requirements.txt` (`pip install -r requirements.txt`)
- **Playwright Chromium**: `playwright install chromium`
- **TeX Live** (`pdflatex`) and **poppler** (`pdfinfo`) for résumé compilation
- The **`claude` CLI** (Claude Code), logged in once interactively — used for unattended tailoring
- A NUworks/Symplicity account and a Telegram bot (via **@BotFather**)

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

cp .env.example .env                 # fill in NEU login + Telegram bot token/chat id
cp data/profile.example.json data/profile.json          # your contact + work-auth answers
cp resume_templates/Resume_SWE_General.example.tex   resume_templates/Resume_SWE_General.tex
cp resume_templates/Resume_AI-ML_General.example.tex resume_templates/Resume_AI-ML_General.tex
# then edit the two .tex files with YOUR real one-page résumé content
```

Everything personal (`.env`, `session.json`, `data/`, `resumes/`, `applications/`,
`cover_letters/`, `documents/`, your filled `resume_templates/*.tex`) is gitignored — only
the `*.example.*` templates are tracked.

## Running

```bash
python3 pipeline.py                 # full run: scrape → tailor (parallel) → approve → apply
python3 pipeline.py --no-scrape     # reuse existing data/jobs.csv
python3 pipeline.py --dry-apply     # stage in the portal but never click Submit (safe test)
python3 pipeline.py --limit N       # only process the first N eligible jobs
```

> Note: a nested `claude -p` is blocked inside an interactive Claude Code session, so real
> tailoring runs from a normal terminal or the scheduled job.

## Scheduling (daily, unattended)

Run `pipeline.py` on a schedule with your OS scheduler. On macOS, a **launchd** agent in
`~/Library/LaunchAgents/` works well (`StartCalendarInterval` for the times, `RunAtLoad`
to catch up after sleep); on Linux use cron/systemd. Wrap it in `caffeinate -i` on a laptop
so it doesn't stall while asleep. The schedule file is intentionally **not** committed (it
hardcodes machine paths) — create your own.

Unattended runs need: the Mac awake and logged in at run time, the `claude` CLI logged in,
and you available to approve the DUO push + the Telegram cards.

## Tuning

| What | Where |
|------|-------|
| Search filters (job types, recency) | `API_PARAMS` in `job_scraper.py` |
| Résumé tailoring rules | `resume_templates/system_instructions.md` |
| Parallel tailoring workers | `NUWORKS_TAILOR_PARALLELISM` env (default 6) |
| Per-job approval wait | `APPROVAL_TIMEOUT` in `pipeline.py` |
| DUO wait window | `DUO_WAIT_SECONDS` env / `job_scraper.py` |

## Caveats

- **Fit isn't judged automatically** — it applies to any eligible in-portal posting it's pointed at. Review the résumé on each Telegram card before approving.
- Only in-portal jobs are auto-applied; email/external jobs are flagged or routed to the external-ATS autofill bridge.
- Automating applications may be against a portal's terms — keep volume low and a human in the loop.
