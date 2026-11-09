#!/usr/bin/env python3
"""Read-only IMAP reader: who from sent_log.csv has replied (and who bounced).

Outbound sending is handled by send.py over SMTP. This script is the inbound
counterpart: it logs into the SAME Gmail account over IMAP using the SAME app
password already stored in the macOS Keychain (Gmail app passwords work for both
SMTP and IMAP), and reports which contacted companies have written back. Use it to
EXCLUDE responders before sending a follow-up batch.

It is strictly READ-ONLY: it never deletes, moves, marks-as-read, or sends
anything. It only runs IMAP SEARCH/FETCH and prints a report. Nothing leaves the
machine.

Shared plumbing (Keychain, IMAP login, header decoding) lives in common.py.

Reuses the existing pipeline files/credential so behavior matches send.py:
  - sent_log.csv            the contact list to check (contact_email, company, ...)
  - config.toml             your Gmail address + IMAP host ([email])
  - Keychain service 'mailmerge-gmail'   the Gmail app password

Usage:
  python3 read_replies.py            # human report: replied / bounced / silent
  python3 read_replies.py --emails   # print ONLY the email addresses that replied
                                     # (one per line, for piping into a filter)

Requires IMAP enabled in Gmail (Settings -> Forwarding and POP/IMAP -> Enable IMAP;
on by default for most accounts).
"""
import argparse
import csv
import datetime
import email
import os
import re
import sys
from email.utils import getaddresses, parsedate_to_datetime

import common

SENT_MAIL = '"[Gmail]/Sent Mail"'  # Gmail's Sent folder; holds the real send dates

APPROX_NOTE = (
    "NOTE: this is the FAST APPROXIMATE fallback. It only finds replies sent FROM\n"
    "the exact address you emailed, so it MISSES replies from a different address\n"
    "(a person replying from a personal account) and cannot reliably tell an\n"
    "auto-acknowledgement from a real human reply. For authoritative reply\n"
    "detection use the Gmail connector; cross-check anything important by hand."
)


def load_contacts(log_path):
    """Return [(email_lower, company)] from sent_log.csv, de-duplicated by address."""
    if not os.path.exists(log_path):
        sys.exit(f"ERROR: {log_path} not found. Nothing to check.")
    seen, contacts = set(), []
    with open(log_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            addr = (row.get("contact_email") or "").strip().lower()
            if addr and addr not in seen:
                seen.add(addr)
                contacts.append((addr, (row.get("company") or "").strip()))
    return contacts


def headers_of(imap, uid, fields):
    """Fetch several header fields for a message UID in ONE read-only round-trip."""
    field_str = " ".join(fields)
    typ, data = imap.uid("fetch", uid, f"(BODY.PEEK[HEADER.FIELDS ({field_str})])")
    if typ != "OK" or not data or not data[0]:
        return {}
    raw = data[0][1].decode("utf-8", "replace")
    msg = email.message_from_string(raw)
    return {f: common.decode_header_str(msg.get(f, "")) for f in fields}


def _parse_date(raw):
    """Parse an RFC-2822 Date header into a UTC 'YYYY-MM-DDTHH:MM:SSZ' string."""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_of(imap, uid):
    return _parse_date(headers_of(imap, uid, ["Date"]).get("Date", ""))


# A message in a contact's thread (or from their address) is only a REPLY if a person
# wrote it. Bounce notices ("couldn't be delivered") and automatic replies ("out of
# office") are not replies: bounces mean a bad address, auto-replies mean nothing.
BOUNCE_SENDERS = {"mailer-daemon", "postmaster", "mail-daemon", "mailerdaemon",
                  "mail.delivery.subsystem", "maildelivery"}
BOUNCE_SUBJECTS = ("undeliverable", "undelivered", "delivery status notification",
                   "mail delivery failed", "delivery has failed", "returned mail",
                   "failure notice", "delivery failure", "message not delivered",
                   "message blocked", "rejected:")
BOUNCE_TEXT = ("couldn't be delivered", "could not be delivered", "wasn't found at",
               "address not found", "user unknown", "unknown to address",
               "recipient address rejected", "mailbox unavailable", "no such user",
               "does not exist", "550 5.1.1", "550-5.1.1", "5.1.10")
AUTO_SUBJECTS = ("automatic reply", "auto reply", "auto-reply", "autoreply", "auto:",
                 "out of office", "out of the office", "away from the office",
                 "on vacation", "on leave")
AUTO_TEXT = ("i am out of the office", "i'm out of the office", "i am currently out of",
             "i'm currently out of", "i am away", "i'm away", "out of office until",
             "this is an automatic reply", "this is an automated response")
CLASSIFY_FIELDS = ["From", "Date", "Subject", "Auto-Submitted", "X-Autoreply",
                   "X-Autorespond", "Precedence", "Content-Type"]


def classify(headers, body=""):
    """'bounce', 'auto' or 'reply' for one incoming message (headers + start of body)."""
    sender = [a.lower() for _, a in getaddresses([headers.get("From", "")]) if a]
    local = sender[0].partition("@")[0] if sender else ""
    subject = " ".join((headers.get("Subject") or "").lower().split())
    ctype = (headers.get("Content-Type") or "").lower()
    text = " ".join((body or "").lower().replace("\u2019", "'").split())
    if (local in BOUNCE_SENDERS or subject.startswith(BOUNCE_SUBJECTS)
            or "report-type=delivery-status" in ctype.replace(" ", "")
            or any(t in text[:4000] for t in BOUNCE_TEXT) and not subject.startswith("re:")):
        return "bounce"
    if ((headers.get("Auto-Submitted") or "no").strip().lower() not in ("", "no")
            or headers.get("X-Autoreply") or headers.get("X-Autorespond")
            or (headers.get("Precedence") or "").strip().lower() in ("auto_reply", "bulk", "junk")
            or subject.startswith(AUTO_SUBJECTS)
            or any(t in text[:600] for t in AUTO_TEXT)):
        return "auto"
    return "reply"


def body_start(imap, uid, size=4000):
    """The first `size` bytes of a message body, as lowercase-able text (read-only)."""
    typ, data = imap.uid("fetch", uid, f"(BODY.PEEK[TEXT]<0.{size}>)")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return ""
    raw = data[0][1].decode("utf-8", "replace")
    if "=\r\n" in raw or "=3D" in raw:              # quoted-printable soft breaks
        raw = raw.replace("=\r\n", "").replace("=3D", "=")
    return raw


def message_kind(imap, uid):
    """(kind, date) for one message: kind is 'bounce', 'auto' or 'reply'."""
    h = headers_of(imap, uid, CLASSIFY_FIELDS)
    return classify(h, body_start(imap, uid)), _parse_date(h.get("Date", ""))


SCAN_FIELDS = CLASSIFY_FIELDS + ["In-Reply-To", "References"]
SCAN_CHUNK = 500  # messages per bulk header FETCH


def _bulk_headers(imap, uids, fields):
    """{uid: headers} for many messages, SCAN_CHUNK per FETCH (read-only)."""
    out = {}
    for i in range(0, len(uids), SCAN_CHUNK):
        chunk = b",".join(uids[i:i + SCAN_CHUNK]).decode()
        typ, data = imap.uid("fetch", chunk,
                             f"(UID BODY.PEEK[HEADER.FIELDS ({' '.join(fields)})])")
        if typ != "OK":
            raise ConnectionError(f"Gmail header fetch failed: {typ}")
        for part in data or []:
            if not isinstance(part, tuple):
                continue                                     # the b")" separators
            m = re.search(rb"UID (\d+)", part[0])
            if not m:
                continue
            msg = email.message_from_string(part[1].decode("utf-8", "replace"))
            out[m.group(1)] = {f: common.decode_header_str(msg.get(f, "")) for f in fields}
    return out


def scan_replies(imap, contacts, msgids, our_addr, since=None):
    """Replies and bounces for all `contacts` in a few round-trips (select All Mail first).

    One SEARCH for everything since `since` (a date) not from us, one bulk header
    FETCH, then local matching: a message counts for a contact if it is FROM their
    address or references one of the Message-IDs we sent them (`msgids`: id -> address),
    so a reply from a different address is caught too. Only matched messages get their
    body read, to tell real replies from bounces and automatic replies.

    Returns (replies {addr: latest date}, bounced {addrs}, thread_only {addrs}).
    The old way was ~4 searches per contact, which Gmail cut off at ~150 contacts.
    """
    our = (our_addr or "").lower()
    crit = ["NOT", "FROM", f'"{our_addr}"'] if our else ["ALL"]
    if since:
        crit = ["SINCE", since.strftime("%d-%b-%Y")] + crit
    typ, data = imap.uid("search", None, *crit)
    if typ != "OK":
        raise ConnectionError(f"Gmail search failed: {typ}")
    uids = (data[0] or b"").split() if data else []
    contacts = set(contacts)
    replies, bounced, by_from = {}, set(), set()
    for uid, h in _bulk_headers(imap, uids, SCAN_FIELDS).items():
        senders = {a.lower() for _, a in getaddresses([h.get("From", "")]) if a}
        if our in senders:
            continue                                         # our own message
        refs = set(re.findall(r"<[^<>\s]+>", f"{h.get('In-Reply-To', '')} "
                                             f"{h.get('References', '')}"))
        from_hits = senders & contacts
        thread_hits = {msgids[r] for r in refs if r in msgids}
        if not (from_hits or thread_hits):
            continue
        kind = classify(h, body_start(imap, uid))
        date = _parse_date(h.get("Date", ""))
        for addr in from_hits | thread_hits:
            if kind == "bounce" and addr in thread_hits:
                bounced.add(addr)
            elif kind == "reply" and date and date > replies.get(addr, ""):
                replies[addr] = date
                if addr in from_hits:
                    by_from.add(addr)
    return replies, bounced, set(replies) - by_from


def sent_meta_for(imap, addr):
    """For mail we sent TO `addr` (select Gmail Sent Mail first): return
    (earliest_send_date, [message_ids]).

    earliest_send_date backfills sent_at; the message_ids let us find replies by
    thread (In-Reply-To/References) regardless of which address replied.
    """
    typ, data = imap.uid("search", None, "TO", f'"{addr}"')
    if typ != "OK" or not data or not data[0]:
        return None, []
    uids = data[0].split()
    dates, msgids = [], []
    for u in uids:
        h = headers_of(imap, u, ["Date", "Message-ID"])  # both in one fetch
        d = _parse_date(h.get("Date", ""))
        if d:
            dates.append(d)
        mid = " ".join((h.get("Message-ID") or "").split())  # collapse folding
        if mid:
            msgids.append(mid)
    return (min(dates) if dates else None), msgids  # earliest BY DATE, not UID order


def last_reply_for(imap, addr):
    """LATEST date `addr` wrote to us, FROM-matched (select All Mail first). Bounce
    notices and automatic replies don't count (see classify).

    Catches a reply only if it was sent from the same address we emailed. Returns the
    max parsed Date across all real replies (not whatever UID happens to be last).
    """
    typ, data = imap.uid("search", None, "FROM", f'"{addr}"')
    if typ != "OK" or not data or not data[0]:
        return None
    dates = [d for u in data[0].split()
             for kind, d in [message_kind(imap, u)] if kind == "reply" and d]
    return max(dates) if dates else None


def thread_reply_for(imap, msgids, our_addr):
    """(latest_reply, bounced) for the threads that reference one of `msgids`
    (select All Mail first).

    latest_reply is the newest REAL reply from someone other than us; bounced is True
    if a bounce notice for our email is in the thread. This is the address-independent
    path: it matches on In-Reply-To/References, so a reply sent from a different
    address than the one we emailed is still caught. Our own follow-ups in the thread
    are excluded by sender; automatic replies are ignored.
    """
    our = (our_addr or "").lower()
    latest, bounced, seen = None, False, set()
    for mid in msgids:
        uids = set()
        for field in ("REFERENCES", "In-Reply-To"):
            typ, data = imap.uid("search", None, "HEADER", field, f'"{mid}"')
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        for uid in uids:
            if uid in seen:
                continue
            seen.add(uid)
            h = headers_of(imap, uid, ["From"])
            from_addrs = [a.lower() for _, a in getaddresses([h.get("From", "")]) if a]
            if our and our in from_addrs:
                continue  # our own message in the thread, not a reply
            kind, d = message_kind(imap, uid)
            if kind == "bounce":
                bounced = True
            elif kind == "reply" and d and (latest is None or d > latest):
                latest = d
    return latest, bounced


def find_replies(imap, contacts):
    """For each contact, IMAP-search All Mail for mail FROM them. Returns dict
    email -> (company, date, subject, count) for those who replied.

    One SEARCH per contact (inherent to FROM-matching) plus ONE combined header
    fetch for the newest hit (Subject + Date together, not two round-trips)."""
    replied = {}
    for addr, company in contacts:
        typ, data = imap.uid("search", None, "FROM", f'"{addr}"')
        if typ != "OK" or not data or not data[0]:
            continue
        uids = data[0].split()
        if not uids:
            continue
        h = headers_of(imap, uids[-1], ["Subject", "Date"])
        replied[addr] = (company, h.get("Date", ""), h.get("Subject", ""), len(uids))
    return replied


def find_bounces(imap, contacts):
    """Find bounce notices (from mailer-daemon or postmaster, or with an
    "Undeliverable"-style subject) and match our recipient addresses in them.
    Returns a set of bounced addresses (best-effort)."""
    bounced = set()
    by_addr = {a for a, _ in contacts if a}
    uids = set()
    for query in (("FROM", '"mailer-daemon"'), ("FROM", '"postmaster"'),
                  ("SUBJECT", '"Undeliverable"'), ("SUBJECT", '"Delivery Status Notification"')):
        typ, data = imap.uid("search", None, *query)
        if typ == "OK" and data and data[0]:
            uids.update(data[0].split())
    for uid in uids:
        # Pull the whole bounce so we can scan for which address failed.
        typ, msgdata = imap.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not msgdata or not msgdata[0]:
            continue
        text = msgdata[0][1].decode("utf-8", "replace").lower()
        for addr in by_addr:
            if addr in text:
                bounced.add(addr)
    return bounced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="config path (default ./config.toml or $OUTREACH_CONFIG)")
    ap.add_argument("--emails", action="store_true",
                    help="print only the addresses that replied, one per line")
    args = ap.parse_args()

    cfg = common.load_config(common.find_config(args.config))
    contacts = load_contacts(cfg["files"]["sent_log"])
    em = cfg["email"]
    username = em.get("address")
    if not username:
        sys.exit("ERROR: set [email].address in config.toml.")
    password = common.get_password(em["keychain_service"])

    imap = common.imap_login(username, password, readonly=True, host=em["imap_host"])
    try:
        replied = find_replies(imap, contacts)
        bounced = find_bounces(imap, contacts) if not args.emails else set()
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    if args.emails:
        for addr in replied:
            print(addr)
        return

    total = len(contacts)
    print(f"Checked {total} contacted companies against {username} (All Mail).")
    print(APPROX_NOTE + "\n")

    if replied:
        print(f"=== REPLIED ({len(replied)}) -- EXCLUDE these from follow-ups ===")
        for addr, (company, date, subject, n) in sorted(
                replied.items(), key=lambda kv: kv[1][0].lower()):
            more = f"  (+{n - 1} more)" if n > 1 else ""
            print(f"  {company} <{addr}>{more}")
            print(f"      last: {date}")
            print(f"      subj: {subject}\n")
    else:
        print("=== REPLIED (0) ===\n  No replies found yet.\n")

    if bounced:
        print(f"=== BOUNCED / undeliverable ({len(bounced)}) -- bad address, drop ===")
        for addr in sorted(bounced):
            company = next((c for a, c in contacts if a == addr), "")
            print(f"  {company} <{addr}>")
        print()

    silent = total - len(replied) - len(bounced)
    print(f"=== SILENT (~{silent}) -- candidates for a follow-up ===")
    print("Note: 'silent' = no reply and no bounce detected. Bounce matching is")
    print("best-effort (scans Mailer-Daemon notices). Verify a sample by hand.")


if __name__ == "__main__":
    main()
