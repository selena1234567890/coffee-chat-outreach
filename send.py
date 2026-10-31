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
import mimetypes
import os
import sys
from email.message import EmailMessage

import common

TEST_COUNT = 2  # how many emails --test sends to yourself


def build_message(row, headers, body, base_dir):
    """Render an email; ATTACHMENT is resolved relative to `base_dir` (the workspace)."""
    msg = EmailMessage()
    msg["To"] = common.render(headers["TO"], row)
    msg["Subject"] = common.render(headers["SUBJECT"], row)
    msg["From"] = common.render(headers.get("FROM", ""), row)
    msg.set_content(common.render(body, row))
    attach = headers.get("ATTACHMENT")
    if attach:
        path = common.resolve(common.render(attach, row), base_dir)
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))
    return msg


def send_test(cfg, plan, workspace):
    """Send rendered emails to the sender's OWN address, marked [TEST].

    Nothing is logged and the database is untouched, so the real send still
    happens later. Use it to see exactly what a recipient will see.
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
            msg.replace_header("To", me)
            msg.replace_header("Subject", f"[TEST to {real_to}] {msg['Subject']}")
            smtp.send_message(msg)
            print(f"  test sent -> {me} (would go to {real_to})")
    print("\nCheck your inbox. Nothing was logged; run --send when it looks right.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="config path (default ./config.toml or $OUTREACH_CONFIG)")
    ap.add_argument("--send", action="store_true", help="actually send (default: dry-run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--test", action="store_true",
                    help=f"send the first {TEST_COUNT} emails to your own address only")
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
    seen, fresh = set(), []
    n_sent = n_dup = n_blank = n_dnc = 0
    for r in rows:
        em = common.normalize_email(r.get("contact_email"))
        if not em:
            n_blank += 1
        elif common.on_do_not_contact(r, cfg):
            n_dnc += 1
        elif em in already:
            n_sent += 1
        elif em in seen:
            n_dup += 1
        else:
            seen.add(em)
            fresh.append(r)
    for label, n in (("already in the sent log", n_sent),
                     ("duplicated within this batch", n_dup),
                     ("missing a contact_email", n_blank),
                     ("on the do-not-contact list", n_dnc)):
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

    print(f"=== {len(plan)} email(s) staged ===\n")
    for r, headers, _ in plan:
        kind = (r.get("type") or "").strip() or "default"
        print(f"  [{kind}]")
        print(f"  TO:      {common.render(headers['TO'], r)}")
        print(f"  SUBJECT: {common.render(headers['SUBJECT'], r)}\n")

    if args.test:
        send_test(cfg, plan[:TEST_COUNT], workspace)
        return

    if not args.send:
        print("Dry-run only. Re-run with --test to email yourself, or --send to deliver.")
        return

    if not args.yes:
        ok = input(f"Send {len(rows)} email(s)? [y/N] ").strip().lower()
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

    # Transactional send: archive each success IMMEDIATELY and rewrite the DB to
    # only the not-yet-sent rows, so a crash mid-batch can never (a) lose a row we
    # already sent or (b) leave a sent row staged for an accidental re-send.
    log_fields = common.SENT_LOG_FIELDS + [f for f in fieldnames
                                           if f not in common.SENT_LOG_FIELDS]
    remaining, failed = list(rows), []
    with common.smtp_session(host, port, username, password) as smtp:
        for r, headers, body in plan:
            try:
                smtp.send_message(build_message(r, headers, body, workspace))
            except Exception as e:
                print(f"  FAILED -> {r['contact_email']} ({r.get('company')}): {e}")
                failed.append(r)
                continue
            # Sent successfully. A failure recording it is NOT a send failure: we
            # must stop immediately rather than keep sending into an inconsistent
            # state. The idempotency check above makes a later retry safe.
            try:
                common.archive(LOG, log_fields,
                               [{**r, "sent_at": common.utcnow_iso()}])
                remaining.remove(r)
                common.write_db(DB, fieldnames, remaining)
            except Exception as e:
                sys.exit(f"\n  CRITICAL: sent to {r['contact_email']} but failed to "
                         f"record it: {e}\n  Aborting batch. Reconcile {os.path.basename(LOG)} "
                         f"and {os.path.basename(DB)} before resending.")
            print(f"  sent -> {r['contact_email']} ({r.get('company')})")

    sent_n = len(rows) - len(failed)
    if sent_n:
        print(f"\nDone. {sent_n} sent and archived to {os.path.basename(LOG)}.")
        if failed:
            print(f"{len(failed)} failed and kept in {os.path.basename(DB)} for retry.")
        else:
            print(f"{os.path.basename(DB)} is now empty (header only), ready for the next batch.")
        print("Tip: watch for Mailer-Daemon bounces over the next few hours.")
    else:
        print("\nNothing sent successfully; database unchanged.")


if __name__ == "__main__":
    main()
