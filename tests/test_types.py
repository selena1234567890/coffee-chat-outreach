"""Tests for per-type templates (coffee_chat / referral), the blank-field guard,
the per-company cap in `stage`, and `--test` sending only to yourself.

Hermetic: temp workspace, fake SMTP, no network or real account.
"""
import argparse
import contextlib
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common  # noqa: E402
import outreach_cli  # noqa: E402
import send  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
FIELDS = ["type", "contact_email", "first_name", "company", "connection", "job_link",
          "subject"]

COFFEE = ("TO: {{contact_email}}\nSUBJECT: {{subject}}\nFROM: Me <me@x.com>\n\n"
          "Hi {{first_name}}, coffee chat about {{company}}? {{connection}}.\n")
REFERRAL = ("TO: {{contact_email}}\nSUBJECT: {{subject}}\nFROM: Me <me@x.com>\n"
            "ATTACHMENT: resume.pdf\n\n"
            "Hi {{first_name}}, could you refer me at {{company}}? {{job_link}}\n")


def _row(kind, email, **kw):
    row = {"type": kind, "contact_email": email, "first_name": "Pat", "company": "Acme",
           "connection": "We're both alumni", "job_link": "https://jobs/1",
           "subject": f"{kind} hello"}
    return {**row, **kw}


class _FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


def _workspace(tmp_path, rows):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "coffee_chat.txt").write_text(COFFEE)
    (tmp_path / "templates" / "referral.txt").write_text(REFERRAL)
    shutil.copy(os.path.join(FIXTURES, "resume.pdf"), tmp_path / "resume.pdf")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[email]\naddress = "me@x.com"\nsmtp_host = "smtp.example.com"\n'
        'smtp_port = 587\nkeychain_service = "svc"\n'
        '[files]\ndatabase = "db.csv"\nsent_log = "sent.csv"\n'
        '[templates]\ncoffee_chat = "templates/coffee_chat.txt"\n'
        'referral = "templates/referral.txt"\n')
    common.write_db(str(tmp_path / "db.csv"), FIELDS, rows)
    return cfg_path


def _run(monkeypatch, tmp_path, rows, *flags):
    cfg_path = _workspace(tmp_path, rows)
    fake = _FakeSMTP()
    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")

    @contextlib.contextmanager
    def fake_session(*a, **k):
        yield fake

    monkeypatch.setattr(common, "smtp_session", fake_session)
    monkeypatch.setattr(sys, "argv", ["send.py", "--config", str(cfg_path), *flags])
    send.main()
    return fake


def test_each_row_uses_template_for_its_type(monkeypatch, tmp_path):
    fake = _run(monkeypatch, tmp_path,
                [_row("coffee_chat", "a@x.com"), _row("referral", "b@x.com")],
                "--send", "--yes")
    by_to = {m["To"]: m for m in fake.sent}
    assert "coffee chat" in by_to["a@x.com"].get_body().get_content()
    assert not by_to["a@x.com"].is_multipart()            # no resume on coffee chats
    assert "refer me" in by_to["b@x.com"].get_body().get_content()
    assert by_to["b@x.com"].is_multipart()                # resume attached


def test_type_is_recorded_in_sent_log(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, [_row("referral", "b@x.com")], "--send", "--yes")
    _, logged = common.read_rows(str(tmp_path / "sent.csv"))
    assert logged[0]["type"] == "referral"
    assert logged[0]["first_name"] == "Pat"  # extra CSV columns are kept too


def test_blank_placeholder_blocks_whole_batch(monkeypatch, tmp_path, capsys):
    rows = [_row("coffee_chat", "a@x.com"), _row("coffee_chat", "b@x.com", first_name=" ")]
    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, rows, "--send", "--yes")
    assert "b@x.com: blank first_name" in capsys.readouterr().out
    assert not (tmp_path / "sent.csv").exists()  # nothing sent, nothing logged


def test_blank_field_only_counts_if_its_template_uses_it(monkeypatch, tmp_path):
    # Coffee chat template has no {{job_link}}, so an empty one is fine.
    fake = _run(monkeypatch, tmp_path, [_row("coffee_chat", "a@x.com", job_link="")],
                "--send", "--yes")
    assert len(fake.sent) == 1


def test_unknown_type_is_rejected(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, [_row("coffe_chat", "a@x.com")], "--send", "--yes")
    assert "'coffe_chat' is not one of" in capsys.readouterr().out


def test_test_flag_sends_only_to_self_and_logs_nothing(monkeypatch, tmp_path):
    rows = [_row("coffee_chat", f"{c}@x.com") for c in "abc"]
    fake = _run(monkeypatch, tmp_path, rows, "--test")
    assert [m["To"] for m in fake.sent] == ["me@x.com"] * send.TEST_COUNT
    assert fake.sent[0]["Subject"].startswith("[TEST to a@x.com]")
    assert not (tmp_path / "sent.csv").exists()
    _, still_staged = common.read_rows(str(tmp_path / "db.csv"))
    assert len(still_staged) == 3


def test_stage_caps_people_per_company(tmp_path):
    files = {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
             "sent_log": str(tmp_path / "sent.csv")}
    cfg = {"files": files, "pacing": {"batch_size": 10},
           "company_limits": {**common.DEFAULT_COMPANY_LIMITS, "unknown": 2}}
    common.write_db(files["sent_log"], FIELDS, [_row("coffee_chat", "a@x.com")])
    common.write_db(files["database"], FIELDS, [])
    common.write_db(files["queue"], FIELDS,
                    [_row("coffee_chat", e) for e in ("b@x.com", "c@x.com")]
                    + [_row("referral", "d@y.com", company="Other")])

    outreach_cli.cmd_stage(argparse.Namespace(n=None), cfg)

    _, staged = common.read_rows(files["database"])
    # Acme (size unknown) already has 1 sent, limit is 2 -> only b@ joins; Other is unaffected.
    assert [r["contact_email"] for r in staged] == ["b@x.com", "d@y.com"]
    _, queue = common.read_rows(files["queue"])
    assert [r["contact_email"] for r in queue] == ["c@x.com"]  # held, not deleted
