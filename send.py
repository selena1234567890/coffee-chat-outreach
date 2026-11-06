#!/usr/bin/env python3
"""Keychain-backed sender for the internship outreach pipeline.

Replaces the interactive `mailmerge` password prompt: the Gmail app password is
read from the macOS Keychain at send time, so no TTY / hidden prompt is needed.
This runs anywhere (including non-interactively).

Files (paths come from config.toml [files]):
  - the database CSV    targets (header: contact_email,contact_name,company,role,subject,note)
  - the template        email, with TO/SUBJECT/FROM/ATTACHMENT headers + {{placeholders}}
  - config.toml         your Gmail address, SMTP host/port ([email])
On a successful send it archives the sent rows into sent_log.csv and resets the
database to header-only, so no company is ever emailed twice.

Shared plumbing (Keychain, SMTP, rendering, CSV I/O) lives in common.py.

Usage:
  python3 send.py            # dry-run: render and preview every email, send nothing
  python3 send.py --send     # preview, then ask 'Send N emails? [y/N]', then send
  python3 send.py --send --yes   # skip the confirmation prompt (hands-off)
  python3 send.py --test     # send the first 2 emails to YOURSELF only (no log)

Each row's `type` column (e.g. coffee_chat / referral) picks its template from the
[templates] section of config.toml. A row with any blank {{placeholder}} blocks the
whole batch, so nobody ever gets "Hi ,".

One-time Keychain setup (stores the Gmail APP password, encrypted, not on disk):
  security add-generic-password -s mailmerge-gmail -a "$USER" -w
"""
import argparse
import contextlib
import datetime
import fcntl
import html
import mimetypes
import os
import random
import smtplib
import sys
import time
from email.message import EmailMessage
from email.utils import make_msgid

import common
import guard

TEST_COUNT = 2  # how many emails --test sends to yourself
LOCK_NAME = ".send.lock"  # in the workspace; only one real send may run at a time


@contextlib.contextmanager
def send_lock(workspace):
    """Hold an exclusive lock while sending, so two runs (say the 10 am task and a
    manual send) can never email the same batch twice. Exits if one already runs."""
    with open(os.path.join(workspace, LOCK_NAME), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("ERROR: another send is already running. Nothing sent; "
                     "wait for it to finish.")
        yield


def already_sent(r, log_path):
    """True if this row's email already went out (re-read the sent log right before
    each send, in case another run sent it since this batch was read)."""
    if not os.path.exists(log_path):
        return False
    em = common.normalize_email(r.get("contact_email"))
    followup = (r.get("type") or "").strip().lower() == "followup"
    for x in common.read_rows(log_path)[1]:
        if common.normalize_email(x.get("contact_email")) != em:
            continue
        if not followup or (x.get("type") or "").strip().lower() == "followup":
            return True
    return False


def build_message(row, headers, body, base_dir):
    """Render an email; ATTACHMENT is resolved relative to `base_dir` (the workspace)."""
    msg = EmailMessage()
    msg["To"] = common.render(headers["TO"], row)
    msg["Subject"] = common.render(headers["SUBJECT"], row)
    msg["From"] = common.render(headers.get("FROM", ""), row)
    if row.get("message_id"):
        msg["Message-ID"] = row["message_id"]
    if row.get("in_reply_to"):  # a follow-up: keep it in the same email thread
        msg["In-Reply-To"] = row["in_reply_to"]
        msg["References"] = row["in_reply_to"]
    text = common.render(body, row)
    msg.set_content(text)
    link = (row.get("resume_link") or "").strip()
    label = (row.get("resume_link_text") or "").strip()
    if link and label and link in text:
        # Show a short clickable name instead of the long link; the plain-text copy
        # (for mail apps without HTML) keeps the full link.
        a = f'<a href="{html.escape(link, quote=True)}">{html.escape(label)}</a>'
        page = html.escape(text).replace(html.escape(link), a).replace("\n", "<br>\n")
        msg.add_alternative(f"<div>{page}</div>", subtype="html")
    attach = headers.get("ATTACHMENT")
    if attach:
        path = common.resolve(common.render(attach, row), base_dir)
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))
    return msg


def wait_between_emails(cfg):
    """Pause a random [sending] min_gap_seconds..max_gap_seconds, so sends look human."""
    gap = {**common.DEFAULT_SENDING, **(cfg.get("sending") or {})}
    seconds = random.uniform(gap["min_gap_seconds"], gap["max_gap_seconds"])
    print(f"  (waiting {seconds:.0f}s before the next email)")
    time.sleep(seconds)


def send_test(cfg, plan, workspace, to=None):
    """Send rendered emails to the sender's OWN address, marked [TEST].

    Nothing is logged and the database is untouched, so the real send still
    happens later. Use it to see exactly what a recipient will see. With `to`
    (a spam check), the email goes there unchanged, with no [TEST] mark.
    """
    try:
        common.validate_for_send(cfg)
    except ValueError as e:
        sys.exit(f"ERROR: {e}")
    em = cfg["email"]
    me = em["address"]
    password = common.get_password(em["keychain_service"])
    with common.smtp_session(em["smtp_host"], int(em["smtp_port"]), me, password) as smtp:
        for r, headers, body in plan:
            msg = build_message(r, headers, body, workspace)
            real_to = msg["To"]
            msg.replace_header("To", to or me)
            if not to:
                msg.replace_header("Subject", f"[TEST to {real_to}] {msg['Subject']}")
            smtp.send_message(msg)
            print(f"  test sent -> {to or me} (would go to {real_to})")
    print("\nCheck that inbox. Nothing was logged; run --send when it looks right.")


def main():
    if "--send" in sys.argv[1:]:
        cfg = common.load_config(common.find_config(_config_arg()))
        with send_lock(cfg["workspace"]):
            return _main()
    return _main()


def _config_arg():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--config" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
    return None


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="config path (default ./config.toml or $OUTREACH_CONFIG)")
    ap.add_argument("--send", action="store_true", help="actually send (default: dry-run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--test", action="store_true",
                    help=f"send the first {TEST_COUNT} emails to your own address only")
    ap.add_argument("--spam-check", metavar="ADDRESS",
                    help="send the first email, unchanged, to another address of yours "
                         "(e.g. Outlook) to see if it lands in inbox or spam")
    args = ap.parse_args()

    cfg = common.load_config(common.find_config(args.config))
    workspace = cfg["workspace"]
    DB = cfg["files"]["database"]
    LOG = cfg["files"]["sent_log"]

    fieldnames, rows = common.read_rows(DB)
    if not rows:
        print(f"No targets in {os.path.basename(DB)} (header only). Nothing to send.")
        return

    # Idempotency + de-dup. Never send to an address already in sent_log.csv (closes
    # the crash window where a row was archived but not yet removed from the DB), and
    # never send the SAME address twice within one batch (case/whitespace-insensitive).
    already = common.sent_emails(LOG)
    log_rows = common.read_rows(LOG)[1] if os.path.exists(LOG) else []
    followed = {common.normalize_email(r.get("contact_email")) for r in log_rows
                if (r.get("type") or "").strip().lower() == "followup"}
    seen, fresh = set(), []
    n_sent = n_dup = n_blank = n_dnc = 0
    for r in rows:
        em = common.normalize_email(r.get("contact_email"))
        if not em:
            n_blank += 1
        elif common.skip_reason(r, cfg):
            n_dnc += 1
        elif (em in followed if (r.get("type") or "").strip().lower() == "followup"
              else em in already):
            n_sent += 1  # a first email, or a follow-up, already went to this address
        elif em in seen:
            n_dup += 1
        else:
            seen.add(em)
            fresh.append(r)
    for label, n in (("already in the sent log", n_sent),
                     ("duplicated within this batch", n_dup),
                     ("missing a contact_email", n_blank),
                     ("on the do-not-contact list or at a covered company", n_dnc)):
        if n:
            print(f"Skipping {n} row(s) {label}.")
    if n_sent or n_dup or n_blank or n_dnc:
        print()
    rows = fresh
    if not rows:
        print("Nothing new to send.")
        return

    # Pick each row's template (by `type`) and check every placeholder is filled.
    # Any problem blocks the whole batch: fix the CSV, then re-run.
    loaded, plan, problems = {}, [], []
    rows = [common.with_blanks(r, cfg) for r in rows]
    for r in rows:
        who = r.get("contact_email")
        try:
            path = common.template_for(r, cfg)
        except ValueError as e:
            problems.append(f"{who}: {e}")
            continue
        if path not in loaded:
            if not os.path.exists(path):
                sys.exit(f"ERROR: template not found: {path}")
            headers, body = common.load_template(path)
            missing = [h for h in ("TO", "SUBJECT") if h not in headers]
            if missing:
                sys.exit(f"ERROR: template {path} missing header(s): {', '.join(missing)}")
            loaded[path] = (headers, body)
        headers, body = loaded[path]
        r["template_version"] = os.path.basename(path)
        blank = common.missing_fields(r, headers, body)
        if blank:
            problems.append(f"{who}: blank {', '.join(blank)}")
        plan.append((r, headers, body))
    if problems:
        print(f"ERROR: {len(problems)} row(s) need fixing in {os.path.basename(DB)}; "
              f"nothing sent:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    # Safety checks (guard.py): anything that isn't clearly what you approved is held
    # for you instead of sent; too many broken emails stops the whole batch.
    checks = [guard.check(r, h, b, common.template_for(r, cfg), cfg) for r, h, b in plan]
    held = [(r, c) for (r, _, _), c in zip(plan, checks) if c]
    plan = [p for p, c in zip(plan, checks) if not c]
    stop = guard.too_many_problems(checks, cfg)
    if held:
        print(f"=== HELD for your review: {len(held)} email(s), not sent ===")
        for r, c in held:
            print(f"  {r.get('first_name')} {r.get('last_name')} <{r.get('contact_email')}> "
                  f"({r.get('company')})")
            for kind, reason in c:
                print(f"    - [{kind}] {reason}")
            common.print_check_line(r, indent="    ")
        print()
    if stop:
        print(f"SAFETY STOP: more than {guard.settings(cfg)['max_problem_percent']}% of "
              f"this batch has a problem, so nothing will be sent. Fix the problems above.")
        if args.send or args.test or args.spam_check:
            if args.send:
                common.record_send_run(cfg, sent=0, failed=[], held=len(held), stopped=True,
                                       problems=sorted({x for _, c in held for k, x in c
                                                        if k == guard.PROBLEM}))
            sys.exit(2)

    print(f"=== {len(plan)} email(s) staged ===\n")
    for r, headers, _ in plan:
        kind = (r.get("type") or "").strip() or "default"
        print(f"  [{kind}]")
        print(f"  TO:      {common.render(headers['TO'], r)}")
        print(f"  SUBJECT: {common.render(headers['SUBJECT'], r)}")
        common.print_check_line(r, indent="  ")
        print()

    if args.test:
        send_test(cfg, plan[:TEST_COUNT], workspace)
        return
    if args.spam_check:
        send_test(cfg, plan[:1], workspace, to=args.spam_check)
        state = common.load_state(cfg)
        state["spam_check_sent"] = datetime.date.today().isoformat()
        common.save_state(cfg, state)
        print("Then record what happened: `outreach health --spam-check inbox` or "
              "`outreach health --spam-check spam`.")
        return

    if not args.send:
        print("Dry-run only. Re-run with --test to email yourself, or --send to deliver.")
        return

    if not plan:
        print("Nothing passed the safety checks, so nothing to send.")
        if held:  # still move them to the held list, so they wait for your OK there
            common.backup(DB)
            common.hold_rows(cfg, [{**r, "held_reason": "; ".join(x for _, x in c)}
                                   for r, c in held])
            common.write_db(DB, fieldnames, [], ignore_extra=True)
            common.record_send_run(cfg, sent=0, failed=[], held=len(held), stopped=False)
        return
    if not args.yes:
        ok = input(f"Send {len(plan)} email(s)? [y/N] ").strip().lower()
        if ok != "y":
            print("Aborted. Nothing sent.")
            return

    try:
        common.validate_for_send(cfg)
    except ValueError as e:
        sys.exit(f"ERROR: {e}")
    em = cfg["email"]
    username = em["address"]
    host, port = em["smtp_host"], int(em["smtp_port"])
    password = common.get_password(em["keychain_service"])

    # Back up the canonical CSVs before mutating them so a crash is recoverable.
    common.backup(LOG)
    common.backup(DB)
    # Preflight: ensure the sent log has the current schema (adds sent_at to old
    # logs) BEFORE any message goes out, never after a send.
    common.migrate_sent_log(LOG)
    if held:  # move held emails out of the batch, into the held list for your OK
        common.hold_rows(cfg, [{**r, "held_reason": "; ".join(x for _, x in c)}
                               for r, c in held])
        rows = [r for r, _, _ in plan]
        common.write_db(DB, fieldnames, rows, ignore_extra=True)
        print(f"Moved {len(held)} held email(s) to {os.path.basename(cfg['files']['held'])}.")

    # Transactional send: archive each success IMMEDIATELY and rewrite the DB to
    # only the not-yet-sent rows, so a crash mid-batch can never (a) lose a row we
    # already sent or (b) leave a sent row staged for an accidental re-send.
    log_fields = common.SENT_LOG_FIELDS + [f for f in fieldnames
                                           if f not in common.SENT_LOG_FIELDS]
    remaining, failed, sent_n = list(rows), [], 0
    domain = username.rpartition("@")[2] or None
    for i, (r, headers, body) in enumerate(plan):
        if i:
            wait_between_emails(cfg)
        if already_sent(r, LOG):
            print(f"  skipped -> {r['contact_email']}: already in the sent log")
            remaining.remove(r)
            common.write_db(DB, fieldnames, remaining, ignore_extra=True)
            continue
        r["message_id"] = make_msgid(domain=domain)
        # A fresh Gmail connection per email: Gmail closes a connection left idle
        # during the 1-3 minute wait, which used to fail every email after it.
        delivered = False
        try:
            msg = build_message(r, headers, body, workspace)
            r["subject_sent"] = msg["Subject"]
            with common.smtp_session(host, port, username, password) as smtp:
                smtp.send_message(msg)
                delivered = True
        except Exception as e:
            if not delivered:
                print(f"  FAILED -> {r['contact_email']} ({r.get('company')}): {e}")
                failed.append(r)
                if common.is_sending_limit_error(e):
                    resume = common.record_warning(cfg, f"Gmail sending limit: {e}")
                    print(f"\n  Gmail says a sending limit was reached. Stopping now and "
                          f"pausing until {resume}; the rest stay in the batch.")
                    break
                if isinstance(e, smtplib.SMTPAuthenticationError):
                    print("\n  Gmail refused the login. Stopping now; the rest stay in "
                          "the batch. Check the App Password in Keychain.")
                    break
                continue
            # Gmail accepted it and only the goodbye failed: it IS sent, so log it
            # (retrying would email this person twice).
        # Sent successfully. A failure recording it is NOT a send failure: we
        # must stop immediately rather than keep sending into an inconsistent
        # state. The idempotency check above makes a later retry safe.
        try:
            common.archive(LOG, log_fields,
                           [{**r, "sent_at": common.utcnow_iso()}])
            remaining.remove(r)
            common.write_db(DB, fieldnames, remaining, ignore_extra=True)
        except Exception as e:
            sys.exit(f"\n  CRITICAL: sent to {r['contact_email']} but failed to "
                     f"record it: {e}\n  Aborting batch. Reconcile {os.path.basename(LOG)} "
                     f"and {os.path.basename(DB)} before resending.")
        print(f"  sent -> {r['contact_email']} ({r.get('company')})")
        sent_n += 1

    common.record_send_run(cfg, sent=sent_n, held=len(held), stopped=False,
                           failed=[f"{r['contact_email']} ({r.get('company')})" for r in failed],
                           not_sent=len(remaining))
    if sent_n:
        print(f"\nDone. {sent_n} sent and archived to {os.path.basename(LOG)}.")
        if remaining:
            print(f"{len(remaining)} not sent ({len(failed)} failed) and kept in "
                  f"{os.path.basename(DB)} for retry.")
        else:
            print(f"{os.path.basename(DB)} is now empty (header only), ready for the next batch.")
        print("Tip: watch for Mailer-Daemon bounces over the next few hours.")
    else:
        print("\nNothing sent successfully; database unchanged.")


if __name__ == "__main__":
    main()
