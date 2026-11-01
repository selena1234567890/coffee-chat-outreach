"""Tests for `outreach report` (metrics to tune the tool by) and `outreach mark`
(recording outcomes).

Hermetic: temp sent log, no network or account.
"""
import argparse

import pytest

import common
import outreach_cli

FIELDS = ["type", "contact_email", "first_name", "last_name", "company", "source",
          "template_version", "tier", "priority_tags", "school", "personal_line",
          "personal_source", "open_intern_roles", "sent_at", "last_reply", "bounced_at",
          "outcome", "outcome_at"]


def _sent(email, **kw):
    row = {"type": "referral", "contact_email": email, "first_name": "Pat", "last_name": "Lee",
           "company": "Google", "source": "web", "template_version": "referral_1.txt",
           "tier": "1", "sent_at": "2026-11-02T15:00:00Z"}
    return {**row, **kw}


def _cfg(tmp_path, rows):
    cfg = {"files": {"sent_log": str(tmp_path / "sent.csv"), "queue": str(tmp_path / "q.csv"),
                     "database": str(tmp_path / "db.csv"),
                     "send_state": str(tmp_path / "state.json"),
                     "targets": str(tmp_path / "none.csv")},
           "priority": {"schools": ["Emory"], "tags": ["chinese", "hand_picked"],
                        "surname_lists": {}},
           "ramp": dict(common.DEFAULT_RAMP), "followup": dict(common.DEFAULT_FOLLOWUP)}
    common.write_db(cfg["files"]["sent_log"], FIELDS, rows)
    return cfg


def _report(cfg, capsys):
    outreach_cli.cmd_report(argparse.Namespace(no_imap=True), cfg)
    return capsys.readouterr().out


def test_people_results_reply_bounce_and_followup(tmp_path):
    rows = [
        _sent("a@g.com", last_reply="2026-11-03T10:00:00Z"),                  # replied, 0.8 days
        _sent("b@g.com", bounced_at="2026-11-02"),                             # bounced
        _sent("c@g.com", last_reply="2026-11-09T09:00:00Z"),                  # after follow-up
        {**_sent("c@g.com", type="followup"), "sent_at": "2026-11-05T15:00:00Z",
         "last_reply": "2026-11-09T09:00:00Z"},
        _sent("d@g.com"),                                                      # no reply
    ]
    cfg = _cfg(tmp_path, rows)
    res = {p["row"]["contact_email"]: p for p in outreach_cli.person_results(rows, cfg)}
    assert len(res) == 4                                   # the follow-up isn't a new person
    assert res["a@g.com"]["replied"] and not res["a@g.com"]["replied_after_followup"]
    assert round(res["a@g.com"]["days_to_reply"], 1) == 0.8
    assert res["b@g.com"]["bounced"] and not res["b@g.com"]["replied"]
    assert res["c@g.com"]["replied_after_followup"] and res["c@g.com"]["followup_sent"]
    assert not res["d@g.com"]["replied"]


def test_report_overall_numbers(tmp_path, capsys):
    rows = [_sent("a@g.com", last_reply="2026-11-03T10:00:00Z", outcome="referral_given"),
            _sent("b@g.com", bounced_at="2026-11-02"),
            _sent("c@g.com"), _sent("d@g.com")]
    out = _report(_cfg(tmp_path, rows), capsys)
    assert "4 people emailed" in out
    assert "Bounced: 1 (25%)" in out
    assert "Replied: 1 (33% of delivered)" in out          # 1 of 3 delivered
    assert "Good results: 1" in out
    assert "Outcomes: referral_given 1" in out


def test_report_splits_by_every_dimension(tmp_path, capsys):
    rows = [_sent("a@g.com", type="coffee_chat", template_version="coffee_chat_2.txt",
                  school="Emory University", personal_line="Loved your talk.",
                  personal_source="https://x", source="hand_picked",
                  last_reply="2026-11-03T10:00:00Z"),
            _sent("b@g.com", personal_line="Plain line.", priority_tags="chinese",
                  open_intern_roles="yes", tier="3")]
    out = _report(_cfg(tmp_path, rows), capsys)
    for heading in ("By type:", "By template version:", "By tier:", "By priority:",
                    "By source:", "By personal line:", "By open intern roles:"):
        assert heading in out
    for group in ("coffee_chat", "coffee_chat_2.txt", "tier 3", "Emory", "chinese",
                  "hand_picked", "real fact", "plain line"):
        assert group in out
    assert "too few to judge" in out


def test_report_credits_per_reply(tmp_path, capsys):
    rows = [_sent(f"p{i}@g.com", source="hand_picked") for i in range(4)]
    rows[0]["last_reply"] = "2026-11-03T10:00:00Z"
    out = _report(_cfg(tmp_path, rows), capsys)
    assert "about 4 used" in out and "credits per reply: 4.0" in out


def test_report_with_nothing_sent(tmp_path, capsys):
    assert "Nothing sent yet" in _report(_cfg(tmp_path, []), capsys)


def test_report_includes_health(tmp_path, capsys):
    assert "=== Sending health ===" in _report(_cfg(tmp_path, [_sent("a@g.com")]), capsys)


# --- outreach mark ------------------------------------------------------------

def test_mark_records_outcome(tmp_path, capsys):
    cfg = _cfg(tmp_path, [_sent("a@g.com"), _sent("b@g.com")])
    outreach_cli.cmd_mark(argparse.Namespace(email="A@G.com", outcome="coffee_chat_booked"),
                          cfg)
    rows = {r["contact_email"]: r for r in common.read_rows(cfg["files"]["sent_log"])[1]}
    assert rows["a@g.com"]["outcome"] == "coffee_chat_booked"
    assert rows["a@g.com"]["outcome_at"]
    assert rows["b@g.com"]["outcome"] == ""
    assert "coffee_chat_booked" in capsys.readouterr().out


def test_mark_rejects_unknown_outcome_and_unknown_person(tmp_path):
    cfg = _cfg(tmp_path, [_sent("a@g.com")])
    with pytest.raises(SystemExit, match="outcome must be one of"):
        outreach_cli.cmd_mark(argparse.Namespace(email="a@g.com", outcome="maybe"), cfg)
    with pytest.raises(SystemExit, match="not in sent.csv"):
        outreach_cli.cmd_mark(argparse.Namespace(email="z@g.com", outcome="interview"), cfg)


def test_report_headline_metrics(tmp_path, capsys):
    rows = [_sent("a@g.com", last_reply="2026-11-03T10:00:00Z", outcome="referral_given"),
            _sent("b@g.com", type="coffee_chat", last_reply="2026-11-03T10:00:00Z",
                  outcome="referral_given"),
            _sent("c@g.com", last_reply="2026-11-04T10:00:00Z"),
            _sent("d@g.com")]
    out = _report(_cfg(tmp_path, rows), capsys)
    assert "RESPONSE RATE:          3 replied / 4 emailed = 75%" in out
    assert "REFERRAL SUCCESS RATE:  2 referrals / 4 emailed = 50%" in out
    assert "of the 3 referral asks: 1 = 33%" in out


def test_report_checks_gmail_for_replies_first(tmp_path, capsys, monkeypatch):
    cfg = _cfg(tmp_path, [_sent("a@g.com")])
    calls = []
    monkeypatch.setattr(outreach_cli, "cmd_backfill_dates", lambda a, c: calls.append(a.apply))
    outreach_cli.cmd_report(argparse.Namespace(no_imap=False), cfg)
    assert calls == [True]                                 # replies synced before counting
    assert "Checking Gmail for new replies" in capsys.readouterr().out
