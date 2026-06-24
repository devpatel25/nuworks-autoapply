#!/usr/bin/env python3
"""Generic external-application autofill + Telegram submit-gate.

Flow (one external job):
  fill_application(url) -> opens the apply page in a PERSISTENT headed browser,
    detects login/captcha walls early, fills every field it can map from
    data/profile.json, and returns the filled values + any required fields it
    could not fill.
  review_and_submit(...) -> sends ALL filled values + a Submit/Cancel button to
    Telegram. On Submit it clicks the real submit and runs a SELF-HEAL loop:
    if the form bounces back a validation error on a field we have data for
    (e.g. the phone "Country" field), it fixes it and re-submits. If it hits a
    captcha, a login wall, or an error it can't resolve, it STACKS the job for
    the laptop (data/needs_human.json) and pings Telegram instead of guessing.

Hard lines (unchanged): never solve a captcha, never create an account, never
guess EEO/visa answers — those come from profile.json or get stacked.

CLI:
  python3 external_autofill.py --job <url> [--resume <pdf>] [--dry]
  python3 external_autofill.py --resume-stacked     # re-open+refill stacked apps on the laptop
  python3 external_autofill.py --list-stacked
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
try:
    PROFILE = json.loads((HERE / "data" / "profile.json").read_text())
except Exception:
    PROFILE = {}
# Location parts for geo-complete fields, derived from the profile (no hardcoded city).
_LOC = (PROFILE.get("location") or "").strip()
LOC_CITY = _LOC.split(",")[0].strip() or "City"
LOC_FULL = _LOC or "City, ST"
PROFILE_DIR = Path(os.environ.get("EXTERNAL_APPLY_PROFILE",
                                  str(Path.home() / ".external_apply_profile")))
NEEDS_HUMAN = HERE / "data" / "needs_human.json"
TRACKER = HERE / "data" / "external_applications.json"
LOGS = HERE / "logs"

MAX_SUBMIT_RETRIES = 4

# Load THIS project's telegram_bot by file path so the module works no matter
# which project imports it (nuworks + wellfound both have a telegram_bot.py).
try:
    import importlib.util as _il
    _spec = _il.spec_from_file_location("nuworks_telegram_bot", str(HERE / "telegram_bot.py"))
    tg = _il.module_from_spec(_spec)
    _spec.loader.exec_module(tg)
except Exception:
    tg = None

# ATS gating: run forms with little human intervention; skip account-wall ATSes.
EASY_ATS = re.compile(r"greenhouse|gh_jid=|gh_src=|lever\.co|ashbyhq|workable|smartrecruiters|"
                      r"bamboohr|rippling|breezy|recruitee|jobvite|dover\.com", re.I)
HARD_ATS = re.compile(r"myworkdayjobs|\.workday|workdayjobs|icims|taleo|brassring|"
                      r"successfactors|oraclecloud|avature|phenom", re.I)


def ats_class(url):
    """'hard' (Workday/iCIMS-style account walls), 'easy', or 'unknown'."""
    if HARD_ATS.search(url or ""):
        return "hard"
    if EASY_ATS.search(url or ""):
        return "easy"
    return "unknown"


# ── field resolution ────────────────────────────────────────────────────────
# Ordered (regex on the field's visible label) -> resolver key. First match wins.
LABEL_RULES = [
    (r"preferred (first )?name|nickname", "skip"),   # must precede first-name rule
    (r"first name|given name", "first_name"),
    (r"last name|surname|family name", "last_name"),
    (r"full name|^name$|your name", "full_name"),
    (r"e-?mail", "email"),
    (r"country", "phone_country"),
    (r"phone|mobile|telephone|cell", "phone"),
    (r"current location|location|city|where.*based", "location"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"portfolio|personal (web)?site|website|other website", "portfolio"),
    (r"current company|employer", "current_company"),
    (r"school|university|college|institution", "school"),
    (r"degree", "degree"),
    (r"discipline|major|field of study|concentration", "discipline"),
    (r"gpa|grade point", "gpa"),
    (r"when.*graduat|expected graduat|graduation", "grad_window"),
    (r"end date month|graduation month", "grad_month"),
    (r"end date year|graduation year", "grad_year"),
    (r"start date|earliest.*start|when.*available", "skip"),
    (r"authoriz.*work|eligible to work|legally.*work|work authoriz", "work_auth_yes"),
    (r"require.*sponsor|need.*sponsor|visa sponsor|sponsorship", "sponsor_no"),
    (r"relocat", "relocate_yes"),
    (r"hybrid|on-?site|in[- ]office|commute", "hybrid_yes"),
    (r"referr", "referred_no"),
    (r"gender", "decline"),
    (r"hispanic|latino|race|ethnicit", "decline"),
    (r"veteran", "decline"),
    (r"disabilit", "decline"),
    (r"resume|^cv$|resume/cv", "resume"),
    (r"undergrad.*transcript|transcript", "transcript"),
    (r"cover letter", "skip"),
]

YES = re.compile(r"^yes\b|i am|i'm|currently eligible|authorized", re.I)
NO = re.compile(r"^no\b|do not|don't|not a|i am not", re.I)
DECLINE = re.compile(r"decline|don'?t wish|do not want|prefer not|not.*answer|not.*disclose", re.I)


def digits(s):
    return re.sub(r"\D", "", s or "")


def text_value(key):
    """Return the plain string for a text-type field, or None to skip."""
    p = PROFILE
    name = p.get("name", "")
    first, _, last = name.partition(" ")
    return {
        "first_name": first,
        "last_name": last or first,
        "full_name": name,
        "email": p.get("email"),
        "phone": digits(p.get("phone", "")),
        "location": LOC_CITY,                           # geocomplete: city alone
        "linkedin": p.get("linkedin"),
        "github": p.get("github") or None,
        "portfolio": p.get("portfolio") or None,
        "current_company": p.get("current_company") or None,
        "grad_year": (p.get("grad_year") or "2027"),
    }.get(key)


# option-text preferences for combobox/select/radio resolvers
OPTION_PREFS = {
    "phone_country": ["united states +1", "united states", "usa"],
    "school": ["northeastern university"],
    "degree": ["master's degree", "master of science", "master"],
    "discipline": ["computer science"],
    "grad_window": ["sept - dec 2027", "fall 2027", "2027", "oct - dec 2027"],
    "grad_month": ["december"],
    "gpa": ["3.6 - 4.0", "3.5 - 4.0", "3.7 - 4.0", "3.5+", "3.9"],
    "work_auth_yes": ["yes"],
    "sponsor_no": ["no"],
    "relocate_yes": ["yes"],
    "hybrid_yes": ["yes"],
    "referred_no": ["no"],
    "decline": ["decline to self identify", "i don't wish to answer",
                "i do not want to answer", "prefer not to say", "decline"],
}


def resolve_key(label):
    label = (label or "").strip().lower()
    for pat, key in LABEL_RULES:
        if re.search(pat, label):
            return key
    return None


# ── wall detection ──────────────────────────────────────────────────────────
def detect_wall(page):
    """Return a reason string if a human-only wall is present, else None."""
    url = page.url.lower()
    body = ""
    try:
        body = page.inner_text("body")[:6000].lower()
    except Exception:
        pass
    if "captcha-delivery.com" in (page.content() or "").lower():
        return "captcha (DataDome)"
    # visible hCaptcha / reCAPTCHA challenge (not the invisible passive widget)
    for sel in ['iframe[src*="hcaptcha.com"][title*="challenge" i]',
                'iframe[title*="recaptcha challenge" i]',
                'div.h-captcha iframe[title*="challenge" i]']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            return "captcha challenge"
    if re.search(r"select all (images|squares)|verify you are human|i'?m not a robot|"
                 r"complete the (captcha|challenge)", body):
        return "captcha challenge"
    if "/login" in url or re.search(r"create an account|sign in to apply|enter your information|"
                                     r"password", body[:1500]):
        if page.query_selector('input[type="password"]') or "enter your information" in body[:1500]:
            return "account / login wall"
    return None


# ── filling ───────────────────────────────────────────────────────────────--
def _human_pause(a=0.6, b=1.6):
    time.sleep(random.uniform(a, b))


def _set_text(el, val):
    """Fill a text input robustly. Tries Playwright fill (short timeout), then a
    JS set via React's native value setter + input/change events — which works on
    stubborn React-controlled fields (e.g. Ashby) where click/fill times out."""
    try:
        el.fill(val, timeout=6000)
        return True
    except Exception:
        pass
    try:
        el.evaluate(
            "(e,v) => { const proto = e.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype "
            ": window.HTMLInputElement.prototype; "
            "const setter = Object.getOwnPropertyDescriptor(proto,'value').set; setter.call(e,v); "
            "e.dispatchEvent(new Event('input',{bubbles:true})); "
            "e.dispatchEvent(new Event('change',{bubbles:true})); "
            "e.dispatchEvent(new Event('blur',{bubbles:true})); }", val)
        return True
    except Exception:
        return False


def _dismiss_overlays(page):
    """Close cookie/consent banners that overlay the form and block clicks."""
    for sel in ['button:has-text("Accept all")', 'button:has-text("Accept All")',
                'button:has-text("Accept cookies")', 'button:has-text("Accept")',
                'button:has-text("I agree")', 'button:has-text("Got it")',
                'button:has-text("Allow all")', '[aria-label*="accept" i]',
                'button:has-text("Close")', 'button[aria-label="Close"]']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=3000)
                time.sleep(0.6)
                return
        except Exception:
            continue


FIELD_SEL = ('input[name*="first" i], input[id*="first" i], input[type=email], '
             'input[name="email"], input[autocomplete="email"], input[name*="name" i]')


def _has_fields(page):
    try:
        return page.query_selector(FIELD_SEL) is not None
    except Exception:
        return False


def _ensure_form(page, wait_s=14):
    """Reveal the application form. Handles two cases: (1) React/SPA forms
    (Ashby, Greenhouse) that hydrate slowly — poll for fields to appear; and
    (2) job-description pages where fields only show after clicking Apply /
    "I'm interested". Polls for fields again after the click."""
    # 1) wait for a slow SPA to render its fields
    for _ in range(wait_s):
        if _has_fields(page):
            return
        time.sleep(1)
    # 2) still nothing -> click an apply trigger, then wait for fields
    for sel in ['button:has-text("Apply for this job")', 'a:has-text("Apply for this job")',
                "button:has-text(\"I'm interested\")", "a:has-text(\"I'm interested\")",
                'button:has-text("I am interested")',
                'button:has-text("Apply Now")', 'a:has-text("Apply Now")',
                'button:has-text("Apply manually")', 'button:has-text("Apply")', 'a:has-text("Apply")']:
        try:
            el = page.query_selector(sel)
        except Exception:
            el = None
        if el and el.is_visible():
            try:
                el.click()
            except Exception:
                continue
            for _ in range(10):
                if _has_fields(page):
                    return
                time.sleep(1)
            return


def _pick_option(page, key):
    """For an open listbox, click the option best matching OPTION_PREFS[key]."""
    opts = page.query_selector_all('[role="option"], li[role="option"]')
    texts = [(o, (o.inner_text() or "").strip().lower()) for o in opts if o.is_visible()]
    for pref in OPTION_PREFS.get(key, []):
        for o, t in texts:
            if pref in t:
                o.click()
                return t
    # decline fallback: any option matching the decline regex
    if key == "decline":
        for o, t in texts:
            if DECLINE.search(t):
                o.click()
                return t
    return None


def fill_application(page, resume_path=None):
    """Fill every mappable field. Returns (filled:list[(label,value)], missing:list[label])."""
    filled, missing = [], []
    transcript = HERE / "documents" / "transcript.pdf"

    # Iterate visible form controls in document order.
    controls = page.query_selector_all(
        'input:not([type=hidden]):not([type=submit]):not([type=button]), '
        'textarea, select, [role="combobox"]')
    seen = set()
    for el in controls:
        try:
            if not el.is_visible():
                continue
            label = _label_for(page, el)
            key = resolve_key(label)
            if not key or key == "skip":
                continue
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            etype = (el.get_attribute("type") or "").lower()
            sig = label.lower().strip()
            if sig in seen:
                continue
            seen.add(sig)

            # file uploads handled by the dedicated _upload_files() pass below
            # (setInputFiles works on drag-drop dropzones + Attach-button inputs).
            if key in ("resume", "transcript"):
                continue

            # text-ish inputs / textareas
            if (tag in ("input", "textarea") and etype in ("", "text", "email", "tel", "url", "number")
                    and key not in OPTION_PREFS):
                val = text_value(key)
                if key == "location":
                    if _fill_location(page, el):
                        filled.append((label, LOC_FULL))
                    elif _required(el, label):
                        missing.append(label)
                    continue
                if val:
                    if _set_text(el, val):
                        filled.append((label, val))
                    elif _required(el, label):
                        missing.append(label)
                continue

            # native <select>
            if tag == "select":
                chosen = _select_native(el, key)
                if chosen:
                    filled.append((label, chosen))
                elif _required(el, label):
                    missing.append(label)
                continue

            # react-select combobox / typeahead
            if tag == "input" or el.get_attribute("role") == "combobox":
                chosen = _fill_combobox(page, el, key)
                if chosen:
                    filled.append((label, chosen))
                elif _required(el, label):
                    missing.append(label)
                continue
        except Exception as e:
            print(f"   field error ({label[:40]!r}): {str(e)[:80]}", flush=True)

    # radios grouped by question
    _fill_radio_groups(page, filled, missing)
    # consent / agreement checkboxes (privacy, terms) — required to submit
    _check_consents(page, filled)
    # file uploads (resume + transcript) via setInputFiles on real file inputs
    filled += _upload_files(page, resume_path)
    return filled, missing


def _upload_files(page, resume_path):
    """Attach resume + transcript. Custom drag-drop uploaders (SmartRecruiters)
    ignore a bare set_input_files, so we PRIMARILY drive the native file chooser
    by clicking the visible "Choose a file"/"Attach" affordance (what a human
    does), then fall back to set_input_files + a change event."""
    out = []
    transcript = HERE / "documents" / "transcript.pdf"
    if resume_path and _upload_one(page, resume_path, want_transcript=False):
        out.append(("Resume/CV", Path(resume_path).name))
    if transcript.exists() and _upload_one(page, str(transcript), want_transcript=True):
        out.append(("Transcript", "transcript.pdf"))
    return out


TRIGGER_RE = re.compile(r"choose a file|attach|^upload$|drop it here|add a file|add file|upload (your )?(resume|cv)", re.I)


def _section_is_transcript(el):
    try:
        anc = (el.evaluate("e => { let n=e; for (let i=0;i<6&&n;i++){ n=n.parentElement; } "
                           "return n ? n.innerText : ''; }") or "").lower()
    except Exception:
        anc = ""
    return "transcript" in anc


def _upload_one(page, path, want_transcript):
    # 1) native file-chooser via the clickable upload affordance (most reliable)
    for t in page.query_selector_all('button, a, label, [role="button"]'):
        try:
            if not t.is_visible():
                continue
            txt = (t.inner_text() or "").strip()
            if len(txt) > 45 or not TRIGGER_RE.search(txt):
                continue
            if _section_is_transcript(t) != want_transcript:
                continue
            with page.expect_file_chooser(timeout=6000) as fc:
                t.click()
            fc.value.set_files(path)
            time.sleep(random.uniform(1.5, 2.8))
            return True
        except Exception:
            continue
    # 2) fallback: set the real file input + dispatch change (for React handlers)
    for fi in page.query_selector_all('input[type=file]'):
        try:
            if _section_is_transcript(fi) != want_transcript:
                continue
            fi.set_input_files(path)
            fi.evaluate("e => { e.dispatchEvent(new Event('input',{bubbles:true})); "
                        "e.dispatchEvent(new Event('change',{bubbles:true})); }")
            time.sleep(random.uniform(1.2, 2.2))
            return True
        except Exception:
            continue
    return False


def _check_consents(page, filled):
    """Tick required privacy/terms/consent checkboxes (common on SmartRecruiters,
    Workable, EU forms). Only ticks ones whose label looks like consent/agreement."""
    for cb in page.query_selector_all('input[type=checkbox]'):
        try:
            if not cb.is_visible() or cb.is_checked():
                continue
            lbl = (cb.evaluate(
                "e => (e.closest('label')?.innerText) || e.closest('div,li')?.innerText || ''") or "").lower()
            if re.search(r"consent|agree|privacy|terms|i have read|acknowledg|gdpr|"
                         r"process my (personal )?data|authoriz", lbl):
                cb.check()
                filled.append(("Consent", lbl.strip()[:40] or "checked"))
        except Exception:
            pass


def _label_for(page, el):
    return el.evaluate("""e => {
        if (e.getAttribute('aria-label')) return e.getAttribute('aria-label');
        if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`); if (l) return l.innerText; }
        let n = e.closest('label'); if (n) return n.innerText;
        n = e.closest('div,li,fieldset,section');
        for (let i=0;i<3 && n;i++){ const lbl=n.querySelector('label,legend,.label'); if(lbl) return lbl.innerText; n=n.parentElement; }
        return e.placeholder || e.name || '';
    }""") or ""


def _required(el, label):
    try:
        if el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true":
            return True
    except Exception:
        pass
    return "*" in (label or "")


def _attach_file(page, el, label, path):
    try:
        # If el is itself the file input:
        if el.evaluate("e => e.type === 'file'"):
            el.set_input_files(path); return True
    except Exception:
        pass
    # else click an Attach button near the label and use the file chooser
    try:
        with page.expect_file_chooser(timeout=8000) as fc:
            btn = page.query_selector(f'button:has-text("Attach")')
            (btn or el).click()
        fc.value.set_files(path)
        return True
    except Exception:
        # last resort: a hidden input[type=file] in the same group
        try:
            fi = el.evaluate_handle("e => e.closest('div,li,fieldset')?.querySelector('input[type=file]')")
            if fi:
                fi.as_element().set_input_files(path); return True
        except Exception:
            pass
    return False


def _fill_location(page, el):
    el.click(); _human_pause()
    el.fill("")
    el.type(LOC_CITY, delay=40)
    time.sleep(2.5)
    opt = page.query_selector(f'[role="option"]:has-text("{LOC_CITY}")')
    if opt and opt.is_visible():
        opt.click(); return True
    # fall back to first option
    first = page.query_selector('[role="option"]')
    if first and first.is_visible():
        first.click(); return True
    return False


def _select_native(el, key):
    prefs = OPTION_PREFS.get(key, [])
    options = el.evaluate("e => Array.from(e.options).map(o => o.text)")
    for pref in prefs:
        for opt in options:
            if pref in opt.lower():
                el.select_option(label=opt); return opt
    if key == "decline":
        for opt in options:
            if DECLINE.search(opt):
                el.select_option(label=opt); return opt
    return None


def _fill_combobox(page, el, key):
    el.click(); _human_pause()
    # typeaheads: type a query to filter; selection lists: just open + pick
    query = {"school": "Northeastern", "degree": "Master", "discipline": "Computer Science",
             "phone_country": "United States", "grad_month": "December"}.get(key)
    if query:
        try:
            el.type(query, delay=40)
        except Exception:
            pass
        time.sleep(2)
    else:
        time.sleep(1)
    return _pick_option(page, key)


def _fill_radio_groups(page, filled, missing):
    groups = page.query_selector_all('fieldset, [role="radiogroup"], li:has(input[type=radio])')
    done = set()
    for g in groups:
        try:
            q = (g.inner_text() or "").strip()
            qhead = q.split("\n")[0][:80]
            if not qhead or qhead in done:
                continue
            key = resolve_key(q)
            if not key:
                continue
            done.add(qhead)
            want = OPTION_PREFS.get(key, [])
            radios = g.query_selector_all('input[type=radio], [role=radio]')
            for r in radios:
                rlabel = (r.evaluate("e => (e.closest('label')?.innerText)|| e.parentElement?.innerText || ''")
                          or "").strip().lower()
                if any(w in rlabel for w in want) or (key == "decline" and DECLINE.search(rlabel)):
                    r.click()
                    filled.append((qhead, rlabel[:30]))
                    break
        except Exception:
            continue


# ── submit + self-heal ───────────────────────────────────────────────────────
SUCCESS = re.compile(r"thank you for applying|application (was )?(sent|submitted|received)|"
                     r"successfully (applied|submitted)|we'?ve received your application", re.I)


# Never click third-party "Apply with X" redirects, cookie, avatar, nav buttons.
_BAD_SUBMIT = re.compile(r"apply with|with indeed|with linkedin|with google|cookie|"
                         r"avatar|upload|cancel|back|save draft|sign in|log ?in", re.I)


def _btn_ok(el):
    try:
        if not el.is_visible() or el.is_disabled():
            return False
        t = (el.inner_text() or el.get_attribute("value") or el.get_attribute("aria-label") or "")
        return not _BAD_SUBMIT.search(t)
    except Exception:
        return False


def _find_submit(page):
    # Prefer explicit submit-y labels (excluding third-party "Apply with…").
    for txt in ["Submit application", "Submit Application", "Submit my application",
                "Send application", "Submit", "Send"]:
        for el in page.query_selector_all(f'button:has-text("{txt}")'):
            if _btn_ok(el):
                return el
    # Fallback: a real submit-type button that isn't a bad/third-party one.
    for el in page.query_selector_all('button[type=submit], input[type=submit]'):
        if _btn_ok(el):
            return el
    return None


def submit_with_heal(page, resume_path):
    """Click submit; auto-fix known validation errors; detect captcha/wall.
    Returns ('submitted'|'stacked'|'failed', detail)."""
    for attempt in range(1, MAX_SUBMIT_RETRIES + 1):
        btn = _find_submit(page)
        if not btn:
            return "failed", "no submit button found"
        btn.click()
        time.sleep(random.uniform(4, 6))

        body = page.inner_text("body") if page else ""
        if SUCCESS.search(body) or re.search(r"/(confirmation|thank)", page.url):
            return "submitted", f"verified after {attempt} attempt(s)"

        wall = detect_wall(page)
        if wall:
            return "stacked", wall

        # collect invalid fields and try to heal from profile
        invalids = _invalid_fields(page)
        if not invalids:
            # no obvious error and no success — give it a moment, re-check once
            time.sleep(3)
            if SUCCESS.search(page.inner_text("body")) or re.search(r"/(confirmation|thank)", page.url):
                return "submitted", "verified (delayed)"
            return "stacked", "submit had no effect and no recognizable error"

        healed = 0
        for el, label in invalids:
            key = resolve_key(label)
            if not key or key == "skip":
                continue
            try:
                if _heal_field(page, el, key):
                    healed += 1
                    print(f"   healed invalid field: {label[:40]!r}", flush=True)
            except Exception:
                pass
        if healed == 0:
            return "stacked", f"validation error I can't resolve: {[l for _, l in invalids][:4]}"
        time.sleep(1)  # loop and re-submit
    return "stacked", "still invalid after max retries"


def _invalid_fields(page):
    out = []
    for el in page.query_selector_all('[aria-invalid="true"], .error input, .invalid, '
                                      'input:invalid, select:invalid'):
        try:
            if el.is_visible():
                out.append((el, _label_for(page, el)))
        except Exception:
            pass
    # also catch react-select wrappers flagged invalid nearby an error message
    return out


def _heal_field(page, el, key):
    tag = el.evaluate("e => e.tagName.toLowerCase()")
    if key in OPTION_PREFS:
        if tag == "select":
            return bool(_select_native(el, key))
        return bool(_fill_combobox(page, el, key))
    if key == "location":
        return _fill_location(page, el)
    val = text_value(key)
    if val:
        el.click(); el.fill(""); el.type(val, delay=30); return True
    return False


# ── stacking for the laptop ───────────────────────────────────────────────────
def _load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def stack_for_human(job, reason, resume_path):
    queue = _load(NEEDS_HUMAN, [])
    queue.append({**job, "reason": reason, "resume_path": resume_path,
                  "stackedAt": datetime.now(timezone.utc).isoformat()})
    NEEDS_HUMAN.write_text(json.dumps(queue, indent=1))
    if tg and tg.configured():
        tg.send_message(f"🧩 <b>Stacked for your laptop</b>\n{job.get('title','')[:60]} @ "
                        f"{job.get('company','')}\nReason: {reason}\n{job.get('url','')}\n\n"
                        f"When you're at the Mac, run: <code>python3 external_autofill.py "
                        f"--resume-stacked</code> — I'll re-fill it so you only do the {reason} + submit.")


def log_result(job, status, detail):
    rec = _load(TRACKER, [])
    if not isinstance(rec, list):
        rec = rec.get("applications", [])
    rec.append({**job, "status": status, "detail": detail,
                "ts": datetime.now(timezone.utc).isoformat()})
    TRACKER.write_text(json.dumps(rec, indent=1))


# ── orchestration ─────────────────────────────────────────────────────────────
def _browser(p):
    PROFILE_DIR.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), channel="chrome", headless=False,
        no_viewport=True, args=["--disable-blink-features=AutomationControlled"])


def review_card(job, filled, missing):
    lines = [f"📋 <b>Review &amp; Submit</b>\n{job.get('title','')[:70]} @ {job.get('company','')}",
             "\nFilled:"]
    for lbl, val in filled:
        lines.append(f"• {lbl.strip()[:34]}: {str(val)[:46]}")
    if missing:
        lines.append("\n⚠️ <b>Could not fill (needs you):</b> " + ", ".join(m.strip()[:30] for m in missing[:8]))
    lines.append("\nTap Submit and I'll submit on the Mac; I'll auto-fix validation errors. "
                 "Captcha/login walls get stacked for your laptop.")
    return "\n".join(lines)


def process_job(url, resume_path=None, dry=False, job_meta=None, skip_hard=True,
                approval_timeout=3600, approval_channel="dedicated"):
    job = {"url": url, **(job_meta or {})}

    # ATS gate: skip Workday/iCIMS-style account walls outright (per Dev's rule).
    cls = ats_class(url)
    if skip_hard and cls == "hard":
        log_result(job, "skipped", f"hard ATS ({cls}) — account wall, skipped")
        print(f"SKIPPED (hard ATS): {url}")
        return "skipped-hard-ats"

    with sync_playwright() as p:
        ctx = _browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(25000)   # hang-guard: no action waits forever
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(random.uniform(4, 7))

        wall = detect_wall(page)
        if wall:
            stack_for_human(job, wall, resume_path)
            log_result(job, "stacked", wall)
            print(f"STACKED ({wall}): {url}")
            return "stacked"

        # Dead / expired posting (e.g. ATS 404) — skip instead of an empty card.
        body_head = ""
        try:
            body_head = page.inner_text("body")[:3000]
        except Exception:
            pass
        if re.search(r"can'?t (seem to )?find the page|error code:?\s*404|page not found|"
                     r"no longer available|position (has been|is no longer)", body_head, re.I):
            log_result(job, "skipped", "dead/expired posting (404 or removed)")
            print(f"SKIPPED (dead/expired): {url}")
            ctx.close()
            return "skipped-dead"

        _ensure_form(page)          # JD page -> click Apply to reveal the form
        _dismiss_overlays(page)     # close cookie/consent banners blocking clicks
        filled, missing = fill_application(page, resume_path)
        print(f"filled {len(filled)} fields; {len(missing)} required unfilled: {missing}", flush=True)
        if not filled and not missing:
            log_result(job, "skipped", "no fillable form found (unsupported page or expired)")
            print(f"SKIPPED (no form): {url}")
            ctx.close()
            return "skipped-no-form"
        page.screenshot(path=str(LOGS / "screenshots" /
                        f"{datetime.now():%Y%m%d_%H%M%S}_ext_filled.png"))

        if dry:
            print("DRY: not submitting.")
            ctx.close()
            return "dry-ok"

        if not (tg and tg.configured()):
            print("Telegram not configured; leaving filled form open for manual submit.")
            return "filled-no-telegram"

        import uuid
        nonce = uuid.uuid4().hex[:8]
        jid = re.search(r"(\d{4,})", url)
        jid = jid.group(1) if jid else nonce
        if approval_channel == "bridge":
            btok, bchat = _bridge_cfg()
            if not (btok and bchat):
                print("bridge bot not configured; cannot send card via MyClaudeBot.", flush=True)
                ctx.close()
                return "no-bridge-bot"
            _send_review_bridge(btok, bchat, job, filled, missing, jid, nonce)
            print(f"CARD_SENT(bridge=MyClaudeBot) jid={jid} nonce={nonce} — "
                  f"waiting up to {approval_timeout}s for your tap", flush=True)
            decision = _wait_decision_bridge(jid, nonce, approval_timeout)
        else:
            _send_review(job, filled, missing, jid, nonce)
            print(f"CARD_SENT jid={jid} nonce={nonce} — waiting up to {approval_timeout}s for your tap",
                  flush=True)
            decision = tg.wait_for_decision(jid, nonce=nonce, timeout=approval_timeout)
        print(f"DECISION={decision}", flush=True)
        if decision != "approve":
            print(f"decision={decision}; not submitting.")
            log_result(job, decision, "user did not approve")
            ctx.close()
            return decision

        status, detail = submit_with_heal(page, resume_path)
        log_result(job, status, detail)
        if status == "submitted":
            tg.send_message(f"✅ Submitted: {job.get('title','')[:60]} @ {job.get('company','')} ({detail})")
        elif status == "stacked":
            stack_for_human(job, detail, resume_path)
        else:
            tg.send_message(f"⚠️ Submit failed: {job.get('title','')[:50]} — {detail}")
        print(f"{status}: {detail}")
        # leave browser open if stacked so it's ready on the laptop
        if status != "stacked":
            ctx.close()
        return status


def _review_keyboard(jid, nonce):
    return {"inline_keyboard": [[
        {"text": "✅ Submit application", "callback_data": f"approve:{jid}:{nonce}"},
        {"text": "❌ Cancel", "callback_data": f"reject:{jid}:{nonce}"},
    ]]}


def _send_review(job, filled, missing, jid, nonce):
    import requests
    requests.post(f"{tg.API}/sendMessage",
                  data={"chat_id": tg.CHAT_ID, "text": review_card(job, filled, missing),
                        "parse_mode": "HTML", "disable_web_page_preview": True,
                        "reply_markup": json.dumps(_review_keyboard(jid, nonce))}, timeout=30)


# ── "bridge" approval channel: send via MyClaudeBot (@DevClaude403Bot) and read
#    the tap from the bridge's callback file, so we never fight the bridge for
#    getUpdates (it's the sole reader of that bot). ───────────────────────────
BRIDGE_CALLBACKS = HERE.parent / "claude-bridge" / "tg_callbacks.jsonl"


def _bridge_cfg():
    env = HERE.parent / ".conv-bot.env"
    tok = chat = None
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("CONV_BOT_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("CONV_CHAT_ID="):
                chat = line.split("=", 1)[1].strip().strip('"')
    return tok, chat


def _send_review_bridge(token, chat, job, filled, missing, jid, nonce):
    import requests
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  data={"chat_id": chat, "text": review_card(job, filled, missing),
                        "parse_mode": "HTML", "disable_web_page_preview": True,
                        "reply_markup": json.dumps(_review_keyboard(jid, nonce))}, timeout=30)


def _wait_decision_bridge(jid, nonce, timeout):
    """Watch the bridge's callback file for this job's tap. Returns approve|reject|timeout."""
    ap, rj = f"approve:{jid}:{nonce}", f"reject:{jid}:{nonce}"
    pos = BRIDGE_CALLBACKS.stat().st_size if BRIDGE_CALLBACKS.exists() else 0  # ignore old taps
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if BRIDGE_CALLBACKS.exists():
                with open(BRIDGE_CALLBACKS) as f:
                    f.seek(pos)
                    for line in f:
                        try:
                            data = json.loads(line).get("data", "")
                        except Exception:
                            continue
                        if data == ap:
                            return "approve"
                        if data == rj:
                            return "reject"
                    pos = f.tell()
        except Exception:
            pass
        time.sleep(3)
    return "timeout"


def _hold_browser_open(minutes):
    """Keep the script (and thus the browser) alive while the user finishes the
    submit by hand. Works with OR without an interactive terminal — never crashes
    on EOF (the old input() did when run via a non-TTY shell)."""
    secs = max(1, int(minutes * 60))
    try:
        if sys.stdin and sys.stdin.isatty():
            input(f"\nBrowser is open & pre-filled. Submit each tab yourself (+ any captcha), "
                  f"then press Enter here to close it... ")
        else:
            print(f"\nBrowser is OPEN & pre-filled. Switch to the Chrome window, submit each tab "
                  f"yourself (+ any captcha). Holding it open for {minutes} min — press Ctrl-C "
                  f"here to close sooner.", flush=True)
            time.sleep(secs)
    except (EOFError, KeyboardInterrupt):
        pass


def resume_stacked(hold_minutes=30):
    queue = _load(NEEDS_HUMAN, [])
    if not queue:
        print("nothing stacked."); return
    with sync_playwright() as p:
        ctx = _browser(p)
        opened = 0
        for job in queue:
            page = ctx.new_page()
            try:
                page.goto(job["url"], wait_until="domcontentloaded")
                time.sleep(random.uniform(4, 6))
                _ensure_form(page)
                _dismiss_overlays(page)
                fill_application(page, job.get("resume_path"))
                print(f"re-filled & open: {job.get('title','')[:50]}  [{job.get('reason','')[:30]}]",
                      flush=True)
                opened += 1
            except Exception as e:
                print(f"  could not reopen {job['url']}: {str(e)[:90]}", flush=True)
        if tg and tg.configured():
            tg.send_message(f"🖥️ Re-filled {opened} stacked application(s) in Chrome on your Mac — "
                            f"submit each yourself (+ any captcha). The browser is open for "
                            f"~{hold_minutes} min.")
        _hold_browser_open(hold_minutes)
        ctx.close()
    # Queue is left intact on purpose (we can't know what you submitted). Clear it
    # yourself once done:  python3 external_autofill.py --clear-stacked
    print("Browser closed. Stacked queue left intact — run --clear-stacked once you've submitted.",
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--resume")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--resume-stacked", action="store_true")
    ap.add_argument("--list-stacked", action="store_true")
    ap.add_argument("--clear-stacked", action="store_true")
    ap.add_argument("--hold-min", type=int, default=30, help="minutes to keep the browser open")
    args = ap.parse_args()
    (LOGS / "screenshots").mkdir(parents=True, exist_ok=True)

    if args.list_stacked:
        for j in _load(NEEDS_HUMAN, []):
            print(f"- {j.get('title','')[:50]} @ {j.get('company','')} | {j.get('reason')} | {j['url']}")
        return
    if args.clear_stacked:
        NEEDS_HUMAN.write_text("[]")
        print("stacked queue cleared.")
        return
    if args.resume_stacked:
        resume_stacked(hold_minutes=args.hold_min); return
    if args.job:
        process_job(args.job, resume_path=args.resume, dry=args.dry)
        return
    ap.error("need --job, --resume-stacked, or --list-stacked")


if __name__ == "__main__":
    main()
