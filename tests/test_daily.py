"""Tests for `outreach daily`, the morning routine Agent 2 runs: health, staging and
preview in order, and nothing sent.

Hermetic: a temp workspace with a real config.toml, offline (--no-imap).
"""
import argparse

import common
import outreach_cli

QUEUE_FIELDS = ["contact_email", "first_name", "last_name", "company", "company_size",
                "title", "start_date", "subject", "email_status"]


def _workspace(tmp_path, n_people=3):
    (tmp_path / "templates").mkdir()
    for kind in ("coffee_chat", "referral", "followup"):
        (tmp_path / "templates" / f"{kind}.txt").write_text(
            "TO: {{contact_email}}\nSUBJECT: {{subject}}\n\nHi {{first_name}}\n")
    (tmp_path / "config.toml").write_text(
        '[email]\naddress = "me@gmail.com"\n'
        '[templates]\ncoffee_chat = "templates/coffee_chat.txt"\n'
        'referral = "templates/referral.txt"\nfollowup = "templates/followup.txt"\n')
    people = [{"contact_email": f"p{i}@google.com", "first_name": f"Pat{i}", "last_name": "Lee",
               "company": "Google", "company_size": "180000", "title": "Data Scientist",
               "start_date": "2026-01", "subject": "Hello", "email_status": "verified"}
              for i in range(n_people)]
    common.write_db(str(tmp_path / "outreach_queue.csv"), QUEUE_FIELDS, people)
    return common.load_config(str(tmp_path / "config.toml"))


def test_daily_stages_and_previews_without_sending(tmp_path, capfd):
    cfg = _workspace(tmp_path)
    outreach_cli.cmd_daily(argparse.Namespace(no_imap=True), cfg)
    out = capfd.readouterr().out
    order = [out.index(h) for h in ("=== 1. Replies", "=== 2. Health", "=== 3. Today's batch",
                                    "=== 4. Preview")]
    assert order == sorted(order)                       # steps run in this order
    assert "Ramp: step 1 of 4, 13 emails/day" in out
    assert "=== 3 email(s) staged ===" in out           # the preview ran on today's batch
    assert "Dry-run only" in out
    assert not (tmp_path / "sent_log.csv").exists()     # nothing was sent
    assert len(common.read_rows(cfg["files"]["database"])[1]) == 3


def test_daily_stops_when_paused(tmp_path, capfd):
    cfg = _workspace(tmp_path)
    common.record_warning(cfg, "spam check landed in spam")
    outreach_cli.cmd_daily(argparse.Namespace(no_imap=True), cfg)
    out = capfd.readouterr().out
    assert "Paused, so nothing is staged today." in out
    assert "=== 3. Today's batch" not in out
    assert not (tmp_path / "mailmerge_database.csv").exists()


def test_daily_reminds_about_spam_check(tmp_path, capfd):
    outreach_cli.cmd_daily(argparse.Namespace(no_imap=True), _workspace(tmp_path))
    assert "Spam check: never done" in capfd.readouterr().out
