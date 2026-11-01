"""Tests for `outreach feedback` (the daily report email) and the Deepline credit
warnings.

Hermetic: temp sent log and state, no network or account.
"""
import datetime

from test_report import _cfg, _sent

import common
import outreach_cli

TODAY = datetime.date(2026, 11, 2)
NOW = datetime.datetime(2026, 11, 2, 18, 0, tzinfo=datetime.timezone.utc)
CREDITS = {"balance": 200.0, "used_30": 20.0, "cap": 500.0}


def _setup(tmp_path, rows, run=None, queue=20):
    cfg = _cfg(tmp_path, rows)
    cfg["files"]["held"] = str(tmp_path / "held.csv")
    common.write_db(cfg["files"]["queue"], ["contact_email"],
                    [{"contact_email": f"q{i}@x.com"} for i in range(queue)])
    if run is not None:
        common.save_state(cfg, {"step": 0, "step_started": "2026-11-02",
                                "last_send": {"date": TODAY.isoformat(), **run}})
    return cfg


def _feedback(cfg, credits=CREDITS):
    return outreach_cli.build_feedback(cfg, today=TODAY, credits=credits, now=NOW)


def test_all_good_day(tmp_path):
    rows = [_sent("a@g.com", last_reply="2026-11-02T15:00:00Z", first_name="Ann"),
            _sent("b@g.com", outcome="referral_given", last_reply="2026-10-30T10:00:00Z")]
    subject, text = _feedback(_setup(tmp_path, rows, run={"sent": 13, "failed": [], "held": 0}))
    assert subject == "Outreach Nov 2: all good"
    assert "Everything's good." in text and "Needs you" not in text
    assert "Sent 13 email(s)." in text
    assert "New replies (last 24 hours): Ann Lee (Google)" in text
    assert "RESPONSE RATE:          2 replied / 2 emailed = 100%" in text
    assert "REFERRAL SUCCESS RATE:  1 referrals / 2 emailed = 50%" in text


def test_problems_are_listed(tmp_path):
    rows = [_sent("a@g.com", bounced_at="2026-11-02")]
    cfg = _setup(tmp_path, rows, run={"sent": 3, "failed": ["x@y.com (Y)"], "held": 2},
                 queue=1)
    common.hold_rows(cfg, [{"contact_email": "h@g.com"}, {"contact_email": "i@g.com"}])
    subject, text = _feedback(cfg, credits=None)
    assert "need you" in subject
    for part in ("1 failed to send: x@y.com (Y)", "1 bounced: a@g.com",
                 "2 email(s) held for your OK", "Queue is low: 1 people left",
                 "Couldn't check Deepline credits"):
        assert part in text, part


def test_no_run_and_safety_stop(tmp_path):
    assert "Nothing was sent today" in _feedback(_setup(tmp_path, [_sent("a@g.com")]))[1]
    text = _feedback(_setup(tmp_path, [_sent("a@g.com")],
                            run={"sent": 0, "stopped": True, "problems": ["template changed"]}))[1]
    assert "SAFETY STOP: nothing was sent. Problems: template changed" in text


def test_credit_warnings():
    assert outreach_cli.credit_warnings(CREDITS, 13) == []
    soon = outreach_cli.credit_warnings({"balance": 40.0}, 13)       # ~3.8 days
    assert soon[0].startswith("Top up Deepline soon")
    now = outreach_cli.credit_warnings({"balance": 15.0}, 13)        # ~1.4 days
    assert now[0].startswith("TOP UP DEEPLINE NOW")
    cap = outreach_cli.credit_warnings({"balance": 500.0, "used_30": 20.0, "cap": 25.0}, 13)
    assert "monthly cap almost reached" in cap[0]
