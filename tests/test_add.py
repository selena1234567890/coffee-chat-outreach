"""Tests for `outreach add` (the checked door from the contact finder into the
queue) and for showing agent-written lines as `>> CHECK:` in preview.

Hermetic: temp files, fake SMTP, no network or account.
"""
import argparse
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common  # noqa: E402
import outreach_cli  # noqa: E402
import send  # noqa: E402


def _lead(email, **kw):
    lead = {"contact_email": email, "first_name": "Pat", "last_name": "Smith",
            "company": "Google", "company_size": 180000, "title": "Data Scientist",
            "email_status": "verified", "personal_line": "Your talk on ranking was great.",
            "personal_source": "https://example.com/talk"}
    return {**lead, **kw}


def _cfg(tmp_path):
    (tmp_path / "dnc.csv").write_text("first_name,last_name,company,contact_email,note\n"
                                      "Kevin,Wu,Capital One,,\n")
    return {
        "files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                  "sent_log": str(tmp_path / "sent.csv"),
                  "do_not_contact": str(tmp_path / "dnc.csv"),
                  "targets": str(tmp_path / "none.csv")},
        "company_limits": dict(common.DEFAULT_COMPANY_LIMITS),
        "search": dict(common.DEFAULT_SEARCH),
    }


def _add(tmp_path, cfg, leads, dry_run=False):
    path = tmp_path / "leads.json"
    path.write_text(json.dumps(leads))
    outreach_cli.cmd_add(argparse.Namespace(file=str(path), dry_run=dry_run), cfg)
    if not os.path.exists(cfg["files"]["queue"]):
        return []
    return common.read_rows(cfg["files"]["queue"])[1]


def test_good_leads_are_added_with_all_columns(tmp_path):
    cfg = _cfg(tmp_path)
    rows = _add(tmp_path, cfg, [_lead("a@google.com", previous_company="Uber")])
    assert len(rows) == 1
    assert rows[0]["previous_company"] == "Uber"
    assert rows[0]["company_size"] == "180000"   # numbers saved as text
    assert rows[0]["found_at"]                   # stamped when added


def test_each_bad_lead_is_skipped_with_a_reason(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    common.write_db(cfg["files"]["sent_log"], ["contact_email"], [{"contact_email": "old@x.com"}])
    rows = _add(tmp_path, cfg, [
        _lead("ok@google.com"),
        _lead("", first_name="NoEmail"),
        _lead("old@x.com"),                                        # already emailed
        _lead("guess@google.com", email_status="unverified"),
        _lead("kw@capitalone.com", first_name="Kevin", last_name="Wu",
              company="Capital One"),                              # do-not-contact
        _lead("t@tiny.io", company="Tiny", company_size=300),     # under 1,000
        _lead("ok@google.com"),                                    # duplicate in the file
    ])
    assert [r["contact_email"] for r in rows] == ["ok@google.com"]
    out = capsys.readouterr().out
    for reason in ("missing contact_email", "already in queue, batch or sent log",
                   "email status is 'unverified'", "do-not-contact", "under min_employees"):
        assert reason in out


def test_dry_run_changes_nothing(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert _add(tmp_path, cfg, [_lead("a@google.com")], dry_run=True) == []
    assert "Would add 1" in capsys.readouterr().out


def test_existing_queue_keeps_its_rows_and_gains_new_columns(tmp_path):
    cfg = _cfg(tmp_path)
    common.write_db(cfg["files"]["queue"], ["contact_email", "first_name", "company"],
                    [{"contact_email": "old@q.com", "first_name": "Old", "company": "Meta"}])
    rows = _add(tmp_path, cfg, [_lead("new@google.com")])
    assert [r["contact_email"] for r in rows] == ["old@q.com", "new@google.com"]
    assert rows[1]["personal_line"] == "Your talk on ranking was great."


def test_add_shows_check_lines(tmp_path, capsys):
    _add(tmp_path, _cfg(tmp_path), [_lead("a@google.com"),
                                    _lead("b@google.com", personal_source="")])
    out = capsys.readouterr().out
    assert '>> CHECK: "Your talk on ranking was great."' in out
    assert "source: https://example.com/talk" in out
    assert "(no source: plain line)" in out


def test_bad_file_gives_a_clear_error(tmp_path):
    path = tmp_path / "leads.json"
    path.write_text('{"not": "a list"}')
    with pytest.raises(SystemExit, match="JSON list"):
        outreach_cli.cmd_add(argparse.Namespace(file=str(path), dry_run=False), _cfg(tmp_path))


def test_preview_highlights_personal_line(monkeypatch, tmp_path, capsys):
    (tmp_path / "t.txt").write_text("TO: {{contact_email}}\nSUBJECT: Hi\n\n{{personal_line}}\n")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[files]\ndatabase = "db.csv"\nsent_log = "sent.csv"\n'
                        'template = "t.txt"\n')
    common.write_db(str(tmp_path / "db.csv"), list(_lead("a@google.com")),
                    [_lead("a@google.com")])
    monkeypatch.setattr(sys, "argv", ["send.py", "--config", str(cfg_path)])
    send.main()  # dry run: preview only
    out = capsys.readouterr().out
    assert '>> CHECK: "Your talk on ranking was great."' in out
    assert "Dry-run only" in out
