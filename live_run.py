#!/usr/bin/env python3
"""Live run: send tailored resumes for the strong-fit SWE jobs to Telegram,
then real-submit each one the user Approves. Multi-job poller so both can be
tapped at the user's pace within one run."""
import csv
import time
import asyncio
from pathlib import Path

import requests
import telegram_bot as tg
import auto_apply

# (job_id_prefix, resume_pdf, upload_label, keyword_summary)
JOBS = [
    ("78df4511",
     "resumes/2026-06-03__Resume_SoftwareEngineerIntern_Peferd.pdf",
     "Resume_SoftwareEngineerIntern_Peferd",
     "Variant: SWE | Python, test automation, regression, CI/CD, deployment automation, observability, backend reliability"),
    ("3c2a9818",
     "applications/2026-06-03/3c2a9818/Resume_SoftwareEngineeringCoOp_SoftwareVelocityCorporation.pdf",
     "Resume_SoftwareEngineeringCoOp_SoftwareVelocityCorporation",
     "Variant: SWE | React, Node.js, REST APIs, AWS, Docker, CI/CD, test automation, OpenAI"),
]

rows = {r["job_id"][:8]: r for r in csv.DictReader(open("data/jobs.csv"))}
meta, pending = {}, set()

print("Sending tailored resumes to Telegram for approval...")
for jid8, pdf, label, summary in JOBS:
    job = rows[jid8]
    tg.send_resume_for_approval(pdf, job, summary=summary)
    meta[job["job_id"]] = (job, pdf, label)
    pending.add(job["job_id"])
    print(f"  sent: {job['title'][:50]}")

tg.send_message("⚠️ <b>Live run.</b> Approve = real application submitted to that employer on NUworks.")
print(f"Waiting for your taps (up to ~8 min) on {len(pending)} job(s)...")

results = {}
deadline = time.time() + 480
offset = None
while pending and time.time() < deadline:
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{tg.API}/getUpdates", params=params, timeout=40).json()
    except requests.RequestException:
        time.sleep(2); continue
    for upd in r.get("result", []):
        offset = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq or ":" not in cq.get("data", ""):
            continue
        action, jid = cq["data"].split(":", 1)
        if jid not in pending:
            continue
        requests.post(f"{tg.API}/answerCallbackQuery",
                      data={"callback_query_id": cq["id"],
                            "text": "Approved ✅" if action == "approve" else "Rejected ❌"}, timeout=20)
        job, pdf, label = meta[jid]
        title = job["title"][:50]
        if action == "approve":
            print(f">>> REAL SUBMIT: {title}")
            res = asyncio.run(auto_apply.apply_to_job(jid, pdf, label, mode="submit"))
            results[jid] = res["status"]
            icon = "✅ Applied" if res["status"] == "applied" else f"⚠️ {res['status']}"
            tg.send_message(f"{icon}: {title}\n{res['detail']}")
            print(f"    -> {res['status']}: {res['detail']}")
        else:
            results[jid] = "rejected"
            tg.send_message(f"⏭️ Skipped: {title}")
            print(f"    -> rejected")
        pending.discard(jid)
    time.sleep(2)

for jid in pending:
    results[jid] = "no_response"

print("\n=== SUMMARY ===")
for jid, (job, _, _) in meta.items():
    print(f"  {results.get(jid,'?'):<14} {job['title'][:50]}")
tg.send_message("🏁 Live run complete:\n" +
                "\n".join(f"{results.get(j,'?')}: {meta[j][0]['title'][:40]}" for j in meta))
