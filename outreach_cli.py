#!/usr/bin/env python3
"""One entrypoint for the internship-outreach pipeline.

Composable, mostly non-interactive commands the agent (or you) can drive end to
end. CRUCIALLY this CLI NEVER sends mail. The only send path is
`python3 send.py --send`, gated by your explicit go-ahead against a fresh preview.
Autonomy ends at "staged + previewed".

Commands:
  outreach_cli.py status          funnel snapshot (queue / staged / sent)
  outreach_cli.py setup           scaffold config.toml + check resume + Keychain
  outreach_cli.py find            print the internship-scout brief (area + exclude list)
  outreach_cli.py stage [-n N]    move N rows queue -> database (backed-up, deduped)
  outreach_cli.py preview         render the staged batch (delegates to send.py dry-run)
  outreach_cli.py replies         run the IMAP reply fallback (delegates to read_replies.py)
  outreach_cli.py followup        list silent contacts (cross-check via Gmail connector)
"""
import argparse
import collections
import csv
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import threading

import common
import guard

PY = sys.executable


def _safe_rows(path):
    if not os.path.exists(path):
        return None, []
    return common.read_rows(path)


def _company(r):
    return (r.get("company") or "").strip().lower()


def _sent_companies(sent_log):
    _, rows = _safe_rows(sent_log)
    return {(r.get("company") or "").strip().lower() for r in rows if r.get("company")}


def _paths(cfg):
    f = cfg["files"]  # already absolute (resolved against the workspace)
    return f["queue"], f["database"], f["sent_log"]


def cmd_status(args, cfg):
    queue, db, sent_log = _paths(cfg)
    _, q_rows = _safe_rows(queue)
    _, db_rows = _safe_rows(db)
    sent = common.sent_emails(sent_log) if os.path.exists(sent_log) else set()
    print("=== Outreach funnel ===")
    print(f"  Sent (people):      {len(sent)} at {len(_sent_companies(sent_log))} companies")
    print(f"  Staged (in DB):     {len(db_rows)}")
    kinds = collections.Counter((r.get("type") or "").strip() or "default" for r in db_rows)
    for kind, n in sorted(kinds.items()):
        print(f"    {kind}: {n}")
    print(f"  Queue (backlog):    {len(q_rows)}")
    _print_reply_rates(sent_log, cfg)
    if not q_rows:
        print("\n  Backlog is dry -> run `find` and the contact finder.")
    elif db_rows:
        print("\n  Batch staged -> `preview`, then `outreach-send --send` once you approve.")


def _print_reply_rates(sent_log, cfg):
    """Sent vs replied, by email type and by priority group, to tune the rules.

    Replies come from the sent log's last_reply column; refresh it first with
    `outreach backfill-dates --apply`.
    """
    _, rows = _safe_rows(sent_log)
    if not rows:
        return
    sent, replied = collections.Counter(), collections.Counter()
    for r in rows:
        tier = common.company_tier(r, cfg)
        groups = [f"type: {(r.get('type') or '').strip() or 'default'}",
                  "tier: " + ("not listed" if tier == common.UNKNOWN_TIER else str(tier)),
                  f"source: {(r.get('source') or '').strip() or 'unknown'}"]
        groups += [f"priority: {g}" for g in common.priority_groups(r, cfg)] or ["priority: none"]
        for g in groups:
            sent[g] += 1
            replied[g] += bool((r.get("last_reply") or "").strip())
    print("\n=== Replies so far (refresh: `outreach backfill-dates --apply`) ===")
    for g in sorted(sent):
        print(f"  {g:<22} sent {sent[g]:>3}   replied {replied[g]:>3}   "
              f"({replied[g] / sent[g]:.0%})")


def cmd_setup(args, cfg):
    cfg_path = cfg["config_path"]
    if not os.path.exists(cfg_path):
        # Scaffold the config at the requested/discovered path (never in the package).
        os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(common.CONFIG_TEMPLATE)
        print(f"Created {cfg_path}.")
        print("Edit it: address, resume path, [outreach].area; then re-run setup.")
        cfg = common.load_config(cfg_path)  # reload so checks below see the new file
    else:
        print(f"config.toml: OK ({cfg_path})")

    attaches = False
    for kind, paths in (cfg.get("templates") or {"default": cfg["files"]["template"]}).items():
        for path in paths if isinstance(paths, list) else [paths]:
            if not os.path.exists(path):
                print(f"Template [{kind}]: MISSING -> copy the .example, then edit {path}")
                continue
            attaches |= "ATTACHMENT" in common.load_template(path)[0]
            print(f"Template [{kind}]: OK {os.path.basename(path)}")
    link = common.with_blanks({}, cfg).get("resume_link")
    print(f"Resume link: {'OK ' + link if link else 'UNSET -> set [blanks].resume_link'}")
    if attaches:  # only needed when a template still attaches the file
        resume = cfg["files"]["resume"]
        found = os.path.exists(resume)
        print(f"Resume file: {'OK' if found else 'MISSING ->'} {resume}")

    backend, ok, hint = common.secret_status(cfg["email"]["keychain_service"])
    print(f"Password: {'OK via ' + backend if ok else 'MISSING -> ' + hint}")

    addr = cfg["email"].get("address")
    print(f"Sender:  {addr or 'UNSET -> set [email].address in config.toml'}")
    area = cfg["outreach"].get("area")
    print(f"Area:    {area or 'UNSET -> set [outreach].area in config.toml'}")


def cmd_find(args, cfg):
    queue, _, sent_log = _paths(cfg)
    area = cfg["outreach"].get("area") or "(unset - fill [outreach].area in config.toml)"
    sent = sorted(_sent_companies(sent_log))
    print("=== internship-scout brief ===")
    print("AREA:", area)
    print(f"\nEXCLUDE these {len(sent)} already-contacted companies:")
    print("  " + (", ".join(sent) if sent else "(none yet)"))
    print("\nHand this brief to the internship-scout subagent. It returns email-VERIFIED")
    print(f"leads; append them to {os.path.basename(queue)}, then run `stage`.")


def _sent_today(r, today=None):
    """True if this sent-log row's sent_at (UTC) falls on today's local date."""
    try:
        dt = datetime.datetime.strptime((r.get("sent_at") or "").strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    local = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
    return local.date() == (today or datetime.date.today())


def _is_followup(r):
    return (r.get("type") or "").strip().lower() == "followup"


def business_days_between(start, end):
    """Weekdays after `start` up to and including `end` (Mon-Fri)."""
    days, d = 0, start
    while d < end:
        d += datetime.timedelta(days=1)
        days += d.weekday() < 5
    return days


def due_followups(sent_rows, db_rows, cfg, today=None):
    """Follow-up rows for people who haven't replied [followup].after_business_days
    business days after their first email. One follow-up per person, ever; never to
    anyone who replied, bounced, or is on the do-not-contact list."""
    fu = {**common.DEFAULT_FOLLOWUP, **(cfg.get("followup") or {})}
    if not fu["enabled"]:
        return []
    today = today or datetime.date.today()
    done = {common.normalize_email(r.get("contact_email"))
            for r in sent_rows + db_rows if _is_followup(r)}
    # Per person, not per row: someone emailed twice by mistake who answered (or
    # bounced) on either copy gets no follow-up.
    done |= {common.normalize_email(r.get("contact_email")) for r in sent_rows
             if (r.get("last_reply") or "").strip() or (r.get("bounced_at") or "").strip()}
    out = []
    for r in sent_rows:
        email = common.normalize_email(r.get("contact_email"))
        if (_is_followup(r) or email in done or not r.get("message_id")
                or (r.get("last_reply") or "").strip() or (r.get("bounced_at") or "").strip()
                or common.skip_reason(r, cfg)):
            continue
        sent = common.first_send_date([r])
        if sent is None or business_days_between(sent, today) < fu["after_business_days"]:
            continue
        out.append({**r, "type": "followup", "in_reply_to": r["message_id"],
                    "original_subject": r.get("subject_sent") or r.get("subject", ""),
                    "first_sent_at": r.get("sent_at", ""), "sent_at": "", "message_id": "",
                    "subject_sent": "", "template_version": ""})
        done.add(email)
    return out


def cmd_stage(args, cfg):
    """Move today's batch from the queue into the database, following the rules:

    1. remove anyone already sent or staged (by email), or on the do-not-contact list
    2. skip companies under [company_limits].min_employees
    3. fill a blank `type` ([routing]), `tier` (target list) and surname tags
    4. order: data/ML/AI job -> company hiring interns (data/open_roles.csv) -> tier
       (1 first) -> priority groups -> queue order. Hiring companies fill the day first
       (each tier its [tier_share], then any tier), then other data/ML/AI people (tier
       shares again), then leftover slots go to the best rest
    5. at most company_limit people per company ([company_limits], by size)
    6. at most [daily] emails per type today (counting staged + sent today); if the
       ramp's day isn't full after that, the other type's unused slots are used
    7. at most N in total (-n, default [pacing].batch_size)
    People skipped by 2 and 5-7 stay in the queue (for another day, or a changed setting).
    """
    queue, db, sent_log = _paths(cfg)
    q_fields, q_rows = _safe_rows(queue)
    db_fields, db_rows = _safe_rows(db)
    _, sent_rows = _safe_rows(sent_log)
    todays = db_rows + [r for r in sent_rows if _sent_today(r)]
    firsts = [r for r in sent_rows + db_rows if not _is_followup(r)]  # first emails only

    daily = cfg.get("daily") or common.DEFAULT_DAILY
    ramp_on = bool((cfg.get("ramp") or {}).get("enabled"))
    if ramp_on:
        ramp = common.ramp_status(cfg, first_send=common.first_send_date(sent_rows))
        if ramp["per_day"] == 0:
            print(f"Paused after a warning sign until {ramp['paused_until']}. "
                  "See `outreach health`.")
            return
        day_total = ramp["per_day"]
        daily = common.split_daily(daily, day_total)
        room = day_total - len(todays)
        n = room if args.n is None else min(args.n, room)
        print(f"Ramp step {ramp['step'] + 1}: {day_total} emails today "
              f"({len(todays)} already staged or sent).")
    else:
        day_total = sum(daily.values())
        n = args.n if args.n is not None else cfg["pacing"]["batch_size"]
    if n <= 0:
        print("Nothing to do: today's limit is reached.")
        return

    if getattr(args, "no_followups", False):
        followups = []
        print("Follow-ups skipped today: the Gmail reply check didn't finish, so we "
              "can't be sure who replied. New people fill the batch instead.")
    else:
        followups = due_followups(sent_rows, db_rows, cfg)[:n]
    n -= len(followups)
    if not q_rows and not followups:
        print("Queue is empty (header only). Run `find` + the contact finder to refill.")
        return
    fields = list(db_fields or q_fields or common.LEAD_FIELDS)
    extras = ["type", "tier", "priority_tags"] + list(q_fields)  # new queue columns too
    if followups:
        extras += [k for r in followups for k in r]
    for extra in extras:
        if extra not in fields:
            fields.append(extra)

    blk_emails = common.sent_emails(sent_log)
    blk_emails |= {common.normalize_email(r.get("contact_email")) for r in db_rows}
    per_company = collections.Counter(_company(r) for r in firsts)
    used_today = collections.Counter(common.pick_type(r, cfg) for r in todays)
    used_tier = collections.Counter(common.company_tier(r, cfg) for r in todays
                                    if not _is_followup(r))
    # Tier shares are of today's NEW emails (follow-ups don't take a tier's share).
    new_today = sum(used_tier.values())
    followups_today = len(todays) - new_today + len(followups)
    tier_cap = common.tier_caps(cfg, min(n + new_today, day_total - followups_today))

    def prepared(r):
        out = dict(r, type=common.pick_type(r, cfg), tier=str(common.company_tier(r, cfg)))
        out["priority_tags"] = ", ".join(sorted(common.auto_tags(r, cfg)))
        return out

    def rank(pair):
        r = pair[1]
        return (not common.relevant_role(r, cfg), not common.has_open_roles(r, cfg),
                int(r["tier"]), -common.priority_score(r, cfg))

    # Best first; sorted() is stable, so ties keep queue order.
    ranked = sorted(((r, prepared(r)) for r in q_rows), key=rank)
    to_move, gone, why = [], set(), {}
    dropped, blocked = 0, []
    # Passes, best people first: (1) data/ML/AI people at companies hiring interns, each
    # tier up to its share of the day ([tier_share]); (2) the same people, any tier;
    # (3) other data/ML/AI people, tier shares; (4) leftover slots to the best rest.
    # In a share pass, when today has room for every tier's share, the smallest tiers go
    # first so a big tier can't use up a type's daily limit before they get theirs.
    if n >= sum(max(0, c - used_tier[t]) for t, c in tier_cap.items()):
        by_tier = sorted(ranked, key=lambda pair: -int(pair[1]["tier"]))
    else:
        by_tier = ranked
    hiring = [p for p in ranked if common.relevant_role(p[1], cfg)
              and common.has_open_roles(p[1], cfg)]
    relevant = [p for p in by_tier if common.relevant_role(p[1], cfg)]
    hiring_ids = {id(p[0]) for p in hiring}
    hiring_by_tier = [p for p in by_tier if id(p[0]) in hiring_ids]
    # (5) if the day still isn't full, one type's unused slots go to the other type,
    # so the day's total is met whenever there are enough people.
    for by_share, order, any_type in ((True, hiring_by_tier, False), (False, hiring, False),
                                      (True, relevant, False), (False, ranked, False),
                                      (False, ranked, ramp_on)):
        for orig, r in order:
            if id(orig) in gone:
                continue
            email = common.normalize_email(r.get("contact_email"))
            kind, tier = r["type"], int(r["tier"])
            if email in blk_emails:
                dropped += 1                 # already sent or staged -> remove from queue
                gone.add(id(orig))
            elif common.skip_reason(r, cfg):
                blocked.append(r)            # do-not-contact or covered company -> remove
                gone.add(id(orig))
            elif common.too_small(r, cfg):
                why[id(orig)] = "small"      # under min_employees -> stays in queue
            elif _company(r) and per_company[_company(r)] >= common.company_limit(r, cfg):
                why[id(orig)] = "company"    # company at its limit -> stays in queue
            elif kind in daily and used_today[kind] >= daily[kind] and not any_type:
                why[id(orig)] = "type"       # today's limit for this type -> stays
            elif len(to_move) >= n:
                why[id(orig)] = "full"       # today's batch is full -> stays
            elif by_share and used_tier[tier] >= tier_cap.get(tier, 0):
                why[id(orig)] = "tier"       # tier's share used; may still get a pass-2 slot
            else:
                to_move.append(r)
                gone.add(id(orig))
                blk_emails.add(email)
                per_company[_company(r)] += 1
                used_today[kind] += 1
                used_tier[tier] += 1
    for orig, r in ranked:                         # not a data/ML/AI job: leftover only
        if why.get(id(orig)) == "full" and not common.relevant_role(r, cfg):
            why[id(orig)] = "role"
    to_move.sort(key=lambda r: rank((None, r)))  # back to priority order
    waiting = collections.Counter(w for k, w in why.items() if k not in gone)
    leftover = [r for r in q_rows if id(r) not in gone]  # original order, unchanged

    if not to_move and not dropped and not blocked and not followups:
        print("Nothing new to stage today.")
        _print_waiting(waiting, used_today, daily, used_tier, tier_cap)
        return

    common.backup(queue)
    common.backup(db)
    # DB grows first (additive), queue is rewritten without moved-or-handled rows.
    # If interrupted between the two writes, the email dedup above plus the
    # idempotency check in send.py mean nothing double-sends; backups aid recovery.
    common.write_db(db, fields, db_rows + followups + to_move)
    common.write_db(queue, q_fields, leftover)

    print(f"Staged {len(followups)} follow-up(s) + {len(to_move)} new -> "
          f"{os.path.basename(db)}; dropped {dropped} already-handled; "
          f"{len(leftover)} left in queue.")
    for r in followups:
        print(f"  ~ [followup] {r.get('company')} <{r.get('contact_email')}> "
              f"(first email {r.get('first_sent_at', '')[:10]})")
    if followups:
        print("  (Follow-ups use the replies in the sent log: run "
              "`outreach backfill-dates --apply` first so nobody who replied gets one.)")
    for r in to_move:
        groups = common.priority_groups(r, cfg)
        star = f"  * {', '.join(groups)}" if groups else ""
        tier = "T?" if int(r["tier"]) == common.UNKNOWN_TIER else f"T{r['tier']}"
        roles = " hiring" if common.has_open_roles(r, cfg) else ""
        print(f"  + {tier} [{r['type']}] {r.get('company')} <{r.get('contact_email')}>"
              f"{roles}{star}")
    for r in blocked:
        print(f"  x removed ({common.skip_reason(r, cfg)}): {r.get('first_name')} "
              f"{r.get('last_name')} at {r.get('company')}")
    _print_waiting(waiting, used_today, daily, used_tier, tier_cap)
    print("\nNext: `outreach preview`, then `outreach-send --test`, then `outreach-send --send`.")


WAITING_REASONS = {
    "small": "company is under min_employees ([company_limits])",
    "company": "their company is at its limit ([company_limits])",
    "type": "today's limit for their type is used up ([daily])",
    "full": "today's batch is full (-n / [pacing].batch_size)",
    "role": "not a data/ML/AI job, so only used when nobody better is left",
}


def _print_waiting(waiting, used_today, daily, used_tier, tier_cap):
    today = ", ".join(f"{k} {used_today[k]}/{v}" for k, v in sorted(daily.items()))
    print(f"\nToday so far: {today}")
    tiers = ", ".join(f"T{t} {used_tier[t]}/{c}" for t, c in sorted(tier_cap.items()))
    extra = used_tier[common.UNKNOWN_TIER]
    print(f"By tier (share): {tiers}" + (f", not listed {extra}" if extra else ""))
    for reason, text in WAITING_REASONS.items():
        if waiting[reason]:
            print(f"  {waiting[reason]} waiting: {text}.")


def cmd_preview(args, cfg):
    sys.stdout.flush()  # print everything so far before the preview's own output
    # `-m` works whether installed as a wheel or run from a source checkout.
    rc = subprocess.run([PY, "-m", "send", "--config", cfg["config_path"]]).returncode
    if rc:
        raise SystemExit(rc)


def mark_bounces(sent_log, bounced, today=None):
    """Stamp bounced_at on sent-log rows whose address bounced; returns how many are new."""
    fields, rows = _safe_rows(sent_log)
    if not rows:
        return 0
    stamp = (today or datetime.date.today()).isoformat()
    new = 0
    for r in rows:
        if common.normalize_email(r.get("contact_email")) in bounced \
                and not (r.get("bounced_at") or "").strip():
            r["bounced_at"] = stamp
            new += 1
    if new:
        fields = list(fields) + [f for f in ("bounced_at",) if f not in fields]
        common.backup(sent_log)
        common.write_db(sent_log, fields, rows)
    return new


def bounce_rate(sent_rows, window):
    """Share of your last `window` first emails that bounced (0.0 if none sent)."""
    firsts = [r for r in sent_rows if not _is_followup(r)][-window:]
    if not firsts:
        return 0.0
    return sum(bool((r.get("bounced_at") or "").strip()) for r in firsts) / len(firsts)


def apply_bounces(cfg, sent_log, bounced):
    """Stamp new bounces on the sent log; too many recent bounces is a warning sign
    (pause + one ramp step down). Returns how many bounces are new."""
    ramp_cfg = {**common.DEFAULT_RAMP, **(cfg.get("ramp") or {})}
    new = mark_bounces(sent_log, bounced)
    window = [r for r in _safe_rows(sent_log)[1]
              if not _is_followup(r)][-ramp_cfg["bounce_window"]:]
    # Bounces at a company you've since blocked (data/covered_companies.csv) don't
    # count: the cause is known and nobody there is emailed again.
    count = sum(bool((r.get("bounced_at") or "").strip())
                and not common.covered_company(r, cfg) for r in window)
    rate = count / len(window) if window else 0.0
    print(f"Bounces: {new} new; {count} = {rate:.0%} of your last "
          f"{ramp_cfg['bounce_window']} emails (pause at over "
          f"{ramp_cfg['bounce_limit_percent']}% and {ramp_cfg['bounce_min_count']}+ bounces).")
    if new and count >= ramp_cfg["bounce_min_count"] \
            and rate * 100 > ramp_cfg["bounce_limit_percent"]:
        resume = common.record_warning(cfg, f"bounce rate {rate:.0%}")
        print(f"  Too many bounces: pausing until {resume}, one ramp step down.")
    return new


def cmd_health(args, cfg):
    """Check sending health: bounces (from Gmail), spam checks, ramp step, pauses.
    A warning sign pauses sending for [ramp].pause_days and steps down the ramp."""
    _, _, sent_log = _paths(cfg)

    if args.spam_check:
        state = common.load_state(cfg)
        state["last_spam_check"] = {"date": datetime.date.today().isoformat(),
                                    "result": args.spam_check}
        common.save_state(cfg, state)
        if args.spam_check == "spam":
            resume = common.record_warning(cfg, "spam check landed in spam")
            print(f"Spam check landed in SPAM: pausing until {resume}, one ramp step down.")
        else:
            print("Spam check landed in the inbox. Good.")

    if not args.no_imap and os.path.exists(sent_log):
        import read_replies  # local import: only the bounce check needs IMAP
        em = cfg["email"]
        imap = common.imap_login(em["address"], common.get_password(em["keychain_service"]),
                                 readonly=True, host=em["imap_host"])
        try:
            contacts = [(common.normalize_email(r.get("contact_email")), "")
                        for r in _safe_rows(sent_log)[1]]
            bounced = read_replies.find_bounces(imap, contacts)
        finally:
            imap.logout()
        apply_bounces(cfg, sent_log, bounced)

    _print_health(cfg, _safe_rows(sent_log)[1])


def _print_health(cfg, sent_rows):
    ramp_cfg = {**common.DEFAULT_RAMP, **(cfg.get("ramp") or {})}
    status = common.ramp_status(cfg, first_send=common.first_send_date(sent_rows))
    state = common.load_state(cfg)
    steps = ramp_cfg["steps"]
    print("\n=== Sending health ===")
    if status["per_day"] == 0:
        print(f"  PAUSED until {status['paused_until']}")
    else:
        print(f"  Ramp: step {status['step'] + 1} of {len(steps)}, "
              f"{status['per_day']} emails/day"
              + ("" if status["step_started"] else " (starts with your first real send)"))
    for w in state.get("warnings", [])[-3:]:
        print(f"  Warning {w['date']}: {w['reason']}")
    last = state.get("last_spam_check")
    every = ramp_cfg["spam_check_every_days"]
    if not last:
        print("  Spam check: never done. Run `outreach-send --spam-check you@outlook.com`.")
    else:
        age = (datetime.date.today() - datetime.date.fromisoformat(last["date"])).days
        due = "  <- due, run one" if age >= every else ""
        print(f"  Spam check: {last['result']} ({age} day(s) ago){due}")


MIN_GROUP = 20  # below this many people, a group's rates are too noisy to act on


def _iso_dt(text):
    try:
        return datetime.datetime.strptime((text or "").strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def person_results(sent_rows, cfg):
    """One summary per person emailed: their first email's details and what happened.

    Replies come from last_reply (refresh with `outreach backfill-dates --apply`); a
    reply that came after the follow-up was sent counts as a follow-up reply.
    """
    people = {}
    for r in sent_rows:
        email = common.normalize_email(r.get("contact_email"))
        if not email:
            continue
        p = people.setdefault(email, {"first": None, "followup": None, "rows": []})
        p["rows"].append(r)
        p["followup" if _is_followup(r) else "first"] = r
    out = []
    for p in people.values():
        first, fu = p["first"], p["followup"]
        if first is None:
            continue
        replied_at = max((r.get("last_reply") or "").strip() for r in p["rows"])
        bounced = any((r.get("bounced_at") or "").strip() for r in p["rows"])
        sent_at, reply_dt = _iso_dt(first.get("sent_at")), _iso_dt(replied_at)
        fu_sent = (fu or {}).get("sent_at", "")
        if (first.get("personal_source") or "").strip():
            line = "real fact"
        elif (first.get("personal_line") or "").strip():
            line = "plain line"
        else:
            line = "none"
        tier = common.company_tier(first, cfg)
        out.append({
            "row": first,
            "replied_at": replied_at,
            "bounced": bounced,
            "replied": bool(replied_at) and not bounced,
            "replied_after_followup": bool(replied_at and fu_sent and replied_at > fu_sent),
            "followup_sent": bool(fu_sent),
            "days_to_reply": ((reply_dt - sent_at).total_seconds() / 86400
                              if reply_dt and sent_at else None),
            "outcome": (first.get("outcome") or "").strip(),
            "groups": {
                "type": [(first.get("type") or "").strip() or "default"],
                "template version": [(first.get("template_version") or "").strip()
                                     or "unknown"],
                "tier": ["not listed" if tier == common.UNKNOWN_TIER else f"tier {tier}"],
                "priority": common.priority_groups(first, cfg) or ["none"],
                "source": [(first.get("source") or "").strip() or "unknown"],
                "personal line": [line],
                "open intern roles": ["yes" if common.has_open_roles(first, cfg)
                                      else "no/unknown"],
            },
        })
    return out


def _rate(part, whole):
    return f"{part / whole:4.0%}" if whole else "  - "


def _print_group_table(results, dimension):
    stats = collections.defaultdict(collections.Counter)
    for p in results:
        for g in p["groups"][dimension]:
            c = stats[g]
            c["people"] += 1
            c["bounced"] += p["bounced"]
            c["replied"] += p["replied"]
            c["good"] += p["outcome"] in common.POSITIVE_OUTCOMES
    print(f"\nBy {dimension}:")
    for g, c in sorted(stats.items(), key=lambda kv: -kv[1]["people"]):
        delivered = c["people"] - c["bounced"]
        small = "  (too few to judge)" if c["people"] < MIN_GROUP else ""
        print(f"  {g:<22} people {c['people']:>4}   bounced {_rate(c['bounced'], c['people'])}"
              f"   replied {_rate(c['replied'], delivered)}   good results {c['good']:>3}"
              f"{small}")


def cmd_report(args, cfg):
    """The numbers to tune the tool by: reply, bounce and good-result rates, split by
    type, template version, tier, priority, source, personal line and open roles;
    first email vs follow-up; time to reply; Apollo credits per reply; health.
    Checks Gmail for new replies first, so the numbers are never stale."""
    _, _, sent_log = _paths(cfg)
    if not args.no_imap and _safe_rows(sent_log)[1]:
        print("Checking Gmail for new replies...")
        cmd_backfill_dates(argparse.Namespace(apply=True), cfg)
        print()
    _, sent_rows = _safe_rows(sent_log)
    results = person_results(sent_rows, cfg)
    if not results:
        print("Nothing sent yet, so nothing to report.")
        _print_health(cfg, sent_rows)
        return

    n = len(results)
    bounced = sum(p["bounced"] for p in results)
    replied = [p for p in results if p["replied"]]
    after_fu = sum(p["replied_after_followup"] for p in replied)
    outcomes = collections.Counter(p["outcome"] for p in results if p["outcome"])
    good = sum(outcomes[o] for o in common.POSITIVE_OUTCOMES)
    referrals = outcomes["referral_given"]
    print(f"=== Outreach report: {n} people emailed ===")
    print(f"  RESPONSE RATE:          {len(replied)} replied / {n} emailed = "
          f"{_rate(len(replied), n).strip()}")
    print(f"  REFERRAL SUCCESS RATE:  {referrals} referrals / {n} emailed = "
          f"{_rate(referrals, n).strip()}")
    asks = [p for p in results if p["groups"]["type"][0] == "referral"]
    if asks:
        got = sum(p["outcome"] == "referral_given" for p in asks)
        print(f"    (of the {len(asks)} referral asks: {got} = {_rate(got, len(asks)).strip()}; "
              f"referrals can also come from coffee chats)")
    print(f"  Bounced: {bounced} ({_rate(bounced, n).strip()})   "
          f"Replied: {len(replied)} ({_rate(len(replied), n - bounced).strip()} of delivered)"
          f"   Good results: {good}")
    print(f"  Replies to the first email: {len(replied) - after_fu}   after the follow-up: "
          f"{after_fu}   (follow-ups sent: {sum(p['followup_sent'] for p in results)})")
    days = sorted(p["days_to_reply"] for p in replied if p["days_to_reply"] is not None)
    if days:
        fu_days = (cfg.get("followup") or common.DEFAULT_FOLLOWUP)["after_business_days"]
        within = sum(d <= fu_days + 2 for d in days)  # ~ the follow-up wait, in calendar days
        print(f"  Time to reply: median {statistics.median(days):.1f} days "
              f"(fastest {days[0]:.1f}, slowest {days[-1]:.1f}); "
              f"{_rate(within, len(days)).strip()} replied within about "
              f"{fu_days} business days (before a follow-up)")
    if outcomes:
        print("  Outcomes: " + ", ".join(f"{o} {outcomes[o]}" for o in common.OUTCOMES
                                         if outcomes[o]))
    for dimension in results[0]["groups"]:
        _print_group_table(results, dimension)
    print(f"\n(Groups under {MIN_GROUP} people are too small to judge yet; replies come "
          "from `outreach backfill-dates --apply`.)")

    apollo = [p for p in results
              if p["groups"]["source"][0] in (common.HAND_PICKED, common.AGENT_FOUND)]
    if apollo:
        apollo_replies = sum(p["replied"] for p in apollo)
        per = f"{len(apollo) / apollo_replies:.1f}" if apollo_replies else "no replies yet"
        print(f"\nApollo credits: about {len(apollo)} used (1 per email revealed) for "
              f"{apollo_replies} replies -> credits per reply: {per}")
    _print_health(cfg, sent_rows)


REPLY_WAIT_SECONDS = 90  # the daily routine never waits longer than this for Gmail


def check_replies_in_background(cfg, wait=REPLY_WAIT_SECONDS):
    """Read replies from Gmail on a side thread and wait at most `wait` seconds.
    Returns True if the check finished. If Gmail is slow, the routine goes on
    without it (the thread dies when the process ends; `outreach feedback` checks
    replies again after sending)."""
    result = {}

    def run():
        try:
            cmd_backfill_dates(argparse.Namespace(apply=True), cfg)
            result["ok"] = True
        except Exception as e:  # a Gmail error must never stop the day's sending
            print(f"Gmail reply check failed: {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(wait)
    if t.is_alive():
        print(f"Gmail didn't answer within {wait}s, so today's batch goes on without "
              f"the reply check (follow-ups wait for a day it works).")
    return bool(result.get("ok")) and not t.is_alive()


def cmd_daily(args, cfg):
    """The morning routine, in the right order. Never sends anything:
    1. replies + send dates from Gmail (so nobody who replied gets a follow-up)
    2. health: bounces, pause, ramp step, spam-check reminder
    3. stage today's batch (follow-ups first, then new people, by the rules)
    4. preview every email, with the >> CHECK lines to review
    Then: `outreach-send --test`, read it, and `outreach-send --send` once you approve.
    """
    _, _, sent_log = _paths(cfg)
    has_sent = bool(_safe_rows(sent_log)[1])
    replies_ok = not has_sent
    print("=== 1. Replies from Gmail ===")
    if args.no_imap:
        print("Skipped (--no-imap): follow-ups use the replies already in the sent log.")
    elif has_sent:
        replies_ok = check_replies_in_background(cfg)
    else:
        print("Nothing sent yet.")
    print("\n=== 2. Health ===")
    cmd_health(argparse.Namespace(spam_check=None,
                                  no_imap=args.no_imap or not has_sent or not replies_ok), cfg)
    _, sent_rows = _safe_rows(sent_log)
    if common.ramp_status(cfg, first_send=common.first_send_date(sent_rows))["per_day"] == 0:
        print("\nPaused, so nothing is staged today.")
        return
    print("\n=== 3. Today's batch ===")
    cmd_stage(argparse.Namespace(n=None, no_followups=not replies_ok and not args.no_imap), cfg)
    print("\n=== 4. Preview (check every >> CHECK line) ===")
    cmd_preview(argparse.Namespace(), cfg)
    print("\nNext: `outreach-send --test` (to you), read it, then "
          "`outreach-send --send` once you approve.")


def cmd_mark(args, cfg):
    """Record what happened with someone you emailed (shown in `outreach report`)."""
    _, _, sent_log = _paths(cfg)
    outcome = args.outcome.strip().lower()
    if outcome not in common.OUTCOMES:
        raise SystemExit(f"ERROR: outcome must be one of: {', '.join(common.OUTCOMES)}")
    fields, rows = _safe_rows(sent_log)
    email = common.normalize_email(args.email)
    hits = [r for r in rows if common.normalize_email(r.get("contact_email")) == email
            and not _is_followup(r)]
    if not hits:
        raise SystemExit(f"ERROR: {args.email} is not in {os.path.basename(sent_log)}.")
    for r in hits:
        r["outcome"] = outcome
        r["outcome_at"] = datetime.date.today().isoformat()
    fields = list(fields) + [f for f in ("outcome", "outcome_at") if f not in fields]
    common.backup(sent_log)
    common.write_db(sent_log, fields, rows)
    r = hits[0]
    print(f"Marked {r.get('first_name', '')} {r.get('last_name', '')} at "
          f"{r.get('company', '')}: {outcome}")


def cmd_replies(args, cfg):
    rc = subprocess.run([PY, "-m", "read_replies",
                         "--config", cfg["config_path"]]).returncode
    if rc:
        raise SystemExit(rc)


def cmd_migrate(args, cfg):
    _, _, sent_log = _paths(cfg)
    if not os.path.exists(sent_log):
        print(f"No sent log at {sent_log}; nothing to migrate.")
        return
    common.backup(sent_log)
    changed = common.migrate_sent_log(sent_log)
    print("Migrated sent_log to current schema (added missing columns)." if changed
          else "sent_log already has the current schema; no change.")


def cmd_followup(args, cfg):
    _, _, sent_log = _paths(cfg)
    _, rows = _safe_rows(sent_log)
    days = cfg["pacing"]["followup_after_days"]
    now = datetime.datetime.now(datetime.timezone.utc)

    seen, due, unknown, replied = set(), [], [], 0
    for r in rows:
        addr = (r.get("contact_email") or "").strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        if (r.get("last_reply") or "").strip():
            replied += 1          # they responded -> not a follow-up target
            continue
        company, sent_at = (r.get("company") or ""), (r.get("sent_at") or "").strip()
        try:
            dt = datetime.datetime.strptime(sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
            age = (now - dt).days
            if age >= days:
                due.append((company, addr, age))
        except ValueError:
            unknown.append((company, addr))

    print(f"=== Follow-up candidates (silent >= {days} days, no reply) ===")
    for company, addr, age in sorted(due, key=lambda t: -t[2]):
        print(f"  {company} <{addr}>  ({age}d ago)")
    if not due:
        print("  (none)")
    if replied:
        print(f"\n{replied} contact(s) excluded (already replied, per last_reply).")
    if unknown:
        print(f"{len(unknown)} contact(s) have no sent_at -> date unknown; "
              f"run `outreach backfill-dates --apply`.")
    print("\nReply detection is best-effort: `backfill-dates` catches same-address "
          "replies AND different-address replies that thread (In-Reply-To/References). "
          "Refresh it with `outreach backfill-dates`; the Gmail connector stays "
          "authoritative for edge cases.")


def cmd_backfill_dates(args, cfg):
    """Sync sent_at + last_reply from Gmail (Sent folder + replies). Universal.

    Pass 1 fills blank sent_at from Gmail's Sent folder (real send dates). Pass 2
    cross-checks who replied (latest message FROM each contact) and records
    last_reply, so `followup` can exclude people who already responded. Dry-run by
    default; --apply writes (backs up sent_log.csv first). Needs no external CRM.
    """
    import read_replies  # local import: only this command needs IMAP

    _, _, sent_log = _paths(cfg)
    if not os.path.exists(sent_log):
        print(f"No sent log at {sent_log}; nothing to backfill.")
        return
    fields, rows = common.read_rows(sent_log)
    em = cfg["email"]
    if not em.get("address"):
        raise SystemExit("ERROR: set [email].address in config.toml first.")

    our_addr = em["address"]
    sent_found, reply_found, bounced, thread_only = {}, {}, set(), 0
    meta = {}  # addr -> [message_ids] of mail we sent them (for thread matching)
    imap = common.imap_login(our_addr, common.get_password(em["keychain_service"]),
                             readonly=True, host=em["imap_host"], mailbox=read_replies.SENT_MAIL)
    try:
        print(f"Pass 1: send dates + message-ids for {len(rows)} contact(s) (Gmail Sent)...")
        for r in rows:
            addr = common.normalize_email(r.get("contact_email"))
            if not addr:
                continue
            date, msgids = read_replies.sent_meta_for(imap, addr)
            meta[addr] = msgids
            if date and not (r.get("sent_at") or "").strip():
                sent_found[addr] = date
        typ, _ = imap.select(common.ALL_MAIL, readonly=True)
        if typ != "OK":
            imap.select("INBOX", readonly=True)
        print(f"Pass 2: replies for {len(rows)} contact(s) (FROM-match + thread-match)...")
        for r in rows:
            addr = common.normalize_email(r.get("contact_email"))
            if not addr:
                continue
            by_from = read_replies.last_reply_for(imap, addr)
            by_thread, bounce = read_replies.thread_reply_for(imap, meta.get(addr, []),
                                                              our_addr)
            if bounce:
                bounced.add(addr)
            latest = max([d for d in (by_from, by_thread) if d], default=None)
            if latest:
                reply_found[addr] = latest
                if by_thread and not by_from:
                    thread_only += 1  # caught only because of thread matching
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    print(f"Found send dates for {len(sent_found)}; replies from {len(reply_found)} "
          f"contact(s) ({thread_only} via thread-match / different address); "
          f"{len(bounced)} bounced. (Bounce notices and automatic replies are not "
          f"counted as replies.)")
    if not args.apply:
        print("\nDry-run. Re-run with --apply to write into sent_log.csv.")
        return

    out_fields = list(fields)
    for f in common.SENT_LOG_FIELDS:
        if f not in out_fields:
            out_fields.append(f)
    new_rows = []
    for r in rows:
        addr = common.normalize_email(r.get("contact_email"))
        nr = {**{k: "" for k in out_fields}, **r}
        if not (nr.get("sent_at") or "").strip() and addr in sent_found:
            nr["sent_at"] = sent_found[addr]
        if addr in reply_found:
            # Never move last_reply backwards: keep the newer of existing vs found
            # (both are fixed-width UTC, so max() is chronological; "" loses to any).
            nr["last_reply"] = max(nr.get("last_reply") or "", reply_found[addr])
        elif addr in bounced:
            nr["last_reply"] = ""   # an earlier "reply" was this bounce notice
        new_rows.append(nr)
    common.backup(sent_log)
    common.write_db(sent_log, out_fields, new_rows)
    print(f"\nApplied -> {os.path.basename(sent_log)} "
          f"(sent_at for {len(sent_found)}, last_reply for {len(reply_found)}; "
          f"backup in .backups/).")
    if bounced:
        apply_bounces(cfg, sent_log, bounced)


def apollo_contacts_to_leads(data, cfg):
    """Turn an Apollo saved-contacts search result into hand-picked leads.

    Keeps contacts saved from [search].handpicked_sources on/after handpicked_since.
    """
    search = {**common.DEFAULT_SEARCH, **(cfg.get("search") or {})}
    sources = {s.lower() for s in search["handpicked_sources"]}
    since = str(search["handpicked_since"])
    leads = []
    for c in data.get("contacts", []):
        if (c.get("source") or "").lower() not in sources:
            continue
        if (c.get("created_at") or "")[:10] < since:
            continue
        leads.append({
            "first_name": c.get("first_name") or "",
            "last_name": c.get("last_name") or "",
            "contact_email": c.get("email") or "",
            "email_status": c.get("email_status") or "",
            "title": c.get("title") or "",
            "company": c.get("organization_name") or "",
            "linkedin_url": c.get("linkedin_url") or "",
            "apollo_id": c.get("person_id") or "",
            "source": common.HAND_PICKED,
        })
    return leads


def cmd_from_apollo(args, cfg):
    """Convert a saved Apollo contacts search (JSON, may be a saved tool-result file)
    into a leads file for `outreach add`. Only your hand-picks are kept."""
    try:
        raw = open(args.file, encoding="utf-8").read()
        data = json.loads(raw[raw.index("{"):])
    except (OSError, ValueError) as e:
        raise SystemExit(f"ERROR: can't read Apollo contacts from {args.file}: {e}")
    leads = apollo_contacts_to_leads(data, cfg)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(leads, f, indent=2, ensure_ascii=False)
    total = len(data.get("contacts", []))
    print(f"{len(leads)} hand-picked contact(s) of {total} -> {args.out}")
    for lead in leads:
        print(f"  {lead['first_name']} {lead['last_name']}, {lead['title']} at {lead['company']}")


EMORY_LINE = ("I noticed you have also studied in Emory and was genuinely impressed by "
              "your work as {article} {title} at {company}.")
LOOKING_WORDS = ("looking for", "open to work", "seeking", "job search", "between roles")


def short_title(title):
    """'Sr Data Scientist - Flight Safety, Ads' -> 'Senior Data Scientist' (for the line)."""
    t = re.split(r" [-|–] |, | \(|\|", (title or "").strip())[0].strip()
    return re.sub(r"\bSr\.?(?=\s)", "Senior", t)


def moved_reason(headline, company):
    """Why the LinkedIn headline suggests this person isn't at `company` now, or None.
    Deepline's job data can lag; an email to an old job bounces or reaches the wrong
    person, so we skip anyone whose own headline disagrees."""
    h = (headline or "").lower()
    if any(w in h for w in LOOKING_WORDS):
        return "headline says they're looking for a job (may have left)"
    key = common._company_key(company).split(" ")[0]
    named = re.findall(r"(?:@|\bat\b)\s*([a-z][\w&.\- ]{1,30})", h)
    if named and key and not any(key in n for n in named):
        return f"headline names another company ({named[0].strip()}), may have moved"
    return None


def deepline_to_leads(people, emails, cfg):
    """Join a Deepline people search (icypeas_find_people CSV) with its email
    waterfall results (CSV) into Emory-alumni leads. Returns (leads, skipped)."""
    by_url = {(e.get("linkedin_url") or "").rstrip("/"): e for e in emails}
    leads, skipped = [], []
    for p in people:
        name = f"{p.get('firstname', '')} {p.get('lastname', '')}".strip()
        url = (p.get("profileUrl") or "").rstrip("/")
        e = by_url.get(url, {})
        email = (e.get("email") or "").strip()
        if not email or (e.get("email_validated") or "").lower() != "true":
            skipped.append((name, "no verified work email found"))
            continue
        company = p.get("lastCompanyName") or e.get("company_name") or ""
        reason = moved_reason(p.get("headline"), company)
        if reason:
            skipped.append((name, reason))
            continue
        title = short_title(p.get("lastJobTitle"))
        leads.append({
            "first_name": re.sub(r"\s*\(.*?\)", "", p.get("firstname") or "").strip(),
            "last_name": (p.get("lastname") or "").strip().rstrip("."),
            "company": company,
            "company_size": p.get("lastCompanySize") or "",
            "company_domain": common.main_domain(p.get("lastCompanyWebsite") or e.get("domain")),
            "title": (p.get("lastJobTitle") or "").strip(),
            "contact_email": email,
            "email_status": "verified",
            "school": "Emory University",
            "start_date": p.get("lastJobStartDate") or "",
            "linkedin_url": url,
            "source": "deepline",
            "personal_line": EMORY_LINE.format(
                article="an" if title[:1].lower() in "aeiou" else "a",
                title=title, company=company),
            "personal_source": url,
        })
    return leads, skipped


def cmd_from_deepline(args, cfg):
    """Convert Deepline results (people search CSV + email waterfall CSV) into a leads
    file for `outreach add`. Keeps verified emails only; skips people who moved."""
    try:
        people = list(csv.DictReader(open(args.people, encoding="utf-8")))
        emails = list(csv.DictReader(open(args.emails, encoding="utf-8")))
    except OSError as e:
        raise SystemExit(f"ERROR: can't read Deepline results: {e}")
    leads, skipped = deepline_to_leads(people, emails, cfg)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(leads, f, indent=2, ensure_ascii=False)
    print(f"{len(leads)} lead(s) of {len(people)} -> {args.out}")
    for lead in leads:
        print(f"  {lead['first_name']} {lead['last_name']}, {lead['title']} at {lead['company']}")
    for name, why in skipped:
        print(f"  - skipped {name}: {why}")


def cmd_approve_templates(args, cfg):
    """Record your templates, as they are now, as approved. If a template changes
    later (by you or anyone), emails using it are stopped until you run this again."""
    for name in guard.approve_templates(cfg):
        print(f"  approved {name}")
    print(f"Saved to {guard.approved_path(cfg)}.")


def cmd_held(args, cfg):
    """List the emails the safety checks held for your review, with the reasons."""
    _, rows = _safe_rows(cfg["files"]["held"])
    if not rows:
        print("Nothing held.")
        return
    print(f"=== {len(rows)} held email(s) ===")
    for r in rows:
        print(f"  {r.get('first_name')} {r.get('last_name')} <{r.get('contact_email')}> "
              f"({r.get('company')}), held {(r.get('held_at') or '')[:10]}")
        for reason in (r.get("held_reason") or "").split("; "):
            print(f"    - {reason}")
        common.print_check_line(r, indent="    ")
    print("\nApprove with `outreach approve <email> ...` (or --all); "
          "drop with `outreach approve --drop <email>`.")


def cmd_approve(args, cfg):
    """Move held emails back into the queue, marked approved=yes (you read them), so
    they go out with the next batch. With --drop, remove them instead."""
    held_path = cfg["files"]["held"]
    fields, rows = _safe_rows(held_path)
    want = {common.normalize_email(e) for e in args.emails}
    if not args.all and not want:
        raise SystemExit("ERROR: give one or more emails, or --all.")
    pick = [r for r in rows if args.all or common.normalize_email(r.get("contact_email")) in want]
    unknown = want - {common.normalize_email(r.get("contact_email")) for r in pick}
    if unknown:
        raise SystemExit(f"ERROR: not in the held list: {', '.join(sorted(unknown))}")
    keep = [r for r in rows if r not in pick]
    common.backup(held_path)
    if not args.drop:
        queue = cfg["files"]["queue"]
        q_fields, q_rows = _safe_rows(queue)
        q_fields = list(q_fields or common.LEAD_FIELDS)
        q_fields += [f for f in ("approved",) if f not in q_fields]
        back = [{k: v for k, v in {**r, "approved": "yes"}.items() if k in q_fields}
                for r in pick]
        if os.path.exists(queue):
            common.backup(queue)
        common.write_db(queue, q_fields, back + q_rows)  # first in line
    common.write_db(held_path, fields, keep)
    verb = "Dropped" if args.drop else "Approved (back at the front of the queue)"
    print(f"{verb}: {len(pick)}. Still held: {len(keep)}.")


def cmd_spot_check(args, cfg):
    """Show a few random emails from the latest send day in full, exactly as sent
    (rebuilt from your approved template + that person's blanks)."""
    import random
    _, rows = _safe_rows(_paths(cfg)[2])
    rows = [r for r in rows if (r.get("sent_at") or "").strip()]
    if not rows:
        print("Nothing sent yet.")
        return
    day = max(r["sent_at"][:10] for r in rows)
    pool = [r for r in rows if r["sent_at"][:10] == day]
    print(f"=== Spot check: {min(args.n, len(pool))} of {len(pool)} sent on {day} ===")
    for r in random.sample(pool, min(args.n, len(pool))):
        print("\n" + rebuild_email(r, cfg))


def rebuild_email(r, cfg):
    """One email in full (TO, SUBJECT, body), rebuilt from its template + blanks."""
    by_name = {os.path.basename(p): p for p in common.template_paths(cfg)}
    path = by_name.get(r.get("template_version") or "")
    if not path:
        return f"(can't rebuild the email to {r.get('contact_email')}: template unknown)"
    headers, body = common.load_template(path)
    r = common.with_blanks(r, cfg)
    return (f"TO:      {r.get('contact_email')}\n"
            f"SUBJECT: {r.get('subject_sent') or common.render(headers['SUBJECT'], r)}\n\n"
            + common.render(body, r).strip())


DEEPLINE_CREDITS_PER_PERSON = 0.8   # measured on 2026-11-01: ~8.4 credits for 11 usable leads


def deepline_credits():
    """{'balance', 'used_30', 'cap'} from `deepline billing usage`, or None if unknown."""
    try:
        out = subprocess.run(["deepline", "billing", "usage"], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = {}
    for key, label in (("balance", "Balance"), ("used_30", "Last 30 days usage"),
                       ("cap", "Monthly usage cap")):
        m = re.search(rf"{label}:\s*([\d.]+)", out)
        if m:
            found[key] = float(m.group(1))
    return found if "balance" in found else None


def credit_warnings(credits, per_day, cost=DEEPLINE_CREDITS_PER_PERSON):
    """When to tell you to add Deepline credits. The finder keeps 2 days of people
    queued, so warn at 5 days left (time to top up) and again at 2 (urgent)."""
    if not credits:
        return ["Couldn't check Deepline credits (run `deepline billing usage`)."]
    need = max(per_day, 1) * cost            # credits per day at today's ramp step
    days = credits["balance"] / need
    out = []
    if days < 2:
        out.append(f"TOP UP DEEPLINE NOW: {credits['balance']:.1f} credits left, about "
                   f"{days:.1f} day(s) of new people at {per_day}/day.")
    elif days < 5:
        out.append(f"Top up Deepline soon: {credits['balance']:.1f} credits left, about "
                   f"{days:.0f} days of new people at {per_day}/day.")
    cap = credits.get("cap")
    if cap and credits.get("used_30", 0) + 2 * need > cap:
        out.append(f"Deepline monthly cap almost reached ({credits.get('used_30', 0):.1f} of "
                   f"{cap:.0f} used in 30 days): raise it with `deepline billing set-limit`.")
    return out


def build_feedback(cfg, today=None, credits=None, now=None):
    """The daily feedback: (subject, text). Short; says "all good" when nothing needs you."""
    today = today or datetime.date.today()
    day = today.isoformat()
    queue, _, sent_log = _paths(cfg)
    _, sent_rows = _safe_rows(sent_log)
    results = person_results(sent_rows, cfg)
    n = len(results)
    replied = [p for p in results if p["replied"]]
    referrals = sum(p["outcome"] == "referral_given" for p in results)
    since = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=1)
    new_replies = [p["row"] for p in replied
                   if p["replied_at"] >= since.strftime("%Y-%m-%dT%H:%M:%SZ")]
    bounced_today = [r for r in sent_rows if (r.get("bounced_at") or "")[:10] == day]
    run = (common.load_state(cfg).get("last_send") or {})
    run = run if run.get("date") == day else {}
    ramp = common.ramp_status(cfg, first_send=common.first_send_date(sent_rows), today=today)
    per_day = ramp["per_day"] or common.DEFAULT_RAMP["steps"][ramp["step"]][0]
    held = _safe_rows(cfg["files"]["held"])[1]
    queued = len(_safe_rows(queue)[1])
    sent_n = sum(_sent_today(r, today) for r in sent_rows)  # every email Gmail sent today
    failed_n = len((run or {}).get("failed") or [])

    needs, good = [], []
    if not run:
        needs.append("Nothing was sent today (the 10 am run didn't send).")
    elif run.get("stopped"):
        needs.append("SAFETY STOP: nothing was sent. Problems: "
                     + "; ".join(run.get("problems") or ["see the 10 am chat"]))
    else:
        sent_today = [r for r in sent_rows if _sent_today(r) and not _is_followup(r)]
        hiring = sum(common.has_open_roles(r, cfg) for r in sent_today)
        good.append(f"Sent {sent_n} email(s)"
                    + (f", {hiring} to companies hiring interns." if sent_today else "."))
        if run.get("failed"):
            needs.append(f"{len(run['failed'])} failed to send: " + ", ".join(run["failed"]))
        short = per_day - sent_n - len(held) - failed_n
        if short > 0 and not ramp["paused_until"]:
            needs.append(f"{short} short of today's target of {per_day}: not enough people "
                         "were ready (queue ran out, or the rest are at companies already "
                         "at their limit). The contact finder needs to add more.")
    if ramp["paused_until"]:
        needs.append(f"Sending is paused until {ramp['paused_until']} (a warning sign).")
    if bounced_today:
        needs.append(f"{len(bounced_today)} email(s) bounced (address doesn't exist; that "
                     "person won't be emailed again): "
                     + ", ".join(r.get("contact_email", "") for r in bounced_today))
    if held:
        needs.append(f"{len(held)} email(s) held, waiting for your review: "
                     + ", ".join(f"{r.get('first_name')} {r.get('last_name')} "
                                 f"({r.get('company')})" for r in held)
                     + ". Say \"approve <name>\" or \"drop <name>\" in any Claude chat.")
    if queued < per_day // 2:
        needs.append(f"Only {queued} people are waiting to be emailed, and tomorrow can send "
                     f"{per_day}. The contact finder couldn't find enough; check Deepline credits.")
    needs += credit_warnings(credits, per_day)
    stale = hiring_todo(cfg, today=today)
    if stale:
        good.append(f"Hiring check: {len(stale)} target companies still to (re)check; "
                    "the contact finder checks 15 a day.")

    lines = [f"Outreach daily report, {today:%a %b %-d}", "",
             f"TODAY'S TARGET:              {per_day}",
             f"SENT TODAY:                  {sent_n}",
             f"HELD, WAITING FOR YOUR REVIEW: {len(held)}",
             f"BOUNCED TODAY (bad address): {len(bounced_today)}",
             f"FAILED TO SEND:              {failed_n}",
             f"NEW REAL REPLIES (24 hours): {len(new_replies)}",
             f"WAITING FOR A LATER DAY:     {queued} people (tomorrow sends up to {per_day})",
             ""]
    lines.append("Everything's good." if not needs else "Needs you:")
    lines += [f"  - {x}" for x in needs]
    lines += ["", "Today:"] + [f"  - {x}" for x in good]
    if new_replies:
        lines.append("  - New real replies (last 24 hours; bounces and out-of-office "
                     "replies not counted): " + ", ".join(
            f"{r.get('first_name')} {r.get('last_name')} ({r.get('company')})"
            for r in new_replies))
    else:
        lines.append("  - No new replies in the last 24 hours.")
    lines += ["", f"RESPONSE RATE:          {len(replied)} replied / {n} emailed = "
                  f"{_rate(len(replied), n).strip()}",
              f"REFERRAL SUCCESS RATE:  {referrals} referrals / {n} emailed = "
              f"{_rate(referrals, n).strip()}",
              f"People who replied so far: {len(replied)}",
              "", f"Ramp: up to {per_day} emails/day (step {ramp['step'] + 1} of 4).",
              "", "Got a referral or a coffee chat? Tell Claude, and it will be recorded."]
    if held:
        lines += ["", "=" * 60, f"EMAILS WAITING FOR YOUR REVIEW ({len(held)})", "=" * 60,
                  "These were NOT sent. The safety checks stopped them for the reason shown.",
                  "Read each one, then tell Claude in any chat, e.g. \"approve Ao Xu\" (it goes",
                  "out with the next batch exactly as shown) or \"drop Ao Xu\" (never sent)."]
        for i, r in enumerate(held, 1):
            lines += ["", f"--- {i}. {r.get('first_name')} {r.get('last_name')}, "
                          f"{r.get('title') or 'title unknown'} at {r.get('company')} ---",
                      "Why held:"]
            lines += [f"  - {x}" for x in (r.get("held_reason") or "").split("; ") if x]
            if (r.get("personal_source") or "").strip():
                lines.append(f"Source of the personal line: {r.get('personal_source')}")
            lines += ["", rebuild_email(r, cfg)]
    subject = (f"Outreach {today:%b %-d}: all good" if not needs
               else f"Outreach {today:%b %-d}: {len(needs)} thing(s) need you")
    return subject, "\n".join(lines) + "\n"


def cmd_feedback(args, cfg):
    """Today's short feedback (what went well, rates, replies, what failed). With
    --email, also sends it to your own Gmail address (nobody else)."""
    if not args.no_imap and _safe_rows(_paths(cfg)[2])[1]:
        try:
            cmd_backfill_dates(argparse.Namespace(apply=True), cfg)
        except Exception as e:  # still send the report, with the replies we have
            print(f"Gmail reply check failed: {e}")
        print()
    subject, text = build_feedback(cfg, credits=deepline_credits())
    print(f"SUBJECT: {subject}\n\n{text}")
    if not args.email:
        return
    from email.message import EmailMessage
    em = cfg["email"]
    msg = EmailMessage()
    msg["From"] = msg["To"] = em["address"]
    msg["Subject"] = subject
    msg.set_content(text)
    with common.smtp_session(em["smtp_host"], int(em["smtp_port"]), em["address"],
                             common.get_password(em["keychain_service"])) as smtp:
        smtp.send_message(msg)
    print(f"Sent to {em['address']}.")


ADD_REQUIRED = ("contact_email", "first_name", "company")


def check_lead(r, cfg, known_emails):
    """Why this lead can't be added (a short reason), or None if it's fine."""
    missing = [k for k in ADD_REQUIRED if not (r.get(k) or "").strip()]
    if missing:
        return f"missing {', '.join(missing)}"
    email = common.normalize_email(r["contact_email"])
    if "@" not in email:
        return "not an email address"
    if email in known_emails:
        return "already in queue, batch or sent log"
    search = {**common.DEFAULT_SEARCH, **(cfg.get("search") or {})}
    accepted = [s.lower() for s in search["accepted_email_statuses"]]
    status = (r.get("email_status") or "").strip().lower()
    if status not in accepted:
        return f"email status is {status or 'unknown'!r}, need one of {accepted}"
    if status == common.PUBLIC:
        if not (r.get("email_source") or "").strip():
            return "public email without email_source (where it was published)"
        seen = common.years_ago(r.get("email_seen"))
        if seen is not None and seen > search["max_email_age_years"]:
            return f"public email last seen {seen:.1f} years ago (too old, may bounce)"
    if common.skip_reason(r, cfg):
        return common.skip_reason(r, cfg)
    company_domain = common.main_domain(r.get("company_domain"))
    email_dom = common.main_domain(common.email_domain(r.get("contact_email")))
    if (company_domain and status != common.PUBLIC and email_dom != company_domain
            and email_dom != common.target_email_domain(r.get("company"), cfg)):
        return f"email is at {email_dom}, not their current company ({company_domain})"
    if common.too_small(r, cfg):
        return "company under min_employees"
    return None


def cmd_add(args, cfg):
    """Add leads from a JSON file (a list of objects, e.g. from the contact finder)
    to the queue, after checking each one. Use --dry-run to see the result first."""
    queue, db, sent_log = _paths(cfg)
    try:
        with open(args.file, encoding="utf-8") as f:
            leads = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"ERROR: can't read {args.file}: {e}")
    if not isinstance(leads, list) or not all(isinstance(x, dict) for x in leads):
        raise SystemExit("ERROR: the file must hold a JSON list of objects.")

    q_fields, q_rows = _safe_rows(queue)
    _, db_rows = _safe_rows(db)
    known = common.sent_emails(sent_log) if os.path.exists(sent_log) else set()
    known |= {common.normalize_email(r.get("contact_email")) for r in q_rows + db_rows}

    added, skipped = [], []
    for lead in leads:
        r = {k: "" if v is None else str(v).strip() for k, v in lead.items()}
        reason = check_lead(r, cfg, known)
        if reason:
            skipped.append((r, reason))
            continue
        r["found_at"] = r.get("found_at") or common.utcnow_iso()
        added.append(r)
        known.add(common.normalize_email(r["contact_email"]))

    verb = "Would add" if args.dry_run else "Added"
    print(f"{verb} {len(added)} to {os.path.basename(queue)}; skipped {len(skipped)}.")
    for r in added:
        tier = common.company_tier(r, cfg)
        tier = "T?" if tier == common.UNKNOWN_TIER else f"T{tier}"
        kind = common.pick_type(r, cfg)
        print(f"  + {tier} [{kind}] {r.get('first_name')} {r.get('last_name', '')}, "
              f"{r.get('title', '')} at {r['company']} <{r['contact_email']}>")
        common.print_check_line(r)
    for r, reason in skipped:
        print(f"  - skipped {r.get('first_name', '?')} {r.get('last_name', '')} at "
              f"{r.get('company', '?')}: {reason}")
    if args.dry_run or not added:
        return

    fields = list(q_fields or common.LEAD_FIELDS)
    for r in added:
        fields += [k for k in r if k not in fields]
    common.backup(queue)
    common.write_db(queue, fields, q_rows + added)


HIRING_RECHECK_DAYS = 7


def hiring_todo(cfg, today=None, limit=None):
    """Target companies whose hiring status is missing or older than a week, companies
    with people in the queue first, then by tier. Covered companies are left out."""
    today = today or datetime.date.today()
    checked = {common._company_key(r.get("company")): (r.get("checked") or "").strip()
               for r in common._csv_rows(cfg["files"].get("open_roles"))}
    queued = {common._company_key(r.get("company")) for r in _safe_rows(_paths(cfg)[0])[1]}
    todo = []
    for r in common._csv_rows(cfg["files"].get("targets")):
        key = common._company_key(r.get("company"))
        if not key or common.covered_company({"company": r.get("company")}, cfg):
            continue
        try:
            age = (today - datetime.date.fromisoformat(checked.get(key, ""))).days
        except ValueError:
            age = None
        if age is None or age >= HIRING_RECHECK_DAYS:
            tier = int(r["tier"]) if (r.get("tier") or "").strip().isdigit() else 9
            todo.append((key not in queued, tier, r.get("company"), r.get("domain"), age))
    todo.sort(key=lambda t: t[:2])
    return [(c, d, age) for _, _, c, d, age in todo][:limit]


def cmd_hiring(args, cfg):
    """Which companies are hiring a Data/ML/AI intern (data/open_roles.csv). People at
    those companies are emailed first. `hiring set` records one company's status."""
    path = cfg["files"]["open_roles"]
    if args.action == "set":
        if not args.company or args.status not in ("yes", "no"):
            sys.exit("usage: outreach hiring set <company> yes|no [--link URL] [--role TITLE]")
        if args.status == "yes" and not (args.link or "").startswith("http"):
            sys.exit("A 'yes' needs --link: the job posting you found.")
        fields, rows = _safe_rows(path)
        fields = fields or common.OPEN_ROLES_FIELDS
        key = common._company_key(args.company)
        rows = [r for r in rows if common._company_key(r.get("company")) != key]
        domain = args.domain or next(
            (t.get("domain", "") for t in common._csv_rows(cfg["files"]["targets"])
             if common._company_key(t.get("company")) == key), "")
        rows.append({"company": args.company, "domain": domain, "hiring": args.status,
                     "role": args.role or "", "link": args.link or "",
                     "checked": datetime.date.today().isoformat()})
        rows.sort(key=lambda r: (r.get("hiring") != "yes", r.get("company", "").lower()))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        common.write_db(path, fields, rows)
        common.load_open_roles.cache_clear()
        print(f"{args.company}: hiring = {args.status}")
        return
    rows = common._csv_rows(path)
    yes = [r for r in rows if (r.get("hiring") or "").strip().lower() in common.YES]
    print(f"Hiring a Data/ML/AI intern ({len(yes)} of {len(rows)} companies checked):")
    for r in yes:
        print(f"  + {r.get('company')}: {r.get('role')}  (checked {r.get('checked')})"
              f"\n      {r.get('link')}")
    todo = hiring_todo(cfg, limit=args.n)
    print(f"\nTo check ({'first ' + str(args.n) if args.n else 'all'}; missing or older "
          f"than {HIRING_RECHECK_DAYS} days, companies with people in the queue first):")
    for company, domain, age in todo:
        print(f"  - {company} ({domain})" + (f", last checked {age} days ago" if age else ""))
    if not todo:
        print("  (none)")


COMMANDS = {
    "status": cmd_status, "setup": cmd_setup, "find": cmd_find,
    "stage": cmd_stage, "preview": cmd_preview, "replies": cmd_replies,
    "followup": cmd_followup, "migrate": cmd_migrate,
    "backfill-dates": cmd_backfill_dates, "add": cmd_add,
    "from-apollo": cmd_from_apollo, "from-deepline": cmd_from_deepline,
    "health": cmd_health, "mark": cmd_mark,
    "report": cmd_report, "daily": cmd_daily,
    "approve-templates": cmd_approve_templates, "held": cmd_held, "approve": cmd_approve,
    "spot-check": cmd_spot_check, "feedback": cmd_feedback, "hiring": cmd_hiring,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config path (default ./config.toml or $OUTREACH_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "stage":
            p.add_argument("-n", type=int, default=None,
                           help="how many to stage (default: [pacing].batch_size)")
        if name == "add":
            p.add_argument("file", help="JSON file: a list of leads (objects)")
            p.add_argument("--dry-run", action="store_true",
                           help="show what would be added, change nothing")
        if name == "feedback":
            p.add_argument("--email", action="store_true",
                           help="also send it to your own Gmail address")
        if name in ("daily", "report", "feedback"):
            p.add_argument("--no-imap", action="store_true",
                           help="don't check Gmail (replies/bounces); for offline runs")
        if name == "mark":
            p.add_argument("email", help="the person's email address")
            p.add_argument("outcome", help=" / ".join(common.OUTCOMES))
        if name == "health":
            p.add_argument("--spam-check", choices=["inbox", "spam"],
                           help="record where your spam-check email landed")
            p.add_argument("--no-imap", action="store_true",
                           help="skip checking Gmail for bounces")
        if name == "from-apollo":
            p.add_argument("file", help="Apollo saved-contacts search result (JSON)")
            p.add_argument("-o", "--out", required=True, help="leads JSON file to write")
        if name == "from-deepline":
            p.add_argument("people", help="Deepline people search results (CSV)")
            p.add_argument("emails", help="Deepline email waterfall results (CSV)")
            p.add_argument("-o", "--out", required=True, help="leads JSON file to write")
        if name == "approve":
            p.add_argument("emails", nargs="*", help="email address(es) to approve")
            p.add_argument("--all", action="store_true", help="every held email")
            p.add_argument("--drop", action="store_true",
                           help="remove them from the held list instead (never send)")
        if name == "hiring":
            p.add_argument("action", nargs="?", choices=["list", "set"], default="list")
            p.add_argument("company", nargs="?", help="for set: the company name")
            p.add_argument("status", nargs="?", help="for set: yes or no")
            p.add_argument("--link", help="for set yes: the job posting link")
            p.add_argument("--role", help="for set: the internship title")
            p.add_argument("--domain", help="for set: company domain (default: target list)")
            p.add_argument("-n", type=int, default=None, help="list: show this many to check")
        if name == "spot-check":
            p.add_argument("-n", type=int, default=2, help="how many (default 2)")
        if name == "backfill-dates":
            p.add_argument("--apply", action="store_true",
                           help="write the dates (default: dry-run preview)")
    args = ap.parse_args()
    COMMANDS[args.cmd](args, common.load_config(common.find_config(args.config)))


if __name__ == "__main__":
    main()
