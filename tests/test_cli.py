"""Tests for config loading, transactional CSV ops, and the stage command (B/D/E)."""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common  # noqa: E402
import outreach_cli  # noqa: E402

FIELDS = ["contact_email", "contact_name", "company", "role", "subject", "note"]


def _row(company, email):
    return {"contact_email": email, "contact_name": "x", "company": company,
            "role": "r", "subject": "s", "note": "n"}


def test_write_db_is_atomic_roundtrip(tmp_path):
    p = tmp_path / "x.csv"
    common.write_db(str(p), FIELDS, [_row("A", "a@x.com")])
    fields, rows = common.read_rows(str(p))
    assert fields == FIELDS
    assert rows[0]["company"] == "A"
    # no leftover temp files in the dir
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".tmp_")]


def _cfg(tmp_path, batch_size=14):
    return {
        "files": {"queue": str(tmp_path / "q.csv"),
                  "database": str(tmp_path / "db.csv"),
                  "sent_log": str(tmp_path / "sent.csv")},
        "pacing": {"batch_size": batch_size},
    }


def test_stage_moves_dedups_and_respects_n(tmp_path):
    cfg = _cfg(tmp_path, batch_size=2)
    common.write_db(cfg["files"]["queue"], FIELDS,
                    [_row("A", "a@x.com"), _row("B", "b@x.com"), _row("C", "c@x.com")])
    common.write_db(cfg["files"]["database"], FIELDS, [])
    common.write_db(cfg["files"]["sent_log"], FIELDS, [_row("C", "c@x.com")])  # already sent

    outreach_cli.cmd_stage(argparse.Namespace(n=None), cfg)

    _, db_rows = common.read_rows(cfg["files"]["database"])
    _, q_rows = common.read_rows(cfg["files"]["queue"])
    assert {r["company"] for r in db_rows} == {"A", "B"}  # C skipped (already sent), capped at 2
    assert q_rows == []  # A/B moved out, C dropped (already sent) -> queue cleaned


class _FakeIMAP:
    """IMAP stand-in backed by an in-memory message store.

    Each message: {"box": mailbox, "uid": int, "h": {header: value}, "body": text}.
    Supports the searches backfill uses: TO, FROM, SUBJECT, HEADER REFERENCES/In-Reply-To.
    """
    def __init__(self, messages):
        self.msgs = messages
        self.box = None

    def select(self, mailbox, readonly=False):
        self.box = mailbox
        return ("OK", [b""])

    def _box(self):
        return sorted((m for m in self.msgs if m["box"] == self.box), key=lambda m: m["uid"])

    @staticmethod
    def _match(m, crit):
        if crit[0] in ("TO", "FROM", "SUBJECT"):
            return crit[1].strip('"').lower() in (m["h"].get(crit[0].title(), "")).lower()
        if crit[0] == "HEADER":
            field, val = crit[1], crit[2].strip('"')
            return val in (m["h"].get(field.title() if field != "In-Reply-To" else field, "") or "")
        return False

    def uid(self, op, *args):
        self.calls = getattr(self, "calls", 0) + 1
        if op == "search":
            crit = list(args[1:])  # drop charset (None)
            if crit[0] == "SINCE":                         # the bulk scan: all but ours
                crit = crit[2:]
            if crit[0] in ("ALL", "NOT"):
                ours = crit[2].strip('"').lower() if crit[0] == "NOT" else None
                hits = [str(m["uid"]).encode() for m in self._box()
                        if not ours or ours not in m["h"].get("From", "").lower()]
            else:
                hits = [str(m["uid"]).encode() for m in self._box()
                        if self._match(m, tuple(crit))]
            return ("OK", [b" ".join(hits) if hits else b""])
        if op == "fetch":
            spec, out = args[1], []
            ids = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            for uid in (int(u) for u in ids.split(",")):
                m = next((x for x in self._box() if x["uid"] == uid), None)
                if not m:
                    continue
                meta = f"{uid} (UID {uid} BODY[] {{1}}".encode()
                if "FIELDS" not in spec:                   # body (TEXT or whole message)
                    out += [(meta, m.get("body", "").encode()), b")"]
                    continue
                fields = re.search(r"FIELDS \(([^)]+)\)", spec).group(1).split()
                raw = "".join(f"{f}: {m['h'].get(f, '')}\r\n" for f in fields) + "\r\n"
                out += [(meta, raw.encode()), b")"]
            return ("OK", out or [None])
        return ("NO", [b""])

    def logout(self):
        pass


SENT = '"[Gmail]/Sent Mail"'
ALL = '"[Gmail]/All Mail"'


def _login_selecting(fake):
    def _login(*a, **k):
        fake.select(k.get("mailbox", ALL))
        return fake
    return _login


def test_backfill_thread_match_catches_different_address_reply(tmp_path, monkeypatch):
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company"],
                    [{"contact_email": "jobs@resim.ai", "company": "ReSim"},
                     {"contact_email": "hi@b.com", "company": "B"}])
    msgs = [
        # what we sent (Sent Mail), with the Gmail-assigned Message-ID
        {"box": SENT, "uid": 1, "h": {"To": "jobs@resim.ai", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<orig@gmail.com>"}},
        {"box": SENT, "uid": 2, "h": {"To": "hi@b.com", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<b@gmail.com>"}},
        # the reply comes from a DIFFERENT address, but references our message-id
        {"box": ALL, "uid": 10, "h": {"From": "matthew@resim.ai", "To": "me@x.com",
            "Date": "Fri, 05 Jun 2026 09:00:00 -0700", "In-Reply-To": "<orig@gmail.com>"}},
    ]
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(_FakeIMAP(msgs)))
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)},
           "email": {"address": "me@x.com", "keychain_service": "svc",
                     "imap_host": "imap.example.com"}}
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True), cfg)

    _, rows = common.read_rows(str(sent_log))
    by = {r["contact_email"]: r for r in rows}
    assert by["jobs@resim.ai"]["sent_at"] == "2026-06-03T17:00:00Z"
    assert by["jobs@resim.ai"]["last_reply"] == "2026-06-05T16:00:00Z"  # caught via thread
    assert by["hi@b.com"]["last_reply"] == ""                            # no reply


def test_backfill_does_not_overwrite_newer_existing_last_reply(tmp_path, monkeypatch):
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company", "last_reply"],
                    [{"contact_email": "a@x.com", "company": "A",
                      "last_reply": "2026-07-01T00:00:00Z"}])  # already a newer reply
    msgs = [
        {"box": SENT, "uid": 1, "h": {"To": "a@x.com", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<o@gmail.com>"}},
        {"box": ALL, "uid": 9, "h": {"From": "a@x.com", "To": "me@x.com",
            "Date": "Thu, 04 Jun 2026 09:00:00 -0700"}},  # OLDER than the existing value
    ]
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(_FakeIMAP(msgs)))
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)},
           "email": {"address": "me@x.com", "keychain_service": "svc",
                     "imap_host": "imap.example.com"}}
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True), cfg)

    _, rows = common.read_rows(str(sent_log))
    assert rows[0]["last_reply"] == "2026-07-01T00:00:00Z"  # kept the newer one


def test_backfill_from_match_still_catches_same_address_reply(tmp_path, monkeypatch):
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company"],
                    [{"contact_email": "a@x.com", "company": "A"}])
    msgs = [
        {"box": SENT, "uid": 1, "h": {"To": "a@x.com", "From": "me@x.com",
            "Date": "Wed, 03 Jun 2026 10:00:00 -0700", "Message-ID": "<o@gmail.com>"}},
        {"box": ALL, "uid": 9, "h": {"From": "a@x.com", "To": "me@x.com",
            "Date": "Thu, 04 Jun 2026 09:00:00 -0700"}},  # same-address reply, no headers
    ]
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(_FakeIMAP(msgs)))
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)},
           "email": {"address": "me@x.com", "keychain_service": "svc",
                     "imap_host": "imap.example.com"}}
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True), cfg)

    _, rows = common.read_rows(str(sent_log))
    assert rows[0]["last_reply"] == "2026-06-04T16:00:00Z"


def _backfill_cfg(tmp_path, sent_log):
    return {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                      "sent_log": str(sent_log), "send_state": str(tmp_path / "state.json")},
            "email": {"address": "me@x.com", "keychain_service": "svc",
                      "imap_host": "imap.example.com"}}


def test_backfill_bounce_and_auto_reply_are_not_replies(tmp_path, monkeypatch):
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company", "last_reply"],
                    [{"contact_email": "natalief@adobe.com", "company": "Adobe",
                      # an earlier run counted the bounce notice as a reply
                      "last_reply": "2026-11-02T17:20:00Z"},
                     {"contact_email": "ooo@b.com", "company": "B", "last_reply": ""},
                     {"contact_email": "real@c.com", "company": "C", "last_reply": ""}])
    msgs = [
        {"box": SENT, "uid": 1, "h": {"To": "natalief@adobe.com", "From": "me@x.com",
            "Date": "Mon, 02 Nov 2026 12:18:00 -0500", "Message-ID": "<n@gmail.com>"}},
        {"box": SENT, "uid": 2, "h": {"To": "ooo@b.com", "From": "me@x.com",
            "Date": "Mon, 02 Nov 2026 12:18:00 -0500", "Message-ID": "<o@gmail.com>"}},
        {"box": SENT, "uid": 3, "h": {"To": "real@c.com", "From": "me@x.com",
            "Date": "Mon, 02 Nov 2026 12:18:00 -0500", "Message-ID": "<r@gmail.com>"}},
        # Office 365 bounce: from postmaster at THEIR domain, in our thread
        {"box": ALL, "uid": 10, "h": {"From": "postmaster@adobe.com", "To": "me@x.com",
            "Subject": "Undeliverable: Coffee Chat Request",
            "Date": "Mon, 02 Nov 2026 12:20:00 -0500", "References": "<n@gmail.com>"},
         "body": "Your message to natalief@adobe.com couldn't be delivered."},
        # out-of-office from the person themself
        {"box": ALL, "uid": 11, "h": {"From": "ooo@b.com", "To": "me@x.com",
            "Subject": "Automatic reply: Coffee Chat Request", "Auto-Submitted": "auto-replied",
            "Date": "Mon, 02 Nov 2026 12:21:00 -0500", "In-Reply-To": "<o@gmail.com>"},
         "body": "I am out of the office until Nov 9."},
        # a real reply
        {"box": ALL, "uid": 12, "h": {"From": "real@c.com", "To": "me@x.com",
            "Subject": "Re: Coffee Chat Request",
            "Date": "Mon, 02 Nov 2026 14:00:00 -0500", "In-Reply-To": "<r@gmail.com>"},
         "body": "Hi Selena, happy to chat! Does Thursday work?"},
    ]
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(_FakeIMAP(msgs)))
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True),
                                    _backfill_cfg(tmp_path, sent_log))

    _, rows = common.read_rows(str(sent_log))
    by = {r["contact_email"]: r for r in rows}
    assert by["natalief@adobe.com"]["last_reply"] == ""        # bounce isn't a reply
    assert by["natalief@adobe.com"]["bounced_at"]              # ...it's a bounce
    assert by["ooo@b.com"]["last_reply"] == ""                 # out-of-office ignored
    assert not by["ooo@b.com"].get("bounced_at")
    assert by["real@c.com"]["last_reply"] == "2026-11-02T19:00:00Z"


def test_classify_messages():
    import read_replies
    c = read_replies.classify
    assert c({"From": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"}) == "bounce"
    assert c({"From": "postmaster@adobe.com", "Subject": "Undeliverable: hi"}) == "bounce"
    assert c({"From": "x@y.com", "Subject": "Delivery Status Notification (Failure)"}) == "bounce"
    assert c({"From": "x@y.com", "Subject": "hi"},
             "Address not found: your message wasn't delivered") == "bounce"
    assert c({"From": "a@b.com", "Subject": "Out of Office: Coffee Chat"}) == "auto"
    assert c({"From": "a@b.com", "Subject": "Re: hi", "Auto-Submitted": "auto-replied"}) == "auto"
    assert c({"From": "a@b.com", "Subject": "Re: hi"},
             "Thanks for reaching out. I'm currently out of the office until Monday.") == "auto"
    assert c({"From": "a@b.com", "Subject": "Re: Coffee Chat Request"},
             "Sure! That role does not exist on my team, but happy to chat.") == "reply"
    assert c({"From": "a@b.com", "Subject": "Re: hi", "Auto-Submitted": "no"}) == "reply"


def test_setup_scaffolds_config_at_requested_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OUTREACH_SECRET_BACKEND", "env")  # avoid macOS `security` call
    cfg_path = tmp_path / "sub" / "config.toml"
    cfg = common.load_config(str(cfg_path))  # file doesn't exist yet
    outreach_cli.cmd_setup(argparse.Namespace(), cfg)
    assert cfg_path.exists()  # created at the requested path, not CWD
    assert "address" in cfg_path.read_text()


def test_followup_uses_sent_at_cadence(tmp_path, capsys):
    import datetime
    sent_log = tmp_path / "sent.csv"
    fields = common.SENT_LOG_FIELDS
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    common.write_db(str(sent_log), fields, [
        {"contact_email": "old@x.com", "company": "OldCo", "sent_at": old},
        {"contact_email": "new@x.com", "company": "NewCo", "sent_at": recent},
        {"contact_email": "blank@x.com", "company": "BlankCo", "sent_at": ""},
        {"contact_email": "replied@x.com", "company": "RepCo", "sent_at": old,
         "last_reply": recent},  # old AND replied -> excluded, not a candidate
    ])
    cfg = {"files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                     "sent_log": str(sent_log)}, "pacing": {"followup_after_days": 6}}
    outreach_cli.cmd_followup(argparse.Namespace(), cfg)
    out = capsys.readouterr().out
    assert "old@x.com" in out               # 10 days, no reply -> due
    assert "new@x.com" not in out           # 1 day -> not due
    assert "replied@x.com" not in out       # old but replied -> excluded
    assert "1 contact(s) excluded" in out   # the replied one
    assert "no sent_at" in out              # blank -> reported as unknown


def test_stage_skips_already_staged(tmp_path):
    cfg = _cfg(tmp_path)
    common.write_db(cfg["files"]["queue"], FIELDS, [_row("A", "a@x.com")])
    common.write_db(cfg["files"]["database"], FIELDS, [_row("A", "a@x.com")])  # already staged
    common.write_db(cfg["files"]["sent_log"], FIELDS, [])

    outreach_cli.cmd_stage(argparse.Namespace(n=None), cfg)

    _, db_rows = common.read_rows(cfg["files"]["database"])
    assert len(db_rows) == 1  # not duplicated


def test_setup_handles_template_versions_and_resume_link(tmp_path, capsys):
    (tmp_path / "t1.txt").write_text("TO: {{contact_email}}\nSUBJECT: Hi\n\nBody {{resume_link}}\n")
    (tmp_path / "t2.txt").write_text("TO: {{contact_email}}\nSUBJECT: Hi\n\nBody\n")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[email]\naddress = "me@gmail.com"\n'
                        '[templates]\ncoffee_chat = ["t1.txt", "t2.txt"]\n'
                        '[blanks]\nresume_link = "https://drive.example/cv"\n')
    outreach_cli.cmd_setup(argparse.Namespace(), common.load_config(str(cfg_path)))
    out = capsys.readouterr().out
    assert "Template [coffee_chat]: OK t1.txt" in out and "OK t2.txt" in out
    assert "Resume link: OK https://drive.example/cv" in out
    assert "Resume file" not in out  # no template attaches a file


def test_one_bounce_does_not_pause_two_do(tmp_path):
    sent_log = tmp_path / "sent.csv"
    rows = [{"contact_email": f"p{i}@x.com", "company": "X"} for i in range(19)]
    common.write_db(str(sent_log), ["contact_email", "company"], rows)
    cfg = _backfill_cfg(tmp_path, sent_log)
    outreach_cli.apply_bounces(cfg, str(sent_log), {"p0@x.com"})        # 1 of 19 = 5%
    assert not common.load_state(cfg).get("paused_until")
    outreach_cli.apply_bounces(cfg, str(sent_log), {"p0@x.com", "p1@x.com"})
    assert common.load_state(cfg).get("paused_until")                    # 2 of 19


def test_bounces_at_blocked_companies_do_not_pause(tmp_path):
    sent_log = tmp_path / "sent.csv"
    rows = [{"contact_email": f"p{i}@x.com", "company": "X"} for i in range(19)]
    rows += [{"contact_email": "a@adobe.com", "company": "Adobe"},
             {"contact_email": "b@adobe.com", "company": "Adobe"}]
    common.write_db(str(sent_log), ["contact_email", "company"], rows)
    cfg = _backfill_cfg(tmp_path, sent_log)
    (tmp_path / "covered.csv").write_text("company,domains,note\nAdobe,adobe.com,blocked\n")
    cfg["files"]["covered_companies"] = str(tmp_path / "covered.csv")
    outreach_cli.apply_bounces(cfg, str(sent_log), {"a@adobe.com", "b@adobe.com"})
    assert not common.load_state(cfg).get("paused_until")
    outreach_cli.apply_bounces(cfg, str(sent_log), {"p0@x.com", "p1@x.com"})
    assert common.load_state(cfg).get("paused_until")


def test_backfill_checks_many_people_in_a_few_gmail_requests(tmp_path, monkeypatch):
    # 150 people, all logged with send date + Message-ID: no per-person searches
    # (the old way did ~4 per person and Gmail cut the connection off).
    sent_log = tmp_path / "sent.csv"
    people = [{"contact_email": f"p{i}@x.com", "company": "X", "message_id": f"<m{i}@gmail.com>",
               "sent_at": "2026-11-02T17:00:00Z", "last_reply": ""} for i in range(150)]
    common.write_db(str(sent_log), list(people[0]), people)
    msgs = [{"box": ALL, "uid": 5, "h": {"From": "p7@x.com", "To": "me@x.com",
                                         "Subject": "Re: hi",
                                         "Date": "Tue, 03 Nov 2026 09:00:00 -0500"},
             "body": "Happy to help!"},
            {"box": ALL, "uid": 6, "h": {"From": "other@y.com", "To": "me@x.com",
                                         "Subject": "Re: hi", "References": "<m9@gmail.com>",
                                         "Date": "Wed, 04 Nov 2026 09:00:00 -0500"},
             "body": "Answering from my personal address."},
            {"box": ALL, "uid": 7, "h": {"From": "me@x.com", "To": "p8@x.com",
                                         "In-Reply-To": "<m8@gmail.com>",
                                         "Date": "Wed, 04 Nov 2026 10:00:00 -0500"}}]
    fake = _FakeIMAP(msgs)
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login_selecting(fake))
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True),
                                    _backfill_cfg(tmp_path, sent_log))
    _, rows = common.read_rows(str(sent_log))
    replied = {r["contact_email"]: r["last_reply"] for r in rows if r["last_reply"]}
    assert replied == {"p7@x.com": "2026-11-03T14:00:00Z",
                       "p9@x.com": "2026-11-04T14:00:00Z"}    # our own message (p8) ignored
    assert fake.calls <= 6                                    # 1 search + 1 fetch + 2 bodies


def test_backfill_retries_once_when_gmail_drops_the_connection(tmp_path, monkeypatch):
    import imaplib
    sent_log = tmp_path / "sent.csv"
    common.write_db(str(sent_log), ["contact_email", "company", "message_id", "sent_at"],
                    [{"contact_email": "a@x.com", "company": "A", "message_id": "<a@gmail.com>",
                      "sent_at": "2026-11-02T17:00:00Z"}])
    msgs = [{"box": ALL, "uid": 9, "h": {"From": "a@x.com", "Subject": "Re: hi",
                                         "Date": "Tue, 03 Nov 2026 09:00:00 -0500"},
             "body": "Sure!"}]
    logins = []

    class _Dropping(_FakeIMAP):
        def uid(self, op, *args):
            if len(logins) == 1:
                raise imaplib.IMAP4.abort("command: UID => socket error: EOF")
            return super().uid(op, *args)

    def _login(*a, **k):
        logins.append(1)
        fake = _Dropping(msgs)
        fake.select(ALL)
        return fake

    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "imap_login", _login)
    outreach_cli.cmd_backfill_dates(argparse.Namespace(apply=True),
                                    _backfill_cfg(tmp_path, sent_log))
    _, rows = common.read_rows(str(sent_log))
    assert len(logins) == 2 and rows[0]["last_reply"] == "2026-11-03T14:00:00Z"
