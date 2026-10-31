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


def test_followups_are_staged_first_and_count_toward_today(tmp_path):
    cfg = _stage_cfg(tmp_path)
    sent = [_sent(f"old{i}@a.com", 30) for i in range(3)]
    queue = ([_person(f"sr{i}@a.com", title="Engineering Manager") for i in range(25)]
             + [_person(f"jr{i}@a.com") for i in range(25)])
    staged = _stage(cfg, queue, sent=sent)
    assert [r["type"] for r in staged[:3]] == ["followup"] * 3
    # the first send was 30 days ago, so the ramp is at 35/day; 3 of them are follow-ups
    assert len(staged) == 35
    assert sum(r["type"] != "followup" for r in staged) == 32


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


def _send(monkeypatch, cfg_path, *flags, fake=None):
    fake = fake or _FakeSMTP()
    waits = []
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")

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
