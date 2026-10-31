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
import datetime
import json
import os
import subprocess
import sys

import common

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

    resume = cfg["files"]["resume"]
    print(f"Resume: {'OK ' + resume if os.path.exists(resume) else 'MISSING -> ' + resume}")
    for kind, path in (cfg.get("templates") or {"default": cfg["files"]["template"]}).items():
        status = "OK " if os.path.exists(path) else "MISSING -> copy the .example, then edit "
        print(f"Template [{kind}]: {status}{path}")

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


def _sent_today(r):
    """True if this sent-log row's sent_at (UTC) falls on today's local date."""
    try:
        dt = datetime.datetime.strptime((r.get("sent_at") or "").strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    local = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
    return local.date() == datetime.date.today()


def cmd_stage(args, cfg):
    """Move today's batch from the queue into the database, following the rules:

    1. remove anyone already sent or staged (by email), or on the do-not-contact list
    2. skip companies under [company_limits].min_employees
    3. fill a blank `type` ([routing]), `tier` (target list) and surname tags
    4. order: tier (1 first) -> open intern roles first -> priority groups -> queue order;
       each tier gets its [tier_share] of the day first, leftover slots go to the best rest
    5. at most company_limit people per company ([company_limits], by size)
    6. at most [daily] emails per type today (counting staged + sent today)
    7. at most N in total (-n, default [pacing].batch_size)
    People skipped by 2 and 5-7 stay in the queue (for another day, or a changed setting).
    """
    queue, db, sent_log = _paths(cfg)
    n = args.n if args.n is not None else cfg["pacing"]["batch_size"]
    if n <= 0:
        print(f"Nothing to do: stage count is {n}.")
        return

    q_fields, q_rows = _safe_rows(queue)
    if not q_rows:
        print("Queue is empty (header only). Run `find` + the contact finder to refill.")
        return
    db_fields, db_rows = _safe_rows(db)
    fields = list(db_fields or q_fields)
    for extra in ("type", "tier", "priority_tags"):
        if extra not in fields:
            fields.append(extra)

    daily = cfg.get("daily") or common.DEFAULT_DAILY
    blk_emails = common.sent_emails(sent_log)
    blk_emails |= {common.normalize_email(r.get("contact_email")) for r in db_rows}
    _, sent_rows = _safe_rows(sent_log)
    per_company = collections.Counter(_company(r) for r in sent_rows + db_rows)
    todays = db_rows + [r for r in sent_rows if _sent_today(r)]
    used_today = collections.Counter(common.pick_type(r, cfg) for r in todays)
    used_tier = collections.Counter(common.company_tier(r, cfg) for r in todays)
    tier_cap = common.tier_caps(cfg, sum(daily.values()))

    def prepared(r):
        out = dict(r, type=common.pick_type(r, cfg), tier=str(common.company_tier(r, cfg)))
        out["priority_tags"] = ", ".join(sorted(common.auto_tags(r, cfg)))
        return out

    def rank(pair):
        r = pair[1]
        return (int(r["tier"]), not common.has_open_roles(r), -common.priority_score(r, cfg))

    # Best first; sorted() is stable, so ties keep queue order.
    ranked = sorted(((r, prepared(r)) for r in q_rows), key=rank)
    to_move, gone, why = [], set(), {}
    dropped, blocked = 0, []
    # Pass 1 fills each tier up to its share of the day ([tier_share]); pass 2 gives
    # any slots a tier couldn't use to the best people left, whatever their tier.
    for by_share in (True, False):
        for orig, r in ranked:
            if id(orig) in gone:
                continue
            email = common.normalize_email(r.get("contact_email"))
            kind, tier = r["type"], int(r["tier"])
            if email in blk_emails:
                dropped += 1                 # already sent or staged -> remove from queue
                gone.add(id(orig))
            elif common.on_do_not_contact(r, cfg):
                blocked.append(r)            # on the do-not-contact list -> remove
                gone.add(id(orig))
            elif common.too_small(r, cfg):
                why[id(orig)] = "small"      # under min_employees -> stays in queue
            elif _company(r) and per_company[_company(r)] >= common.company_limit(r, cfg):
                why[id(orig)] = "company"    # company at its limit -> stays in queue
            elif kind in daily and used_today[kind] >= daily[kind]:
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
    waiting = collections.Counter(w for k, w in why.items() if k not in gone)
    leftover = [r for r in q_rows if id(r) not in gone]  # original order, unchanged

    if not to_move and not dropped and not blocked:
        print("Nothing new to stage today.")
        _print_waiting(waiting, used_today, daily, used_tier, tier_cap)
        return

    common.backup(queue)
    common.backup(db)
    # DB grows first (additive), queue is rewritten without moved-or-handled rows.
    # If interrupted between the two writes, the email dedup above plus the
    # idempotency check in send.py mean nothing double-sends; backups aid recovery.
    common.write_db(db, fields, db_rows + to_move)
    common.write_db(queue, q_fields, leftover)

    print(f"Staged {len(to_move)} -> {os.path.basename(db)}; "
          f"dropped {dropped} already-handled; {len(leftover)} left in queue.")
    for r in to_move:
        groups = common.priority_groups(r, cfg)
        star = f"  * {', '.join(groups)}" if groups else ""
        tier = "T?" if int(r["tier"]) == common.UNKNOWN_TIER else f"T{r['tier']}"
        roles = " open-roles" if common.has_open_roles(r) else ""
        print(f"  + {tier} [{r['type']}] {r.get('company')} <{r.get('contact_email')}>"
              f"{roles}{star}")
    for r in blocked:
        print(f"  x removed (do-not-contact list): {r.get('first_name')} {r.get('last_name')}"
              f" at {r.get('company')}")
    _print_waiting(waiting, used_today, daily, used_tier, tier_cap)
    print("\nNext: `outreach preview`, then `outreach-send --test`, then `outreach-send --send`.")


WAITING_REASONS = {
    "small": "company is under min_employees ([company_limits])",
    "company": "their company is at its limit ([company_limits])",
    "type": "today's limit for their type is used up ([daily])",
    "full": "today's batch is full (-n / [pacing].batch_size)",
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
    # `-m` works whether installed as a wheel or run from a source checkout.
    rc = subprocess.run([PY, "-m", "send", "--config", cfg["config_path"]]).returncode
    if rc:
        raise SystemExit(rc)


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
    sent_found, reply_found, thread_only = {}, {}, 0
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
            by_thread = read_replies.thread_reply_for(imap, meta.get(addr, []), our_addr)
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
          f"contact(s) ({thread_only} via thread-match / different address).")
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
        new_rows.append(nr)
    common.backup(sent_log)
    common.write_db(sent_log, out_fields, new_rows)
    print(f"\nApplied -> {os.path.basename(sent_log)} "
          f"(sent_at for {len(sent_found)}, last_reply for {len(reply_found)}; "
          f"backup in .backups/).")


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
    if common.on_do_not_contact(r, cfg):
        return "on the do-not-contact list"
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


COMMANDS = {
    "status": cmd_status, "setup": cmd_setup, "find": cmd_find,
    "stage": cmd_stage, "preview": cmd_preview, "replies": cmd_replies,
    "followup": cmd_followup, "migrate": cmd_migrate,
    "backfill-dates": cmd_backfill_dates, "add": cmd_add,
    "from-apollo": cmd_from_apollo,
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
        if name == "from-apollo":
            p.add_argument("file", help="Apollo saved-contacts search result (JSON)")
            p.add_argument("-o", "--out", required=True, help="leads JSON file to write")
        if name == "backfill-dates":
            p.add_argument("--apply", action="store_true",
                           help="write the dates (default: dry-run preview)")
    args = ap.parse_args()
    COMMANDS[args.cmd](args, common.load_config(common.find_config(args.config)))


if __name__ == "__main__":
    main()
