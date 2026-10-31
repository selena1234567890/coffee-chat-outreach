#!/usr/bin/env python3
"""Shared plumbing for the internship-outreach pipeline.

This module holds the plumbing shared by send.py, read_replies.py, and
outreach_cli.py: the Keychain fetch, IMAP login, SMTP session, header decoding,
template rendering, machine config, and (atomic, backed-up) CSV helpers.

Config: machine settings come from `config.toml` (see config.toml.example). The
Gmail app password comes from a credential backend (keyring / macOS Keychain /
env var), never from a file.
"""
import contextlib
import csv
import datetime
import functools
import getpass
import hashlib
import imaplib
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import tomllib
from email.header import decode_header, make_header

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_NAME = "config.toml"

KEYCHAIN_SERVICE = "mailmerge-gmail"
IMAP_HOST = "imap.gmail.com"
ALL_MAIL = '"[Gmail]/All Mail"'  # catches replies even after they've been archived

# Conventional defaults so a missing config.toml (e.g. a fresh clone) never hard-
# crashes an import. Only the email address has no safe default.
DEFAULT_EMAIL = {
    "smtp_host": "smtp.gmail.com", "smtp_port": 587,
    "imap_host": IMAP_HOST, "keychain_service": KEYCHAIN_SERVICE,
}
DEFAULT_FILES = {
    "template": "mailmerge_template.txt",
    "database": "mailmerge_database.csv",
    "sent_log": "sent_log.csv",
    "queue": "outreach_queue.csv",
    "resume": "resume.pdf",
    "targets": "data/target_companies.csv",
    "do_not_contact": "data/do_not_contact.csv",
    "send_state": "data/send_state.json",
}
DEFAULT_PACING = {"batch_size": 14, "followup_after_days": 6}
# Most people to contact per company, by company size (employee count).
DEFAULT_COMPANY_LIMITS = {"small": 3, "mid": 5, "large": 7, "unknown": 4,
                          "small_below": 1000, "large_from": 10000, "min_employees": 1000}
# Which email a person gets when their `type` cell is blank.
DEFAULT_ROUTING = {"coffee_chat_min_years": 3,
                   "coffee_chat_titles": ["lead", "manager", "director", "principal",
                                          "staff", "head"]}
# Most emails per type per day.
DEFAULT_DAILY = {"coffee_chat": 6, "referral": 8}
# Volume ramp: [emails per day, days at this step] (0 days = stay there). The ramp
# moves up only after a full step with no warning sign; a warning pauses sending
# for pause_days and steps down one level.
DEFAULT_RAMP = {"enabled": True, "steps": [[13, 3], [18, 7], [25, 7], [35, 0]],
                "pause_days": 2, "bounce_limit_percent": 3, "bounce_window": 50,
                "spam_check_every_days": 3}
# One follow-up to people who haven't replied after this many business days.
DEFAULT_FOLLOWUP = {"enabled": True, "after_business_days": 3}
# Random wait between emails at send time, so they don't all go out at once.
DEFAULT_SENDING = {"min_gap_seconds": 60, "max_gap_seconds": 180}
# Share of each day's emails per company tier, in percent.
DEFAULT_TIER_SHARE = {1: 60, 2: 30, 3: 10}
# Who is staged first. Empty by default; set yours in config.toml.
DEFAULT_PRIORITY = {"schools": [], "tags": [], "surname_lists": {},
                    "hand_picked_unlisted_tier": 1}
# `source` values: who found the person (compare reply rates by source in `status`).
HAND_PICKED = "hand_picked"   # you saved them yourself (Apollo extension)
AGENT_FOUND = "agent"         # the contact finder's Apollo search found them
WEB_FOUND = "web"             # the contact finder found them + a public email on the web
PUBLIC = "public"             # email_status for an email the person published themselves

# Columns a lead (one person to contact) can have, in the order a new queue file uses.
# Any other column is kept too and can be used as a {{blank}} in templates.
LEAD_FIELDS = ["type", "contact_email", "first_name", "last_name", "company", "company_size",
               "title", "start_date", "school", "previous_company", "tier",
               "open_intern_roles", "priority_tags", "linkedin_url", "email_status",
               "email_source", "email_seen", "personal_line", "personal_source",
               "connection", "target_role", "job_link",
               "subject", "source", "apollo_id", "found_at"]
# What the contact finder searches for (see [search] in config.toml).
DEFAULT_SEARCH = {
    "titles": ["Data Scientist", "Data Analyst", "Machine Learning Engineer", "AI Engineer",
               "Applied Scientist", "Research Scientist", "Data Engineer",
               "Analytics Engineer", "Business Intelligence Analyst", "Product Analyst",
               "Decision Scientist", "MLOps Engineer"],
    "manager_titles": True,       # also search managers of these roles
    "locations": ["United States"],
    "max_credits_per_run": 25,
    "accepted_email_statuses": ["verified", "public"],
    "max_email_age_years": 2,
    # Hand-picks: contacts saved from these Apollo sources on/after this date.
    "handpicked_sources": ["chrome_extension_linkedin"],
    "handpicked_since": "2026-10-31",
}

# Canonical sent-log schema. Existing logs may have only the first columns; that's
# tolerated on read and extended by `outreach migrate`.
SENT_LOG_FIELDS = ["contact_email", "contact_name", "company", "role",
                   "subject", "note", "type", "template_version", "subject_sent",
                   "message_id", "sent_at", "last_reply", "bounced_at", "outcome",
                   "outcome_at"]
# What can happen after an email; the first four count as a good result.
POSITIVE_OUTCOMES = ("coffee_chat_booked", "referral_given", "interview", "offer")
OUTCOMES = POSITIVE_OUTCOMES + ("not_interested", "no_reply")

# Embedded so `outreach setup` can scaffold a config without a shipped example file
# (works for any install mode, editable or wheel).
CONFIG_TEMPLATE = '''\
[email]
address = "you@gmail.com"
smtp_host = "smtp.gmail.com"
smtp_port = 587
imap_host = "imap.gmail.com"
# Store your Gmail APP password (not your login password) in a backend, then point
# this name at it. See SECURITY.md.
keychain_service = "mailmerge-gmail"

[files]
template = "mailmerge_template.txt"
database = "mailmerge_database.csv"
sent_log = "sent_log.csv"
queue = "outreach_queue.csv"
resume = "resume.pdf"
targets = "data/target_companies.csv"        # company, domain, tier (you edit)
do_not_contact = "data/do_not_contact.csv"   # people never to cold email (private)

# One template per email type. Each row's `type` column picks its template.
# Leave this section out to use the single [files].template for every row.
[templates]
coffee_chat = "templates/coffee_chat.txt"
referral = "templates/referral.txt"
followup = "templates/followup.txt"
# 2-3 versions of a type, so not every email has the same wording, e.g.:
# coffee_chat = ["templates/coffee_chat_1.txt", "templates/coffee_chat_2.txt"]

[pacing]
batch_size = 14            # most emails per day in total; do not blast
followup_after_days = 6

# Most people to contact at one company, by company size (number of employees,
# from the `company_size` column). Extra people wait in the queue.
[company_limits]
small = 3                  # fewer than small_below employees
mid = 5
large = 7                  # large_from employees or more (Meta, Amazon...)
unknown = 4                # company_size is blank
small_below = 1000
min_employees = 1000       # skip companies smaller than this entirely (0 = no floor)
large_from = 10000

# Which email a person gets when their `type` cell is blank. A type you fill in
# by hand always wins.
[routing]
coffee_chat_min_years = 3  # at the company this long (from `start_date`) -> coffee_chat
coffee_chat_titles = ["lead", "manager", "director", "principal", "staff", "head"]
# everyone else -> referral

# Most emails per type per day. Tip: use half for your first week.
[daily]
coffee_chat = 6
referral = 8

# What the contact finder (Agent 1) searches for in Apollo. Apollo also matches
# similar titles, so combinations like "Data Scientist, AI" are included.
[search]
titles = ["Data Scientist", "Data Analyst", "Machine Learning Engineer", "AI Engineer",
          "Applied Scientist", "Research Scientist", "Data Engineer", "Analytics Engineer",
          "Business Intelligence Analyst", "Product Analyst", "Decision Scientist",
          "MLOps Engineer"]
manager_titles = true        # also search managers of these roles
locations = ["United States"]
max_credits_per_run = 25     # Apollo credits (1 per email found); the agent asks first
accepted_email_statuses = ["verified", "public"]
# verified = Apollo checked it; public = the person published it themselves (paper,
# personal site, GitHub) - needs email_source, and email_seen within max_email_age_years
max_email_age_years = 2
# Your hand-picks: people you save with the Apollo LinkedIn extension on/after this date.
handpicked_sources = ["chrome_extension_linkedin"]
handpicked_since = "2026-10-31"

# Daily volume ramp. The day's emails (new + follow-ups) follow these steps; within a
# day they're split between types like [daily] (6:8 -> about 3:4). Moves up a step only
# after a full step with no warning sign. Warning signs (bounces over the limit, a
# Gmail sending-limit error, a spam check that landed in spam) pause sending for
# pause_days, then go down one step. See `outreach health`.
[ramp]
enabled = true
steps = [[13, 3], [18, 7], [25, 7], [35, 0]]   # [emails per day, days]; 0 days = stay
pause_days = 2
bounce_limit_percent = 3
bounce_window = 50           # bounce rate over your last 50 emails
spam_check_every_days = 3    # remind you to run a spam check this often

# One follow-up (in the same email thread, using templates/followup.txt) to people
# who haven't replied after this many business days. Never more than one per person.
[followup]
enabled = true
after_business_days = 3

# Wait a random time between emails so they don't all go out at once.
[sending]
min_gap_seconds = 60
max_gap_seconds = 180

# Share of each day's emails per company tier (percent). Every tier with a share
# above 0 gets at least 1 a day; slots a tier can't fill go to the best people left.
# Compare reply rates by tier in `outreach status` and adjust.
[tier_share]
1 = 60                     # big tech
2 = 30                     # finance / banking / consulting
3 = 10                     # other big companies

# Who gets staged first: a matching `school`, or a tag in `priority_tags`.
[priority]
schools = ["Emory"]
tags = ["chinese", "hand_picked"]   # hand_picked = people you saved yourself in Apollo
hand_picked_unlisted_tier = 1       # tier for hand-picked people at unlisted companies

# A tag is added automatically when `last_name` is in that file (one name per line).
[priority.surname_lists]
chinese = "data/chinese_surnames.txt"

[outreach]
# The one thing you specify: what kind of internships/roles to target.
area = "<e.g. Machine Learning, scientific computing; remote, unpaid>"

# Values that are the same in every email; use them in any template as {{name}}.
# A column with the same name in your spreadsheet wins over these.
[blanks]
resume_link = "<link to your resume, e.g. a Google Drive 'anyone with the link' URL>"
'''


def find_config(explicit=None):
    """Locate config.toml: --config > $OUTREACH_CONFIG > ./config.toml (in CWD).

    The directory containing the chosen path is the WORKSPACE; all relative paths in
    [files] resolve against it, so a pip-installed CLI reads/writes the user's working
    directory, never the install location. The returned path may not exist yet (e.g.
    before `outreach setup`); load_config tolerates that. There is deliberately NO
    fallback to the module/install dir, so a stray source-tree config is never used.
    """
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("OUTREACH_CONFIG")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(DEFAULT_CONFIG_NAME)  # ./config.toml in the current dir


def resolve(path, base):
    """Resolve a (possibly relative) path against `base` (the workspace dir)."""
    return path if os.path.isabs(path) else os.path.join(base, path)


def load_config(path=None):
    """Return machine config with ABSOLUTE file paths and the workspace dir.

    Shape: {email, files (absolute paths), pacing, outreach, workspace, config_path}.
    Never raises on a missing file — defaults fill in everything except the email
    address (set [email].address in config.toml before sending).
    """
    path = path or find_config()
    workspace = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    email_cfg = {**DEFAULT_EMAIL, **data.get("email", {})}
    files_cfg = {k: resolve(v, workspace)
                 for k, v in {**DEFAULT_FILES, **data.get("files", {})}.items()}
    templates_cfg = {k.strip().lower(): ([resolve(p, workspace) for p in v]
                                         if isinstance(v, list) else resolve(v, workspace))
                     for k, v in data.get("templates", {}).items()}
    priority_cfg = {**DEFAULT_PRIORITY, **data.get("priority", {})}
    priority_cfg["surname_lists"] = {k.strip().lower(): resolve(v, workspace)
                                     for k, v in priority_cfg["surname_lists"].items()}
    return {
        "email": email_cfg,
        "files": files_cfg,
        "templates": templates_cfg,
        "pacing": {**DEFAULT_PACING, **data.get("pacing", {})},
        "company_limits": {**DEFAULT_COMPANY_LIMITS, **data.get("company_limits", {})},
        "routing": {**DEFAULT_ROUTING, **data.get("routing", {})},
        "daily": {k.strip().lower(): v
                  for k, v in {**DEFAULT_DAILY, **data.get("daily", {})}.items()},
        "priority": priority_cfg,
        "search": {**DEFAULT_SEARCH, **data.get("search", {})},
        "tier_share": {int(k): v for k, v in
                       {**DEFAULT_TIER_SHARE, **data.get("tier_share", {})}.items()},
        "outreach": data.get("outreach", {}),
        "blanks": data.get("blanks", {}),
        "ramp": {**DEFAULT_RAMP, **data.get("ramp", {})},
        "sending": {**DEFAULT_SENDING, **data.get("sending", {})},
        "followup": {**DEFAULT_FOLLOWUP, **data.get("followup", {})},
        "workspace": workspace,
        "config_path": path,
    }


def validate_for_send(cfg):
    """Fail early (ValueError) on config that would make a send incoherent."""
    em = cfg["email"]
    if not em.get("address"):
        raise ValueError("no sender: set [email].address in config.toml")
    try:
        int(em["smtp_port"])
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"[email].smtp_port must be an integer, got {em.get('smtp_port')!r}")
    for template in template_paths(cfg):
        if not os.path.exists(template):
            raise ValueError(f"template not found: {template}")
        headers, _ = load_template(template)
        missing = [h for h in ("TO", "SUBJECT") if h not in headers]
        if missing:
            raise ValueError(f"template {template} missing header(s): {', '.join(missing)}")


def template_paths(cfg):
    """Every template this config can use: the [templates] map, else [files].template."""
    paths = []
    for v in (cfg.get("templates") or {}).values():
        paths += v if isinstance(v, list) else [v]
    return paths or [cfg["files"]["template"]]


def template_for(row, cfg):
    """Pick the template path for a row from its `type` column.

    With a [templates] map, the row's type must be one of its keys (ValueError
    otherwise), so a typo never silently falls back to the wrong email. Without
    one, every row uses [files].template. A type may list several versions; each
    person always gets the same one (picked from their email address), so preview
    and send match, and the choice is spread evenly across people.
    """
    templates = cfg.get("templates") or {}
    if not templates:
        return cfg["files"]["template"]
    kind = (row.get("type") or "").strip().lower()
    if kind not in templates:
        raise ValueError(f"type {kind or '(blank)'!r} is not one of: "
                         f"{', '.join(sorted(templates))}")
    choice = templates[kind]
    if not isinstance(choice, list):
        return choice
    digest = hashlib.sha256(normalize_email(row.get("contact_email")).encode()).hexdigest()
    return choice[int(digest, 16) % len(choice)]


def company_size(row):
    """Employee count from the `company_size` cell ("12,000", "1001-5000", "500+").

    Takes the first number found; None if the cell has no number.
    """
    m = re.search(r"\d[\d,]*", row.get("company_size") or "")
    return int(m.group().replace(",", "")) if m else None


def company_limit(row, cfg):
    """Most people to contact at this row's company, from [company_limits] by size."""
    lim = {**DEFAULT_COMPANY_LIMITS, **(cfg.get("company_limits") or {})}
    size = company_size(row)
    if size is None:
        return lim["unknown"]
    if size < lim["small_below"]:
        return lim["small"]
    if size >= lim["large_from"]:
        return lim["large"]
    return lim["mid"]


UNKNOWN_TIER = 4  # companies not on the target list sort after tier 3


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def email_domain(addr):
    """'Jane@Mail.Meta.com' -> 'mail.meta.com' ('' if no @)."""
    return normalize_email(addr).rpartition("@")[2]


def _csv_rows(path):
    if not path or not os.path.exists(path):
        return []
    return read_rows(path)[1]


@functools.lru_cache(maxsize=None)
def load_targets(path):
    """Map company name and domain (lowercased) -> tier, from target_companies.csv."""
    out = {}
    for r in _csv_rows(path):
        tier = (r.get("tier") or "").strip()
        if not tier.isdigit():
            continue
        for key in (_norm(r.get("company")), _norm(r.get("domain"))):
            if key:
                out[key] = int(tier)
    return out


def company_tier(row, cfg):
    """1 = big tech, 2 = finance/banking/consulting, 3 = other big companies.

    Uses the row's own `tier` if filled in, else looks the company up in the target
    list by name, then by email domain (sub.domain.com also matches domain.com).
    Companies not on the list get UNKNOWN_TIER, so they sort last, except people
    you hand-picked: they get [priority].hand_picked_unlisted_tier (you chose them).
    """
    own = (row.get("tier") or "").strip()
    if own.isdigit():
        return int(own)
    targets = load_targets((cfg.get("files") or {}).get("targets"))
    if _norm(row.get("company")) in targets:
        return targets[_norm(row.get("company"))]
    parts = email_domain(row.get("contact_email")).split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in targets:
            return targets[".".join(parts[i:])]
    if is_hand_picked(row):
        pr = {**DEFAULT_PRIORITY, **(cfg.get("priority") or {})}
        return int(pr["hand_picked_unlisted_tier"])
    return UNKNOWN_TIER


def is_hand_picked(row):
    return (row.get("source") or "").strip().lower() == HAND_PICKED


def tier_caps(cfg, day_total):
    """Most emails per tier today: [tier_share] percent of day_total, at least 1 per tier
    with a share above 0, so every tier gets some each day."""
    shares = cfg.get("tier_share") or DEFAULT_TIER_SHARE
    caps = {t: max(1, round(pct / 100 * day_total)) if pct > 0 else 0
            for t, pct in shares.items()}
    # Rounding up to 1 can overshoot a small day; trim the biggest share first.
    while sum(caps.values()) > day_total and max(caps.values()) > 1:
        caps[max(caps, key=caps.get)] -= 1
    return caps


def too_small(row, cfg):
    """True if the company's known size is under [company_limits].min_employees.

    Unknown size is never "too small" (it uses the `unknown` per-company limit).
    """
    lim = {**DEFAULT_COMPANY_LIMITS, **(cfg.get("company_limits") or {})}
    size = company_size(row)
    return size is not None and size < lim["min_employees"]


def has_open_roles(row):
    """True if the `open_intern_roles` column says yes."""
    return (row.get("open_intern_roles") or "").strip().lower() in {"yes", "y", "true", "1"}


@functools.lru_cache(maxsize=None)
def _do_not_contact_list(path):
    return tuple(r for r in _csv_rows(path)
                 if any((r.get(k) or "").strip() for k in ("contact_email", "first_name")))


def on_do_not_contact(row, cfg):
    """True if this person is on data/do_not_contact.csv.

    A list entry matches by email, or by first name + (last name if given) +
    (company if given). Company names match loosely ("JPMorgan" ~ "JPMorgan Chase").
    """
    email = normalize_email(row.get("contact_email"))
    first, last = _norm(row.get("first_name")), _norm(row.get("last_name"))
    company = _norm(row.get("company"))
    for d in _do_not_contact_list((cfg.get("files") or {}).get("do_not_contact")):
        d_email = normalize_email(d.get("contact_email"))
        if d_email and d_email == email:
            return True
        d_first, d_last, d_company = (_norm(d.get(k)) for k in ("first_name", "last_name",
                                                                  "company"))
        same_company = not d_company or (company and (d_company in company
                                                      or company in d_company))
        if d_first and d_first == first and d_last in ("", last) and same_company:
            return True
    return False


def years_ago(date_text, today=None):
    """Years since a date written YYYY-MM-DD, YYYY-MM or YYYY; None if not a date."""
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", (date_text or "").strip())
    if not m:
        return None
    try:
        then = datetime.date(int(m[1]), int(m[2] or 1), int(m[3] or 1))
    except ValueError:
        return None
    return ((today or datetime.date.today()) - then).days / 365.25


def years_at_company(row, today=None):
    """Years since `start_date` (YYYY-MM-DD, YYYY-MM or YYYY), or None if unknown."""
    return years_ago(row.get("start_date"), today)


def pick_type(row, cfg, today=None):
    """Email type for a row: its own `type` if filled in, else by [routing].

    coffee_chat for people at the company coffee_chat_min_years or longer, or whose
    `title` has one of coffee_chat_titles as a whole word; referral for everyone else
    (including people whose start date is unknown).
    """
    own = (row.get("type") or "").strip().lower()
    if own:
        return own
    rules = {**DEFAULT_ROUTING, **(cfg.get("routing") or {})}
    years = years_at_company(row, today)
    if years is not None and years >= rules["coffee_chat_min_years"]:
        return "coffee_chat"
    title = (row.get("title") or "").lower()
    if any(re.search(rf"\b{re.escape(w.lower())}\b", title)
           for w in rules["coffee_chat_titles"]):
        return "coffee_chat"
    return "referral"


def _letters(name):
    return re.sub(r"[^a-z]", "", (name or "").lower())


@functools.lru_cache(maxsize=None)
def load_surnames(path):
    """Last names in a list file (one per line, # comments), lowercased, letters only."""
    try:
        with open(path, encoding="utf-8") as f:
            return frozenset(_letters(line.split("#")[0]) for line in f) - {""}
    except FileNotFoundError:
        return frozenset()


def split_tags(text):
    """'chinese, alumni' -> {'chinese', 'alumni'} (comma or semicolon separated)."""
    return {t.strip().lower() for t in re.split(r"[,;]", text or "") if t.strip()}


def auto_tags(row, cfg):
    """The row's `priority_tags`, plus any tag whose surname list has its `last_name`,
    plus `hand_picked` for people you saved yourself."""
    tags = split_tags(row.get("priority_tags"))
    if is_hand_picked(row):
        tags.add(HAND_PICKED)
    last = _letters(row.get("last_name"))
    lists = (cfg.get("priority") or {}).get("surname_lists", {})
    tags |= {tag for tag, path in lists.items() if last and last in load_surnames(path)}
    return tags


def priority_groups(row, cfg):
    """Which [priority] schools and tags this row matches, e.g. ['Emory', 'chinese']."""
    pr = cfg.get("priority") or {}
    school = (row.get("school") or "").lower()
    tags = split_tags(row.get("priority_tags"))
    return ([s for s in pr.get("schools", []) if s.lower() in school]
            + [t for t in pr.get("tags", []) if t.lower() in tags])


def priority_score(row, cfg):
    """How many priority groups a row matches; higher is staged first."""
    return len(priority_groups(row, cfg))


def load_state(cfg):
    """The sending state (ramp step, pause, warnings, spam checks); {} if none yet."""
    path = (cfg.get("files") or {}).get("send_state")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(cfg, state):
    path = cfg["files"]["send_state"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _atomic_write(path, lambda f: json.dump(state, f, indent=2))


def first_send_date(sent_rows):
    """Local date of the earliest sent_at in the sent log, or None."""
    dates = []
    for r in sent_rows:
        try:
            dt = datetime.datetime.strptime((r.get("sent_at") or "").strip(),
                                            "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        dates.append(dt.replace(tzinfo=datetime.timezone.utc).astimezone().date())
    return min(dates) if dates else None


def ramp_status(cfg, first_send=None, today=None):
    """Today's email limit from the ramp: {per_day, step, step_started, paused_until}.

    The ramp starts on your first real send. per_day is 0 while paused. Moving up a
    step is saved to the state file.
    """
    ramp = {**DEFAULT_RAMP, **(cfg.get("ramp") or {})}
    steps = ramp["steps"]
    today = today or datetime.date.today()
    state = load_state(cfg)
    if not state.get("step_started"):
        if first_send is None:  # nothing sent yet: the first step, nothing to save
            return {"per_day": steps[0][0], "step": 0, "step_started": None,
                    "paused_until": None}
        state.update(step=0, step_started=first_send.isoformat())
    step = min(int(state.get("step", 0)), len(steps) - 1)
    started = datetime.date.fromisoformat(state["step_started"])
    paused = state.get("paused_until") or None
    if paused and today < datetime.date.fromisoformat(paused):
        return {"per_day": 0, "step": step, "step_started": started, "paused_until": paused}
    while step < len(steps) - 1 and steps[step][1] > 0 \
            and (today - started).days >= steps[step][1]:
        started += datetime.timedelta(days=steps[step][1])
        step += 1
    if (step, started.isoformat()) != (state.get("step"), state.get("step_started")) \
            or "step" not in state:
        state.update(step=step, step_started=started.isoformat())
        save_state(cfg, state)
    return {"per_day": steps[step][0], "step": step, "step_started": started,
            "paused_until": None}


def record_warning(cfg, reason, today=None):
    """A warning sign: pause sending for [ramp].pause_days, then go down one step."""
    ramp = {**DEFAULT_RAMP, **(cfg.get("ramp") or {})}
    today = today or datetime.date.today()
    resume = today + datetime.timedelta(days=ramp["pause_days"])
    state = load_state(cfg)
    state.update(step=max(0, int(state.get("step", 0)) - 1),
                 step_started=resume.isoformat(), paused_until=resume.isoformat())
    state.setdefault("warnings", []).append({"date": today.isoformat(), "reason": reason})
    save_state(cfg, state)
    return resume


def split_daily(daily, total):
    """Split a day's total between email types in the same ratio as [daily].

    Each type gets its whole-number share; leftover emails go to the types with the
    biggest remainders, so the parts always add up to the total.
    """
    weights = {k: v for k, v in daily.items() if v > 0}
    if not weights or total <= 0:
        return {k: 0 for k in daily}
    exact = {k: total * v / sum(weights.values()) for k, v in weights.items()}
    out = {k: int(x) for k, x in exact.items()}
    by_remainder = sorted(exact, key=lambda k: exact[k] - out[k], reverse=True)
    for k in by_remainder[:total - sum(out.values())]:
        out[k] += 1
    return out


LIMIT_ERROR_WORDS = ("sending limit", "daily user sending", "quota", "5.4.5", "rate limit",
                     "too many")


def is_sending_limit_error(exc):
    """True if an SMTP error looks like Gmail telling us we've hit a sending limit."""
    return any(w in str(exc).lower() for w in LIMIT_ERROR_WORDS)


def backup(path, backup_dir=None):
    """Copy `path` into a `.backups/` dir (next to it) with a UTC timestamp.

    Returns the backup path, or None if the source does not exist yet.
    """
    if not os.path.exists(path):
        return None
    backup_dir = backup_dir or os.path.join(
        os.path.dirname(os.path.abspath(path)), ".backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(backup_dir, f"{os.path.basename(path)}.{stamp}.bak")
    shutil.copy2(path, dest)
    return dest


def _atomic_write(path, write_fn):
    """Write via a temp file in the same dir, fsync, then os.replace().

    fsync of both the file and its directory makes the replacement durable across
    an OS/power crash, not just a clean process exit.
    """
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Directory fsync makes the rename durable, but O_DIRECTORY/dir-fsync isn't
        # supported everywhere (e.g. Windows); the file fsync + replace above are the
        # core guarantee, so treat the dir fsync as best-effort.
        try:
            dirfd = os.open(d, getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except (OSError, AttributeError):
            pass
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def normalize_email(addr):
    """Canonical contact identity used for dedup everywhere (stage, send, replies)."""
    return (addr or "").strip().lower()


def sent_emails(log_path):
    """Set of normalized recipient addresses already in the sent log (idempotency)."""
    if not os.path.exists(log_path):
        return set()
    _, rows = read_rows(log_path)
    return {normalize_email(r.get("contact_email")) for r in rows
            if r.get("contact_email")}


ENV_PASSWORD = "OUTREACH_GMAIL_APP_PASSWORD"
ENV_BACKEND = "OUTREACH_SECRET_BACKEND"  # force one of: keyring|security|env


def _from_keyring(service):
    """Look up the password via the optional, cross-platform `keyring` library."""
    try:
        import keyring  # lazy: absence must not affect imports/tests
    except ImportError:
        return None
    try:
        return keyring.get_password(service, getpass.getuser())
    except Exception:
        return None


def _from_security(service):
    """Look up the password via the macOS `security` CLI (Keychain)."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", service, "-a", getpass.getuser(), "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _from_env(_service):
    return os.environ.get(ENV_PASSWORD) or None


# Recommended local backends first; env var is an explicit CI/escape hatch only
# (env vars can leak via shell history, process listings, and CI logs).
_BACKENDS = {"keyring": _from_keyring, "security": _from_security, "env": _from_env}
_AUTO_ORDER = ["keyring", "security", "env"]


def _setup_hint():
    return (f"Store your Gmail APP password once. Recommended:\n"
            f"  - keyring (any OS): pip install keyring; "
            f"python -m keyring set {KEYCHAIN_SERVICE} \"$USER\"\n"
            f"  - macOS Keychain:   security add-generic-password -s "
            f"{KEYCHAIN_SERVICE} -a \"$USER\" -w\n"
            f"  - CI/escape hatch:  export {ENV_PASSWORD}=...")


def _backend_order():
    """Resolve the backend search order, exiting on an invalid forced override."""
    forced = os.environ.get(ENV_BACKEND)
    if forced and forced not in _BACKENDS:
        sys.exit(f"ERROR: invalid {ENV_BACKEND}={forced!r}; choose one of "
                 f"{', '.join(_BACKENDS)}.")
    return [forced] if forced else _AUTO_ORDER


def secret_status(service=KEYCHAIN_SERVICE):
    """Report (backend_name, found_bool, setup_hint) without raising. For `setup`."""
    for name in _backend_order():
        if _BACKENDS[name](service):
            return name, True, ""
    return "none", False, _setup_hint()


def get_password(service=KEYCHAIN_SERVICE):
    """Fetch the Gmail app password from the first backend that has it.

    Order: an explicit OUTREACH_SECRET_BACKEND override, else keyring (if installed)
    > macOS `security` > the OUTREACH_GMAIL_APP_PASSWORD env var. Cross-platform; the
    macOS-only path is just one option. Exits with a setup hint if none has it.
    """
    for name in _backend_order():
        pw = _BACKENDS[name](service)
        if pw:
            return pw
    sys.exit(f"\nERROR: no Gmail app password found (service '{service}').\n{_setup_hint()}\n")


@contextlib.contextmanager
def smtp_session(host, port, username, password):
    """Logged-in STARTTLS SMTP session as a context manager."""
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        yield smtp


def imap_login(username, password, readonly=True, host=IMAP_HOST, mailbox=ALL_MAIL):
    """Log into Gmail over IMAP and select a mailbox (All Mail, INBOX fallback)."""
    imap = imaplib.IMAP4_SSL(host)
    imap.login(username, password)
    typ, _ = imap.select(mailbox, readonly=readonly)
    if typ != "OK":  # All Mail label may be localized/renamed
        imap.select("INBOX", readonly=readonly)
    return imap


def decode_header_str(s):
    """Best-effort decode of a MIME-encoded header to a plain string."""
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def body_text(msg):
    """Extract the plain-text body from a parsed email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return str(msg.get_payload())


def render(text, row):
    """Replace every {{column}} with the row value."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: row.get(m.group(1), m.group(0)), text)


def placeholders(text):
    """Names of every {{column}} used in `text`, in first-seen order."""
    return list(dict.fromkeys(re.findall(r"\{\{(\w+)\}\}", text)))


def with_blanks(row, cfg):
    """The row plus the shared [blanks] values (a filled-in row column wins).

    A [blanks] value still written like "<...>" counts as not filled in, so the
    blank check stops the send instead of mailing the placeholder text.
    """
    shared = {k: str(v) for k, v in (cfg.get("blanks") or {}).items()
              if not re.fullmatch(r"\s*<.*>\s*", str(v))}
    filled = {k: v for k, v in row.items() if (v or "").strip()}
    return {**row, **shared, **filled}


def missing_fields(row, headers, body):
    """Placeholders in the template (headers + body) that this row leaves blank."""
    used = placeholders("\n".join(headers.values()) + "\n" + body)
    return [name for name in used if not (row.get(name) or "").strip()]


def print_check_line(row, indent="      "):
    """Print what the agent found (personal line, public email source), marked so it's
    easy to review before sending."""
    line = (row.get("personal_line") or "").strip()
    if line:
        print(f'{indent}>> CHECK: "{line}"')
        source = (row.get("personal_source") or "").strip()
        print(f"{indent}   " + (f"source: {source}" if source else "(no source: plain line)"))
    if (row.get("email_status") or "").strip().lower() == PUBLIC:
        seen = (row.get("email_seen") or "").strip() or "date unknown"
        print(f"{indent}>> CHECK email: published at {row.get('email_source')} ({seen})")


def load_template(template_path):
    """Split the template into its header dict and body, preserving raw text for {{subst}}."""
    raw = open(template_path, encoding="utf-8").read()
    head, _, body = raw.partition("\n\n")
    headers = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().upper()] = v.strip()
    return headers, body


def read_rows(db_path):
    """Return (fieldnames, rows) from a mailmerge CSV.

    Uses utf-8-sig so a stray BOM on the header (e.g. contact_email) is stripped
    rather than silently corrupting the column name and defeating dedup.
    """
    with open(db_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def archive(log_path, fieldnames, sent_rows):
    """Record sent rows in the log via an ATOMIC full rewrite (not a bare append).

    - Durable: read existing rows, add the new ones, write atomically (temp+fsync+
      os.replace), so a crash can't leave a half-written line that later breaks
      dedup.
    - Header-stable: reuse the existing log's column order so appended rows never
      land under the wrong columns.
    - Fail-closed: refuse to write to a non-empty log whose header lacks
      contact_email, since that would silently defeat the idempotency guard.
    """
    has_log = os.path.exists(log_path) and os.path.getsize(log_path) > 0
    existing_fields, existing_rows = read_rows(log_path) if has_log else (None, [])
    if existing_fields is not None and "contact_email" not in existing_fields:
        raise ValueError(
            f"{log_path} header lacks 'contact_email' (got {existing_fields}); "
            f"refusing to write, as it would defeat dedup.")
    out_fields = existing_fields or fieldnames

    def _w(f):
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(existing_rows)
        w.writerows(sent_rows)
    _atomic_write(log_path, _w)


def utcnow_iso():
    """Current UTC time as an ISO-8601 'Z' timestamp (for sent_at stamps)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def migrate_sent_log(log_path):
    """Extend an existing sent log's header to the full SENT_LOG_FIELDS (blank-fill).

    Backward-compatible: old rows keep their values and gain empty new columns
    (e.g. sent_at). No-op if the log is absent or already complete. Returns True if
    it rewrote the file. Run this as a preflight BEFORE sending, never after.
    """
    if not (os.path.exists(log_path) and os.path.getsize(log_path) > 0):
        return False
    fields, rows = read_rows(log_path)
    if "contact_email" not in (fields or []):
        raise ValueError(f"{log_path} header lacks 'contact_email'; cannot migrate.")
    missing = [f for f in SENT_LOG_FIELDS if f not in fields]
    if not missing:
        return False
    new_fields = fields + missing

    def _w(f):
        w = csv.DictWriter(f, fieldnames=new_fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**{k: "" for k in new_fields}, **r})
    _atomic_write(log_path, _w)
    return True


def write_db(db_path, fieldnames, rows, ignore_extra=False):
    """Atomically rewrite the database with exactly `rows` (header always written).

    Uses a temp file + os.replace so a crash mid-write can never leave a truncated
    or half-written CSV that would lose staged leads. With ignore_extra, keys that
    are not columns of the file are left out instead of raising.
    """
    def _w(f):
        w = csv.DictWriter(f, fieldnames=fieldnames,
                           extrasaction="ignore" if ignore_extra else "raise")
        w.writeheader()
        w.writerows(rows)
    _atomic_write(db_path, _w)
