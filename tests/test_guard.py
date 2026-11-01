"""Tests for the safety checks (guard.py) that let emails go out without you reading
each one: approved templates, approved personal-line sentences, names, links, the
held list, and the safety stop.

Hermetic: temp workspace, fake SMTP, no network or account.
"""
import argparse

import pytest
from test_sending import _person, _send, _workspace

import common
import guard
import outreach_cli

pytestmark = pytest.mark.guard

EMORY = ("I noticed you have also studied in Emory and was genuinely impressed by your "
         "work as {a} {title} at {company}.")


def _cfg(tmp_path):
    t = tmp_path / "templates"
    t.mkdir(exist_ok=True)
    (t / "referral_1.txt").write_text("TO: {{contact_email}}\nSUBJECT: Hi\n\n"
                                      "Hi {{first_name}}, {{personal_line}} {{resume_link}}\n")
    (tmp_path / "config.toml").write_text(
        '[email]\naddress = "me@gmail.com"\n[templates]\nreferral = "templates/referral_1.txt"\n'
        '[blanks]\nresume_link = "https://drive.example/cv"\n')
    return common.load_config(str(tmp_path / "config.toml"))


def _row(**kw):
    row = {"type": "referral", "contact_email": "pat.lee@google.com", "first_name": "Pat",
           "last_name": "Lee", "company": "Google", "company_domain": "google.com",
           "email_status": "verified", "resume_link": "https://drive.example/cv",
           "personal_line": EMORY.format(a="a", title="Data Scientist", company="Google")}
    return {**row, **kw}


def _check(cfg, row):
    path = common.template_for(row, cfg)
    headers, body = common.load_template(path)
    return guard.check(row, headers, body, path, cfg)


# --- templates ----------------------------------------------------------------

def test_unapproved_or_changed_template_is_a_problem(tmp_path):
    cfg = _cfg(tmp_path)
    assert any("never approved" in r for _, r in _check(cfg, _row()))
    guard.approve_templates(cfg)
    assert _check(cfg, _row()) == []
    (tmp_path / "templates" / "referral_1.txt").write_text("TO: {{contact_email}}\n"
                                                           "SUBJECT: Hi\n\nSomething new\n")
    assert [(guard.PROBLEM, "template referral_1.txt changed since you approved it")] \
        == _check(cfg, _row())


# --- personal lines -----------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    c = _cfg(tmp_path)
    guard.approve_templates(c)
    return c


def test_approved_sentences_pass(cfg):
    plain = ("I came across your profile and was genuinely impressed by your work as an "
             "Applied Scientist at Google.")
    assert _check(cfg, _row()) == []
    assert _check(cfg, _row(personal_line=plain)) == []


@pytest.mark.parametrize("line, why", [
    (EMORY.format(a="a", title="Data Scientist", company="Meta"), "their company is"),
    (EMORY.format(a="a", title="Applied Scientist", company="Google"), "should be 'an"),
    (EMORY.format(a="a", title="Data Scientist II Ads Ranking Search Platform Team Lead",
                  company="Google"), "too long"),
    ("I loved your recent talk about ranking at Google!", "approved sentences"),
])
def test_other_lines_are_held_for_review(cfg, line, why):
    reasons = _check(cfg, _row(personal_line=line))
    assert reasons and all(kind == guard.REVIEW for kind, _ in reasons)
    assert any(why in r for _, r in reasons)


def test_approved_row_skips_person_checks(cfg):
    row = _row(personal_line="I loved your recent talk about ranking!", approved="yes")
    assert _check(cfg, row) == []


# --- names, links, values -----------------------------------------------------

@pytest.mark.parametrize("changes, why", [
    ({"first_name": "Yulin", "last_name": "G", "contact_email": "gai@google.com"},
     "doesn't match the email"),
    ({"first_name": "Kailang (Robert)"}, "doesn't look like a first name"),
    ({"first_name": "PAT"}, "all capitals"),
    ({"first_name": "Dr", "contact_email": "dr.lee@google.com"}, "a title"),
    ({"contact_email": "careers@google.com"}, "shared inbox"),
    ({"contact_email": "pat.lee@meta.com"}, "not google.com"),
])
def test_name_and_email_problems_are_held(cfg, changes, why):
    assert any(why in r for _, r in _check(cfg, _row(**changes)))


def test_name_matching_email_by_last_name_passes(cfg):
    assert _check(cfg, _row(first_name="Zackary", last_name="Eccher",
                            contact_email="zeccher@google.com")) == []


@pytest.mark.parametrize("changes, why", [
    ({"resume_link": "https://evil.example/x"}, "resume link"),
    ({"first_name": "Pat", "personal_line": EMORY.format(
        a="a", title="Data Scientist", company="Google") + " https://x.io"}, "contains a link"),
    ({"personal_line": "I came across <insert fact here>."}, "leftover marker"),
])
def test_bad_values_are_problems(cfg, changes, why):
    reasons = _check(cfg, _row(**changes))
    assert any(kind == guard.PROBLEM and why in r for kind, r in reasons)


def test_too_many_problems():
    ok, bad = [], [(guard.PROBLEM, "x")]
    assert not guard.too_many_problems([ok] * 4 + [bad], {})      # 20%: not over
    assert guard.too_many_problems([ok] * 3 + [bad], {})          # 25%: stop
    assert not guard.too_many_problems([ok, [(guard.REVIEW, "y")]], {})


# --- in send.py: hold, send the rest, safety stop ------------------------------

def _send_ws(tmp_path, rows):
    path = _workspace(tmp_path, rows)
    guard.approve_templates(common.load_config(str(path)))
    return path


def test_send_holds_flagged_emails_and_sends_the_rest(monkeypatch, tmp_path):
    rows = [_person(f"pat.lee{i}@corp.com", type="referral") for i in range(5)]
    rows.append(_person("careers@corp.com", type="referral"))
    fake, _ = _send(monkeypatch, _send_ws(tmp_path, rows), "--send", "--yes")
    assert len(fake.sent) == 5
    assert "careers@corp.com" not in [m["To"] for m in fake.sent]
    _, held = common.read_rows(str(tmp_path / "data" / "held.csv"))
    assert [r["contact_email"] for r in held] == ["careers@corp.com"]
    assert "shared inbox" in held[0]["held_reason"]
    assert common.read_rows(str(tmp_path / "db.csv"))[1] == []      # batch is empty


def test_safety_stop_sends_nothing(monkeypatch, tmp_path, capsys):
    rows = [_person(f"pat.lee{i}@corp.com", type="referral") for i in range(4)]
    path = _send_ws(tmp_path, rows)
    (tmp_path / "templates" / "referral_1.txt").write_text(
        "TO: {{contact_email}}\nSUBJECT: changed\n\nchanged {{first_name}}\n")
    with pytest.raises(SystemExit) as e:
        _send(monkeypatch, path, "--send", "--yes")
    assert e.value.code == 2
    assert "SAFETY STOP" in capsys.readouterr().out
    assert len(common.read_rows(str(tmp_path / "db.csv"))[1]) == 4  # nothing moved


def test_dry_run_shows_held_but_moves_nothing(monkeypatch, tmp_path, capsys):
    rows = [_person("pat.lee@corp.com", type="referral"),
            _person("careers@corp.com", type="referral")]
    _send(monkeypatch, _send_ws(tmp_path, rows))
    out = capsys.readouterr().out
    assert "HELD for your review: 1" in out and "=== 1 email(s) staged ===" in out
    assert not (tmp_path / "data" / "held.csv").exists()


# --- outreach held / approve / spot-check --------------------------------------

def test_approve_moves_held_back_to_front_of_queue(cfg, tmp_path, capsys):
    cfg["files"]["queue"] = str(tmp_path / "q.csv")
    common.write_db(cfg["files"]["queue"], common.LEAD_FIELDS,
                    [{"contact_email": "old@x.com", "first_name": "Old"}])
    common.hold_rows(cfg, [_row(held_reason="needs OK"),
                           _row(contact_email="sam.lee@google.com", first_name="Sam")])
    outreach_cli.cmd_held(argparse.Namespace(), cfg)
    assert "needs OK" in capsys.readouterr().out
    outreach_cli.cmd_approve(argparse.Namespace(emails=["Pat.Lee@google.com"], all=False,
                                                drop=False), cfg)
    q = common.read_rows(cfg["files"]["queue"])[1]
    assert [r["contact_email"] for r in q] == ["pat.lee@google.com", "old@x.com"]
    assert q[0]["approved"] == "yes"
    outreach_cli.cmd_approve(argparse.Namespace(emails=["sam.lee@google.com"], all=False,
                                                drop=True), cfg)
    assert common.read_rows(cfg["files"]["held"])[1] == []
    assert len(common.read_rows(cfg["files"]["queue"])[1]) == 2       # dropped, not queued


def test_approve_unknown_email_fails(cfg):
    with pytest.raises(SystemExit, match="not in the held list"):
        outreach_cli.cmd_approve(argparse.Namespace(emails=["z@z.com"], all=False,
                                                    drop=False), cfg)


def test_spot_check_rebuilds_sent_email(cfg, tmp_path, capsys):
    cfg["files"]["sent_log"] = str(tmp_path / "sent.csv")
    sent = {**_row(), "template_version": "referral_1.txt",
            "sent_at": "2026-11-02T15:00:00Z", "subject_sent": "Hi"}
    common.write_db(cfg["files"]["sent_log"],
                    list(dict.fromkeys(common.SENT_LOG_FIELDS + list(sent))), [sent])
    outreach_cli.cmd_spot_check(argparse.Namespace(n=2), cfg)
    out = capsys.readouterr().out
    assert "Spot check: 1 of 1 sent on 2026-11-02" in out
    assert "Hi Pat, I noticed you have also studied in Emory" in out
