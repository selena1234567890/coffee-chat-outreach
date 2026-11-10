"""Tests for safer sending: the volume ramp, warning signs and pauses, follow-ups,
template versions, spacing between emails, shared blanks (resume link), and
`outreach health`.

Hermetic: temp workspace, fake SMTP, no network or account.
"""
import argparse
import contextlib
import datetime
import json
import smtplib
import sys

import pytest

import common
import outreach_cli
import send

FIELDS = ["type", "contact_email", "first_name", "company", "title", "subject",
          "sent_at", "last_reply", "message_id", "subject_sent", "bounced_at"]
TODAY = datetime.date(2026, 11, 4)  # a Wednesday


def _utc(day):
    return f"{day.isoformat()}T12:00:00Z"


def _person(email, **kw):
    row = {"contact_email": email, "first_name": "Pat", "company": email.split("@")[1],
           "title": "Data Scientist", "subject": "Hello"}
    return {**row, **kw}


# --- ramp ------------------------------------------------------------------------

def _ramp_cfg(tmp_path):
    return {"files": {"send_state": str(tmp_path / "state.json")},
            "ramp": dict(common.DEFAULT_RAMP)}


def test_ramp_starts_at_13_before_any_send(tmp_path):
    assert common.ramp_status(_ramp_cfg(tmp_path), today=TODAY)["per_day"] == 13


@pytest.mark.parametrize("days_since_first_send, per_day", [
    (0, 13), (2, 13), (3, 18), (9, 18), (10, 25), (16, 25), (17, 35), (100, 35)])
def test_ramp_steps_up_over_time(tmp_path, days_since_first_send, per_day):
    first = TODAY - datetime.timedelta(days=days_since_first_send)
    status = common.ramp_status(_ramp_cfg(tmp_path), first_send=first, today=TODAY)
    assert status["per_day"] == per_day


def test_warning_pauses_then_steps_down(tmp_path):
    cfg = _ramp_cfg(tmp_path)
    first = TODAY - datetime.timedelta(days=12)                  # at step 3 (25/day)
    assert common.ramp_status(cfg, first_send=first, today=TODAY)["per_day"] == 25
    resume = common.record_warning(cfg, "bounce rate 5%", today=TODAY)
    assert resume == TODAY + datetime.timedelta(days=2)
    assert common.ramp_status(cfg, today=TODAY + datetime.timedelta(days=1))["per_day"] == 0
    after = common.ramp_status(cfg, today=resume)
    assert after["per_day"] == 18                                # one step down
    # climbs again only after a full clean step from the resume date
    assert common.ramp_status(cfg, today=resume + datetime.timedelta(days=6))["per_day"] == 18
    assert common.ramp_status(cfg, today=resume + datetime.timedelta(days=7))["per_day"] == 25
    assert json.loads((tmp_path / "state.json").read_text())["warnings"][0]["reason"] == \
        "bounce rate 5%"


def test_ramp_steps_are_adjustable(tmp_path):
    cfg = _ramp_cfg(tmp_path)
    cfg["ramp"]["steps"] = [[5, 1], [9, 0]]
    first = TODAY - datetime.timedelta(days=1)
    assert common.ramp_status(cfg, first_send=first, today=TODAY)["per_day"] == 9


@pytest.mark.parametrize("total, coffee, referral", [(13, 6, 7), (18, 8, 10), (35, 15, 20)])
def test_daily_split_keeps_the_ratio_and_adds_up(total, coffee, referral):
    split = common.split_daily({"coffee_chat": 6, "referral": 8}, total)
    assert split == {"coffee_chat": coffee, "referral": referral}


def test_sending_limit_errors_are_recognised():
    err = smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded.")
    assert common.is_sending_limit_error(err)
    assert not common.is_sending_limit_error(smtplib.SMTPRecipientsRefused({}))


# --- staging with the ramp + follow-ups -------------------------------------------

def _stage_cfg(tmp_path, **over):
    cfg = {
        "files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                  "sent_log": str(tmp_path / "sent.csv"),
                  "send_state": str(tmp_path / "state.json"),
                  "targets": str(tmp_path / "none.csv"),
                  "do_not_contact": str(tmp_path / "none.csv")},
        "pacing": {"batch_size": 99},
        "company_limits": {**common.DEFAULT_COMPANY_LIMITS, "unknown": 99},
        "daily": dict(common.DEFAULT_DAILY),
        "ramp": dict(common.DEFAULT_RAMP),
        "followup": dict(common.DEFAULT_FOLLOWUP),
        "priority": {"schools": [], "tags": [], "surname_lists": {}},
    }
    cfg.update(over)
    return cfg


def _stage(cfg, queue, sent=()):
    common.write_db(cfg["files"]["queue"], FIELDS, queue)
    common.write_db(cfg["files"]["database"], FIELDS, [])
    common.write_db(cfg["files"]["sent_log"], FIELDS, list(sent))
    outreach_cli.cmd_stage(argparse.Namespace(n=None), cfg)
    return common.read_rows(cfg["files"]["database"])[1]


def test_stage_follows_todays_ramp_limit(tmp_path):
    seniors = [_person(f"s{i}@a.com", title="Engineering Manager") for i in range(20)]
    juniors = [_person(f"j{i}@a.com") for i in range(20)]
    staged = _stage(_stage_cfg(tmp_path), seniors + juniors)
    kinds = [r["type"] for r in staged]
    assert len(staged) == 13                       # first step
    assert kinds.count("coffee_chat") == 6 and kinds.count("referral") == 7


def test_stage_does_nothing_while_paused(tmp_path, capsys):
    cfg = _stage_cfg(tmp_path)
    common.record_warning(cfg, "spam check landed in spam")
    assert _stage(cfg, [_person("a@a.com")]) == []
    assert "Paused after a warning sign" in capsys.readouterr().out


def test_business_days_skip_weekends():
    friday = datetime.date(2026, 10, 30)
    assert outreach_cli.business_days_between(friday, datetime.date(2026, 11, 2)) == 1  # Mon
    assert outreach_cli.business_days_between(friday, datetime.date(2026, 11, 4)) == 3  # Wed


def _sent(email, days_ago, **kw):
    return _person(email, type="referral", sent_at=_utc(TODAY - datetime.timedelta(days=days_ago)),
                   message_id=f"<{email}.1@gmail.com>", subject_sent="Referral for DS intern",
                   **kw)


def test_due_followups(tmp_path):
    cfg = _stage_cfg(tmp_path)
    sent = [_sent("due@a.com", 5),                                  # Fri -> Wed: 3 business days
            _sent("early@a.com", 2),                                # too soon
            _sent("replied@a.com", 7, last_reply="2026-11-01T10:00:00Z"),
            _sent("bounced@a.com", 7, bounced_at="2026-10-29"),
            _sent("done@a.com", 9),
            _person("done@a.com", type="followup", sent_at=_utc(TODAY))]  # already followed up
    due = outreach_cli.due_followups(sent, [], cfg, today=TODAY)
    assert [r["contact_email"] for r in due] == ["due@a.com"]
    assert due[0]["in_reply_to"] == "<due@a.com.1@gmail.com>"
    assert due[0]["original_subject"] == "Referral for DS intern"


def test_followup_wait_is_adjustable(tmp_path):
    cfg = _stage_cfg(tmp_path, followup={"enabled": True, "after_business_days": 1})
    assert len(outreach_cli.due_followups([_sent("a@a.com", 2)], [], cfg, today=TODAY)) == 1


def test_followups_are_staged_first_on_top_of_the_new_emails(tmp_path):
    cfg = _stage_cfg(tmp_path)
    sent = [_sent(f"old{i}@a.com", 30) for i in range(3)]
    queue = ([_person(f"sr{i}@a.com", title="Engineering Manager") for i in range(25)]
             + [_person(f"jr{i}@a.com") for i in range(25)])
    staged = _stage(cfg, queue, sent=sent)
    assert [r["type"] for r in staged[:3]] == ["followup"] * 3
    # the first send was 30 days ago, so the ramp is at 35/day: 35 NEW emails, and the
    # 3 follow-ups go on top (your choice, 2026-11-10)
    assert sum(r["type"] != "followup" for r in staged) == 35
    assert len(staged) == 38


def test_followups_sent_today_dont_use_up_the_new_emails(tmp_path):
    cfg = _stage_cfg(tmp_path)
    old = [_sent(f"old{i}@a.com", 30) for i in range(3)]
    now = _utc(datetime.date.today())        # "sent today" means the real today
    fu_today = [_person(f"old{i}@a.com", type="followup", sent_at=now) for i in range(3)]
    new_today = [_person(f"n{i}@b.com", type="referral", sent_at=now) for i in range(5)]
    queue = [_person(f"jr{i}@a.com") for i in range(50)]
    staged = _stage(cfg, queue, sent=old + fu_today + new_today)
    assert sum(r["type"] != "followup" for r in staged) == 30   # 35 - the 5 new sent today


# --- sending ---------------------------------------------------------------------

class _FakeSMTP:
    def __init__(self, fail_after=None):
        self.sent, self.fail_after = [], fail_after

    def send_message(self, msg):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded.")
        self.sent.append(msg)


def _workspace(tmp_path, rows, sent=(), templates=None, blanks=""):
    t = tmp_path / "templates"
    t.mkdir(exist_ok=True)
    (t / "referral_1.txt").write_text("TO: {{contact_email}}\nSUBJECT: Hi {{company}}\n\n"
                                      "V1 {{first_name}} {{resume_link}}\n")
    (t / "referral_2.txt").write_text("TO: {{contact_email}}\nSUBJECT: Hello {{company}}\n\n"
                                      "V2 {{first_name}} {{resume_link}}\n")
    (t / "followup.txt").write_text("TO: {{contact_email}}\nSUBJECT: Re: {{original_subject}}"
                                    "\n\nJust following up, {{first_name}}.\n")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[email]\naddress = "me@gmail.com"\nsmtp_port = 587\n'
        '[files]\ndatabase = "db.csv"\nsent_log = "sent.csv"\n'
        'send_state = "state.json"\n'
        '[templates]\n'
        + (templates or 'referral = ["templates/referral_1.txt", "templates/referral_2.txt"]\n'
                        'followup = "templates/followup.txt"\n')
        + (blanks or '[blanks]\nresume_link = "https://drive.example/cv"\n'))
    fields = list(dict.fromkeys(FIELDS + [k for r in rows for k in r]))
    common.write_db(str(tmp_path / "db.csv"), fields, rows)
    if sent:
        common.write_db(str(tmp_path / "sent.csv"),
                        list(dict.fromkeys(FIELDS + [k for r in sent for k in r])), list(sent))
    return cfg_path


class _FakeImap:
    def logout(self):
        pass


def _send(monkeypatch, cfg_path, *flags, fake=None, replies=None, imap_error=None):
    fake = fake or _FakeSMTP()
    waits = []
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    import read_replies  # never touch real Gmail: the follow-up reply check is faked

    def login(*a, **k):
        if imap_error:
            raise imap_error
        return _FakeImap()

    monkeypatch.setattr(common, "imap_login", login)
    monkeypatch.setattr(read_replies, "last_reply_for",
                        lambda imap, addr: (replies or {}).get(addr))
    monkeypatch.setattr(read_replies, "thread_reply_for", lambda imap, mids, our: (None, False))

    @contextlib.contextmanager
    def session(*a, **k):
        yield fake

    monkeypatch.setattr(common, "smtp_session", session)
    monkeypatch.setattr(send.time, "sleep", waits.append)
    monkeypatch.setattr(sys, "argv", ["send.py", "--config", str(cfg_path), *flags])
    send.main()
    return fake, waits


def test_send_logs_message_id_subject_and_template_version(monkeypatch, tmp_path):
    rows = [_person(f"p{i}@corp.com", type="referral") for i in range(6)]
    fake, _ = _send(monkeypatch, _workspace(tmp_path, rows), "--send", "--yes")
    _, log = common.read_rows(str(tmp_path / "sent.csv"))
    assert all(r["message_id"].startswith("<") and r["message_id"].endswith("@gmail.com>")
               for r in log)
    assert {r["template_version"] for r in log} == {"referral_1.txt", "referral_2.txt"}
    for r, msg in zip(log, fake.sent):
        assert r["subject_sent"] == msg["Subject"]
        assert msg["Message-ID"] == r["message_id"]
        assert "https://drive.example/cv" in msg.get_body().get_content()  # shared blank


def test_same_person_always_gets_the_same_version(tmp_path):
    cfg = {"templates": {"referral": ["a.txt", "b.txt", "c.txt"]}}
    row = {"type": "referral", "contact_email": "Jane@Corp.com"}
    picks = {common.template_for(dict(row), cfg) for _ in range(5)}
    assert len(picks) == 1
    assert common.template_for({**row, "contact_email": " jane@corp.com "}, cfg) in picks


def test_emails_are_spaced_out(monkeypatch, tmp_path):
    rows = [_person(f"p{i}@corp.com", type="referral") for i in range(3)]
    _, waits = _send(monkeypatch, _workspace(tmp_path, rows), "--send", "--yes")
    assert len(waits) == 2 and all(60 <= w <= 180 for w in waits)  # between, not before


def test_followup_goes_in_the_same_thread(monkeypatch, tmp_path):
    first = _sent("a@corp.com", 6)
    fu = {**first, "type": "followup", "in_reply_to": first["message_id"],
          "original_subject": first["subject_sent"], "sent_at": "", "message_id": "",
          "subject_sent": ""}
    fake, _ = _send(monkeypatch, _workspace(tmp_path, [fu], sent=[first]), "--send", "--yes")
    msg = fake.sent[0]
    assert msg["In-Reply-To"] == first["message_id"] == msg["References"]
    assert msg["Subject"] == "Re: Referral for DS intern"


def test_second_followup_is_blocked(monkeypatch, tmp_path, capsys):
    first = _sent("a@corp.com", 6)
    earlier_fu = _person("a@corp.com", type="followup", sent_at=_utc(TODAY))
    fu = {**first, "type": "followup", "in_reply_to": first["message_id"],
          "original_subject": "x", "sent_at": "", "message_id": "", "subject_sent": ""}
    fake, _ = _send(monkeypatch, _workspace(tmp_path, [fu], sent=[first, earlier_fu]),
                    "--send", "--yes")
    assert fake.sent == []


def test_gmail_limit_stops_and_pauses(monkeypatch, tmp_path, capsys):
    rows = [_person(f"p{i}@corp.com", type="referral") for i in range(4)]
    cfg_path = _workspace(tmp_path, rows)
    fake, _ = _send(monkeypatch, cfg_path, "--send", "--yes", fake=_FakeSMTP(fail_after=2))
    assert len(fake.sent) == 2
    _, left = common.read_rows(str(tmp_path / "db.csv"))
    assert len(left) == 2                                   # unsent stay for later
    state = json.loads((tmp_path / "state.json").read_text())
    assert "Gmail sending limit" in state["warnings"][0]["reason"]
    out = capsys.readouterr().out
    assert "Done. 2 sent" in out and "2 not sent" in out


def test_dropped_connection_only_fails_that_email(monkeypatch, tmp_path, capsys):
    # Gmail closes a connection left idle between emails; each email gets its own.
    rows = [_person(f"p{i}@corp.com", type="referral") for i in range(4)]
    cfg_path = _workspace(tmp_path, rows)
    fake, sessions = _FakeSMTP(), []
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")

    @contextlib.contextmanager
    def session(*a, **k):
        sessions.append(1)
        if len(sessions) == 2:
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        yield fake

    monkeypatch.setattr(common, "smtp_session", session)
    monkeypatch.setattr(send.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["send.py", "--config", str(cfg_path), "--send", "--yes"])
    send.main()
    assert len(sessions) == 4 and len(fake.sent) == 3
    _, left = common.read_rows(str(tmp_path / "db.csv"))
    assert [r["contact_email"] for r in left] == ["p1@corp.com"]   # kept for retry


def test_error_after_gmail_accepted_still_counts_as_sent(monkeypatch, tmp_path):
    rows = [_person("p@corp.com", type="referral")]
    cfg_path = _workspace(tmp_path, rows)
    fake = _FakeSMTP()
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")

    @contextlib.contextmanager
    def session(*a, **k):
        yield fake
        raise smtplib.SMTPServerDisconnected("closed on goodbye")

    monkeypatch.setattr(common, "smtp_session", session)
    monkeypatch.setattr(sys, "argv", ["send.py", "--config", str(cfg_path), "--send", "--yes"])
    send.main()
    _, log = common.read_rows(str(tmp_path / "sent.csv"))
    _, left = common.read_rows(str(tmp_path / "db.csv"))
    assert [r["contact_email"] for r in log] == ["p@corp.com"] and left == []


def test_unfilled_resume_link_blocks_the_send(monkeypatch, tmp_path, capsys):
    cfg_path = _workspace(tmp_path, [_person("p@corp.com", type="referral")],
                          blanks='[blanks]\nresume_link = "<link to your resume>"\n')
    with pytest.raises(SystemExit):
        _send(monkeypatch, cfg_path, "--send", "--yes")
    assert "blank resume_link" in capsys.readouterr().out


def test_spam_check_goes_unchanged_to_the_given_address(monkeypatch, tmp_path):
    cfg_path = _workspace(tmp_path, [_person("p@corp.com", type="referral")])
    fake, _ = _send(monkeypatch, cfg_path, "--spam-check", "me@outlook.com")
    msg = fake.sent[0]
    assert msg["To"] == "me@outlook.com" and not msg["Subject"].startswith("[TEST")
    assert not (tmp_path / "sent.csv").exists()            # nothing logged


# --- health ----------------------------------------------------------------------

def test_mark_bounces_and_rate(tmp_path):
    log = tmp_path / "sent.csv"
    common.write_db(str(log), FIELDS, [_sent(f"p{i}@a.com", 3) for i in range(10)])
    assert outreach_cli.mark_bounces(str(log), {"p1@a.com"}, today=TODAY) == 1
    assert outreach_cli.mark_bounces(str(log), {"p1@a.com"}, today=TODAY) == 0  # not twice
    rows = common.read_rows(str(log))[1]
    assert outreach_cli.bounce_rate(rows, 50) == 0.1


def test_health_spam_result_spam_pauses(tmp_path, capsys):
    cfg = _stage_cfg(tmp_path)
    outreach_cli.cmd_health(argparse.Namespace(spam_check="spam", no_imap=True), cfg)
    out = capsys.readouterr().out
    assert "landed in SPAM" in out and "PAUSED until" in out


def test_health_spam_result_inbox_is_fine(tmp_path, capsys):
    cfg = _stage_cfg(tmp_path)
    outreach_cli.cmd_health(argparse.Namespace(spam_check="inbox", no_imap=True), cfg)
    out = capsys.readouterr().out
    assert "inbox. Good." in out and "13 emails/day" in out


def test_spam_check_not_due_again_while_one_awaits_your_answer(tmp_path, capsys):
    cfg = _stage_cfg(tmp_path)
    today = datetime.date.today()
    common.save_state(cfg, {"last_spam_check": {"date": (today - datetime.timedelta(days=10))
                                                .isoformat(), "result": "inbox"},
                            "spam_check_sent": (today - datetime.timedelta(days=1)).isoformat()})
    outreach_cli.cmd_health(argparse.Namespace(spam_check=None, no_imap=True), cfg)
    out = capsys.readouterr().out
    assert "Spam check: inbox (10 day(s) ago)" in out and "due, run one" not in out


def test_resume_link_shown_as_short_clickable_text(monkeypatch, tmp_path):
    rows = [_person("pat@corp.com", type="referral")]
    blanks = ('[blanks]\nresume_link = "https://drive.example/cv?usp=drive_link"\n'
              'resume_link_text = "Selena Su Resume"\n')
    fake, _ = _send(monkeypatch, _workspace(tmp_path, rows, blanks=blanks), "--send", "--yes")
    msg = fake.sent[0]
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    page = msg.get_body(preferencelist=("html",)).get_content()
    assert "https://drive.example/cv?usp=drive_link" in plain          # full link kept
    assert '<a href="https://drive.example/cv?usp=drive_link">Selena Su Resume</a>' in page
    assert page.count("drive.example") == 1                            # only inside the link


def test_no_link_text_means_plain_email_only(monkeypatch, tmp_path):
    fake, _ = _send(monkeypatch, _workspace(tmp_path, [_person("pat@corp.com",
                                                               type="referral")]),
                    "--send", "--yes")
    assert fake.sent[0].get_body(preferencelist=("html",)) is None


def test_looking_at_a_future_date_does_not_move_the_ramp(tmp_path):
    cfg = {"files": {"send_state": str(tmp_path / "state.json")}}
    today = datetime.date.today()
    common.save_state(cfg, {"step": 0, "step_started": today.isoformat()})
    future = common.ramp_status(cfg, first_send=today, today=today + datetime.timedelta(days=30),
                                save=False)
    assert future["step"] > 0
    assert common.load_state(cfg)["step"] == 0


# --- never two sends at once / never the same person twice ------------------------

def test_second_send_run_is_blocked_while_one_runs(monkeypatch, tmp_path):
    import fcntl
    cfg_path = _workspace(tmp_path, [_person("p1@corp.com", type="referral")])
    with open(tmp_path / send.LOCK_NAME, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)   # another run is sending
        with pytest.raises(SystemExit, match="another send is already running"):
            _send(monkeypatch, cfg_path, "--send", "--yes")
    assert not (tmp_path / "sent.csv").exists()


def test_email_sent_by_another_run_mid_batch_is_skipped(monkeypatch, tmp_path):
    rows = [_person(f"p{i}@corp.com", type="referral") for i in range(3)]
    cfg_path = _workspace(tmp_path, rows)
    log = str(tmp_path / "sent.csv")

    def other_run_sends_p1(_seconds):  # during the wait, another run emails p1
        common.archive(log, common.SENT_LOG_FIELDS, [{**rows[1], "sent_at": _utc(TODAY)}])

    monkeypatch.setattr(send, "wait_between_emails", other_run_sends_p1)
    fake, _ = _send(monkeypatch, cfg_path, "--send", "--yes")
    assert [m["To"] for m in fake.sent] == ["p0@corp.com", "p2@corp.com"]
    assert common.read_rows(str(tmp_path / "db.csv"))[1] == []


# --- the day's total is met --------------------------------------------------------

def test_unused_type_slots_fill_the_day(tmp_path):
    juniors = [_person(f"j{i}@a.com") for i in range(20)]   # referrals only, no coffee chats
    staged = _stage(_stage_cfg(tmp_path), juniors)
    assert len(staged) == 13 and {r["type"] for r in staged} == {"referral"}


def test_slow_gmail_does_not_hold_up_the_day(monkeypatch, tmp_path):
    import threading
    stop = threading.Event()
    monkeypatch.setattr(outreach_cli, "cmd_backfill_dates", lambda *a: stop.wait(5))
    assert outreach_cli.check_replies_in_background({}, wait=0.05) is False
    stop.set()


def test_followups_wait_when_the_reply_check_did_not_finish(tmp_path, capsys):
    cfg = _stage_cfg(tmp_path)
    old = _sent("old@a.com", 10)
    common.write_db(cfg["files"]["queue"], FIELDS, [_person("new@a.com")])
    common.write_db(cfg["files"]["database"], FIELDS, [])
    common.write_db(cfg["files"]["sent_log"], list(dict.fromkeys(FIELDS + list(old))), [old])
    outreach_cli.cmd_stage(argparse.Namespace(n=None, no_followups=True), cfg)
    staged = common.read_rows(cfg["files"]["database"])[1]
    assert [r["contact_email"] for r in staged] == ["new@a.com"]
    assert "Follow-ups skipped today" in capsys.readouterr().out


def test_no_followup_when_either_copy_of_a_double_send_got_a_reply(tmp_path):
    cfg = _stage_cfg(tmp_path)
    first = _sent("twice@a.com", 10)
    second = {**_sent("twice@a.com", 10), "message_id": "<twice.2@gmail.com>",
              "last_reply": _utc(TODAY)}
    assert outreach_cli.due_followups([first, second], [], cfg, today=TODAY) == []
    assert len(outreach_cli.due_followups([first], [], cfg, today=TODAY)) == 1


def _followup_of(first):
    return {**first, "type": "followup", "in_reply_to": first["message_id"],
            "original_subject": first["subject_sent"], "sent_at": "", "message_id": "",
            "subject_sent": ""}


def test_followup_not_sent_to_someone_who_already_replied(monkeypatch, tmp_path):
    first = _sent("a@corp.com", 6)
    cfg = _workspace(tmp_path, [_followup_of(first)], sent=[first])
    fake, _ = _send(monkeypatch, cfg, "--send", "--yes",
                    replies={"a@corp.com": "2026-11-08T09:00:00Z"})
    assert fake.sent == []
    _, log = common.read_rows(str(tmp_path / "sent.csv"))
    assert log[0]["last_reply"] == "2026-11-08T09:00:00Z"   # never due again
    assert common.read_rows(str(tmp_path / "db.csv"))[1] == []


def test_followup_waits_when_gmail_cannot_be_read(monkeypatch, tmp_path, capsys):
    first = _sent("a@corp.com", 6)
    cfg = _workspace(tmp_path, [_followup_of(first)], sent=[first])
    fake, _ = _send(monkeypatch, cfg, "--send", "--yes", imap_error=OSError("EOF"))
    assert fake.sent == []
    assert "waits for the next day" in capsys.readouterr().out
    _, log = common.read_rows(str(tmp_path / "sent.csv"))
    assert not log[0]["last_reply"]                          # due again next day


def test_daily_stages_followups_even_if_reply_check_fails(monkeypatch, tmp_path, capsys):
    cfg = _stage_cfg(tmp_path)
    common.write_db(cfg["files"]["sent_log"], FIELDS, [_sent("due@a.com", 5)])
    common.write_db(cfg["files"]["queue"], FIELDS, [])
    monkeypatch.setattr(outreach_cli, "check_replies_in_background", lambda cfg: False)
    monkeypatch.setattr(outreach_cli, "cmd_health", lambda a, c: None)
    monkeypatch.setattr(outreach_cli, "cmd_preview", lambda a, c: None)
    monkeypatch.setattr(outreach_cli, "due_followups",
                        lambda s, d, c, today=None: [{**s[0], "type": "followup",
                                                      "in_reply_to": s[0]["message_id"]}])
    outreach_cli.cmd_daily(argparse.Namespace(no_imap=False), cfg)
    out = capsys.readouterr().out
    assert "Staged 1 follow-up(s)" in out
    assert "checked for a reply on its own" in out


def test_no_sending_on_skip_days(monkeypatch, tmp_path, capsys):
    today = datetime.date.today().strftime("%a")
    cfg = _workspace(tmp_path, [_person("p@corp.com", type="referral")])
    cfg.write_text(cfg.read_text() + f'[sending]\nskip_days = ["{today}"]\n')
    fake, _ = _send(monkeypatch, cfg, "--send", "--yes")
    assert fake.sent == []
    assert "No emails on" in capsys.readouterr().out
    assert len(common.read_rows(str(tmp_path / "db.csv"))[1]) == 1  # batch waits


def test_is_skip_day():
    cfg = {"sending": {"skip_days": ["Saturday"]}}
    assert common.is_skip_day(cfg, datetime.date(2026, 11, 14))       # a Saturday
    assert not common.is_skip_day(cfg, datetime.date(2026, 11, 10))   # a Tuesday
    assert not common.is_skip_day({"sending": {}}, datetime.date(2026, 11, 14))


def test_missing_attachment_blocks_the_batch(monkeypatch, tmp_path, capsys):
    cfg = _workspace(tmp_path, [_person("p@corp.com", type="referral")])
    t = tmp_path / "templates" / "referral_1.txt"
    t.write_text("TO: {{contact_email}}\nSUBJECT: Hi\nATTACHMENT: nope/resume.pdf\n\nV1\n")
    (tmp_path / "templates" / "referral_2.txt").write_text(t.read_text())
    with pytest.raises(SystemExit):
        _send(monkeypatch, cfg, "--send", "--yes")
    assert "attachment not found" in capsys.readouterr().out
