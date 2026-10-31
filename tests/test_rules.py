"""Tests for the adjustable staging rules: email type by tenure/title, people per
company by company size, priority (school, tags, surname list), daily limits per
type, and reply rates in `status`.

Hermetic: temp CSVs and a hand-built config dict; no network or account.
"""
import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common  # noqa: E402
import outreach_cli  # noqa: E402

FIELDS = ["type", "contact_email", "first_name", "last_name", "company", "company_size",
          "title", "start_date", "school", "priority_tags", "sent_at", "last_reply"]
TODAY = datetime.date(2026, 10, 31)
# Staging uses the real date, so stage tests use start dates relative to it.
RECENT = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()  # < 1 year


def _person(email, **kw):
    row = {"contact_email": email, "first_name": "Pat", "last_name": "Smith",
           "company": f"Co-{email}", "title": "Software Engineer", "start_date": RECENT}
    return {**row, **kw}


def _cfg(tmp_path, **over):
    surnames = tmp_path / "surnames.txt"
    surnames.write_text("# test list\nZhang\nOu-Yang\n")
    cfg = {
        "files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                  "sent_log": str(tmp_path / "sent.csv")},
        "pacing": {"batch_size": 50},
        "company_limits": dict(common.DEFAULT_COMPANY_LIMITS),
        "routing": dict(common.DEFAULT_ROUTING),
        "daily": dict(common.DEFAULT_DAILY),
        "priority": {"schools": ["Emory"], "tags": ["chinese"],
                     "surname_lists": {"chinese": str(surnames)}},
    }
    cfg.update(over)
    return cfg


def _stage(cfg, queue, sent=(), n=None):
    common.write_db(cfg["files"]["queue"], FIELDS, queue)
    common.write_db(cfg["files"]["database"], FIELDS, [])
    common.write_db(cfg["files"]["sent_log"], FIELDS, list(sent))
    outreach_cli.cmd_stage(argparse.Namespace(n=n), cfg)
    _, staged = common.read_rows(cfg["files"]["database"])
    _, left = common.read_rows(cfg["files"]["queue"])
    return staged, left


# --- email type ([routing]) -------------------------------------------------

def test_long_tenure_gets_coffee_chat(tmp_path):
    assert common.pick_type({"start_date": "2022-06-01"}, _cfg(tmp_path), TODAY) == "coffee_chat"


def test_short_tenure_gets_referral(tmp_path):
    row = {"start_date": "2025-06", "title": "Software Engineer"}
    assert common.pick_type(row, _cfg(tmp_path), TODAY) == "referral"


def test_senior_title_gets_coffee_chat_even_if_new(tmp_path):
    row = {"start_date": "2026-01", "title": "Engineering Manager"}
    assert common.pick_type(row, _cfg(tmp_path), TODAY) == "coffee_chat"


def test_title_word_must_match_whole_word(tmp_path):
    row = {"title": "Leadership Program Analyst"}  # "lead" inside "Leadership"
    assert common.pick_type(row, _cfg(tmp_path), TODAY) == "referral"


def test_unknown_start_date_and_plain_title_gets_referral(tmp_path):
    assert common.pick_type({"title": "Engineer"}, _cfg(tmp_path), TODAY) == "referral"


def test_type_typed_by_hand_always_wins(tmp_path):
    row = {"type": "Referral", "title": "Director", "start_date": "2010"}
    assert common.pick_type(row, _cfg(tmp_path), TODAY) == "referral"


def test_routing_numbers_are_adjustable(tmp_path):
    cfg = _cfg(tmp_path, routing={"coffee_chat_min_years": 1, "coffee_chat_titles": []})
    assert common.pick_type({"start_date": "2025-06"}, cfg, TODAY) == "coffee_chat"


# --- people per company ([company_limits]) ----------------------------------

def test_company_limit_by_size(tmp_path):
    cfg = _cfg(tmp_path)
    assert common.company_limit({"company_size": "70,000"}, cfg) == 7      # Meta-size
    assert common.company_limit({"company_size": "10001-50000"}, cfg) == 7
    assert common.company_limit({"company_size": "5000"}, cfg) == 5
    assert common.company_limit({"company_size": "500"}, cfg) == 3
    assert common.company_limit({"company_size": ""}, cfg) == 4            # unknown


def test_stage_allows_more_people_at_big_companies(tmp_path):
    big = [_person(f"m{i}@meta.com", company="Meta", company_size="70000") for i in range(9)]
    small = [_person(f"s{i}@tiny.io", company="Tiny", company_size="500") for i in range(5)]
    cfg = _cfg(tmp_path, daily={"referral": 50})
    staged, left = _stage(cfg, big + small)
    count = {c: sum(r["company"] == c for r in staged) for c in ("Meta", "Tiny")}
    assert count == {"Meta": 7, "Tiny": 3}
    assert len(left) == 4  # the extra people wait in the queue, not deleted


def test_already_sent_people_count_toward_company_limit(tmp_path):
    sent = [_person(f"old{i}@tiny.io", company="Tiny", company_size="500") for i in range(2)]
    queue = [_person(f"new{i}@tiny.io", company="Tiny", company_size="500") for i in range(3)]
    staged, _ = _stage(_cfg(tmp_path), queue, sent=sent)
    assert len(staged) == 1  # limit 3, 2 already emailed


# --- priority ([priority]) ---------------------------------------------------

def test_priority_people_are_staged_first(tmp_path):
    queue = [_person("plain@x.com"),
             _person("emory@x.com", school="Emory University"),
             _person("both@x.com", school="Emory University", last_name="Zhang"),
             _person("tagged@x.com", priority_tags="chinese")]
    staged, left = _stage(_cfg(tmp_path), queue, n=3)
    assert [r["contact_email"] for r in staged] == ["both@x.com", "emory@x.com", "tagged@x.com"]
    assert [r["contact_email"] for r in left] == ["plain@x.com"]


def test_surname_list_adds_tag(tmp_path):
    cfg = _cfg(tmp_path)
    assert common.auto_tags({"last_name": "ZHANG"}, cfg) == {"chinese"}
    assert common.auto_tags({"last_name": "Ouyang"}, cfg) == {"chinese"}  # hyphen ignored
    assert common.auto_tags({"last_name": "Smith"}, cfg) == set()


def test_staged_row_records_type_and_tags(tmp_path):
    staged, _ = _stage(_cfg(tmp_path), [_person("a@x.com", last_name="Zhang",
                                                 start_date="2020-01")])
    assert staged[0]["type"] == "coffee_chat"
    assert staged[0]["priority_tags"] == "chinese"


# --- daily limits per type ([daily]) ----------------------------------------

def test_daily_split_six_coffee_eight_referral(tmp_path):
    seniors = [_person(f"sr{i}@x.com", start_date="2018-01") for i in range(10)]
    juniors = [_person(f"jr{i}@x.com") for i in range(10)]
    staged, left = _stage(_cfg(tmp_path), seniors + juniors)
    kinds = [r["type"] for r in staged]
    assert kinds.count("coffee_chat") == 6
    assert kinds.count("referral") == 8
    assert len(left) == 6  # 4 + 2 wait for tomorrow


def test_emails_sent_today_count_toward_daily_limit(tmp_path):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sent = [_person(f"done{i}@x.com", type="coffee_chat", sent_at=now) for i in range(5)]
    queue = [_person(f"sr{i}@x.com", start_date="2018-01") for i in range(3)]
    staged, _ = _stage(_cfg(tmp_path), queue, sent=sent)
    assert len(staged) == 1  # 6 a day, 5 already sent today


def test_total_cap_n_still_applies(tmp_path):
    queue = [_person(f"jr{i}@x.com") for i in range(5)]
    staged, _ = _stage(_cfg(tmp_path), queue, n=2)
    assert len(staged) == 2


# --- reply rates in status ---------------------------------------------------

def test_status_shows_reply_rates_by_type_and_priority(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    sent = [_person("a@x.com", type="coffee_chat", school="Emory", last_reply="2026-10-30"),
            _person("b@x.com", type="coffee_chat"),
            _person("c@x.com", type="referral", priority_tags="chinese")]
    _stage(cfg, [], sent=sent)
    capsys.readouterr()
    outreach_cli.cmd_status(argparse.Namespace(), cfg)
    out = capsys.readouterr().out
    assert "type: coffee_chat" in out and "sent   2   replied   1   (50%)" in out
    assert "priority: Emory" in out
    assert "priority: chinese" in out
