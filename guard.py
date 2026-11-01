"""Safety checks run on every email before it is sent, so emails can go out without
you reading each one.

Your templates are fixed text; only the blanks change from person to person. So
these checks look at the blanks (name, company, personal line, links) and at the
templates themselves:

- "problem": something is broken (a template changed since you approved it, a
  leftover {{blank}} or <...>, a link that isn't your resume link). If too many
  emails in one batch have a problem, the whole batch stops: that usually means
  something upstream went wrong.
- "review": the email is probably fine but needs your eyes once (a personal line
  that isn't one of your approved sentences, a name that doesn't match the email
  address). It is held, not sent, until you approve it.

Approved sentences and other settings live in [guard] in config.toml.
"""
import hashlib
import json
import os
import re

import common

PROBLEM, REVIEW = "problem", "review"

DEFAULT_GUARD = {
    # Personal lines that may be sent without you reading them. {a} is "a"/"an",
    # {title} is the person's job title, {company} must be their company.
    "line_patterns": [
        "I noticed you have also studied in Emory and was genuinely impressed by your "
        "work as {a} {title} at {company}.",
        "I came across your profile and was genuinely impressed by your work as {a} "
        "{title} at {company}.",
    ],
    "max_title_words": 8,
    "max_problem_percent": 20,     # more than this share of a batch broken -> send nothing
    "banned_words": ["lorem", "todo", "tbd", "xxx", "placeholder", "example.com",
                     "insert", "your name"],
    "approved_templates": "data/approved_templates.json",
}
GENERIC_INBOXES = {"info", "contact", "hello", "careers", "jobs", "hr", "admin", "support",
                   "team", "recruiting", "recruiter", "talent", "office", "noreply", "no-reply"}
NAME_RE = re.compile(r"[A-Z][a-zA-Z'\-]*(?: [A-Z][a-zA-Z'\-]*)?")
TITLES = {"dr", "mr", "mrs", "ms", "prof", "sir", "madam"}


def settings(cfg):
    return {**DEFAULT_GUARD, **(cfg.get("guard") or {})}


def _hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def approved_path(cfg):
    return common.resolve(settings(cfg)["approved_templates"], cfg["workspace"])


def approve_templates(cfg):
    """Record the current text of every template as approved. Returns {name: hash}."""
    hashes = {os.path.basename(p): _hash(p) for p in common.template_paths(cfg)
              if os.path.exists(p)}
    path = approved_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    common._atomic_write(path, lambda f: json.dump(hashes, f, indent=2))
    return hashes


def load_approved(cfg):
    try:
        with open(approved_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _line_regex(pattern):
    """'... as {a} {title} at {company}.' -> a regex with named groups."""
    rx = re.escape(pattern)
    rx = rx.replace(re.escape("{a}"), r"(?P<a>an?)")
    rx = rx.replace(re.escape("{title}"), r"(?P<title>[^.]{2,80}?)")
    rx = rx.replace(re.escape("{company}"), r"(?P<company>.+?)")
    return re.compile(rf"^{rx}$")


def check_line(line, row, cfg):
    """Problems with the personal line, or [] if it is one of the approved sentences."""
    g = settings(cfg)
    line = (line or "").strip()
    for pattern in g["line_patterns"]:
        m = _line_regex(pattern).match(line)
        if not m:
            continue
        found = m.groupdict()
        out = []
        if "company" in found and found["company"].strip() != (row.get("company") or "").strip():
            out.append(f"line names {found['company']!r}, but their company is "
                       f"{row.get('company')!r}")
        title = found.get("title", "")
        if title and len(title.split()) > g["max_title_words"]:
            out.append(f"title in the line is too long ({len(title.split())} words)")
        if "a" in found and title:
            want = "an" if title[0].lower() in "aeiou" else "a"
            if found["a"] != want:
                out.append(f"grammar: '{found['a']} {title}' should be '{want} {title}'")
        return out
    return ["personal line isn't one of your approved sentences (needs your OK once)"]


def check_name(row):
    """Problems with the greeting name (first_name) and its match to the email address."""
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    if not NAME_RE.fullmatch(first) or len(first) < 2 or len(first) > 25:
        return [f"greeting name {first!r} doesn't look like a first name"]
    if first.isupper() and len(first) > 2:
        return [f"greeting name {first!r} is all capitals"]
    if first.lower().rstrip(".") in TITLES:
        return [f"greeting name {first!r} is a title, not a name"]
    local = common.normalize_email(row.get("contact_email")).partition("@")[0]
    if local in GENERIC_INBOXES:
        return [f"email {local}@... is a shared inbox, not a person"]
    letters = re.sub(r"[^a-z]", "", local)
    parts = [re.sub(r"[^a-z]", "", p) for p in first.lower().split() + [last.lower()]]
    if not any(len(p) >= 3 and p in letters for p in parts):
        return [f"name {first} {last} doesn't match the email address {local}@..."]
    return []


def check_values(row, used, cfg):
    """Problems in the filled-in blanks: links, leftover markers, banned words."""
    g = settings(cfg)
    resume = str((cfg.get("blanks") or {}).get("resume_link", "")).strip()
    out = []
    for name in used:
        value = (row.get(name) or "").strip()
        if name == "resume_link":
            if resume and value != resume:
                out.append("resume link isn't the one in config.toml")
            continue
        if name == "contact_email":
            continue
        low = value.lower()
        if re.search(r"https?://|www\.", low):
            out.append(f"{name} contains a link")
        if re.search(r"\{\{|\}\}|<[^>]*>", value):
            out.append(f"{name} contains a leftover marker")
        for word in g["banned_words"]:
            if re.search(rf"\b{re.escape(word)}\b", low):
                out.append(f"{name} contains {word!r}")
        if name == "company" and (len(value) > 60 or "|" in value):
            out.append(f"company {value!r} looks wrong")
    return out


def check(row, headers, body, template_path, cfg):
    """Every reason this email can't go out unread, as [(kind, reason)]; [] = safe."""
    out = []
    approved = load_approved(cfg)
    name = os.path.basename(template_path)
    if name not in approved:
        out.append((PROBLEM, f"template {name} was never approved "
                             f"(run `outreach approve-templates` after reading it)"))
    elif _hash(template_path) != approved[name]:
        out.append((PROBLEM, f"template {name} changed since you approved it"))
    used = common.placeholders("\n".join(headers.values()) + "\n" + body)
    rendered = common.render("\n".join(headers.values()) + "\n" + body, row)
    if re.search(r"\{\{|\}\}", rendered):
        out.append((PROBLEM, "a {{blank}} is left in the email"))
    out += [(PROBLEM, r) for r in check_values(row, used, cfg)]
    company_dom = common.main_domain(row.get("company_domain"))
    email_dom = common.main_domain(common.email_domain(row.get("contact_email")))
    if (company_dom and email_dom != company_dom
            and (row.get("email_status") or "").strip().lower() != common.PUBLIC):
        out.append((REVIEW, f"email is at {email_dom}, not {company_dom}"))
    if (row.get("approved") or "").strip().lower() == "yes":
        return out                     # you read this one: skip the per-person checks
    if "first_name" in used:
        out += [(REVIEW, r) for r in check_name(row)]
    if "personal_line" in used:
        out += [(REVIEW, r) for r in check_line(row.get("personal_line"), row, cfg)]
    return out


def too_many_problems(results, cfg):
    """True if the share of emails with a "problem" is above [guard].max_problem_percent."""
    if not results:
        return False
    broken = sum(any(kind == PROBLEM for kind, _ in r) for r in results)
    return broken * 100 > settings(cfg)["max_problem_percent"] * len(results)
