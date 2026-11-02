"""Tests for targeting: company tiers (target list), the minimum company size,
companies hiring interns first, and the do-not-contact list (at stage and at send).

Hermetic: temp CSVs, a fake SMTP, no network or account.
"""
import argparse
import collections
import contextlib
import datetime
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common  # noqa: E402
import outreach_cli  # noqa: E402
import send  # noqa: E402

FIELDS = ["type", "contact_email", "first_name", "last_name", "company", "company_size",
          "title", "start_date", "school", "priority_tags", "tier", "open_intern_roles",
          "subject", "source", "sent_at", "last_reply"]
RECENT = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
TARGETS = ("company,domain,tier,notes\n"
           "Google,google.com,1,\nMeta,meta.com,1,\nMeta,fb.com,1,\n"
           "Capital One,capitalone.com,2,\nPublix,publix.com,3,\n")
DO_NOT_CONTACT = ("first_name,last_name,company,contact_email,note\n"
                  "Kevin,Wu,Capital One,,\nLeah,,Tesla,,\n,,,known@x.com,\n")


def _person(email, **kw):
    row = {"contact_email": email, "first_name": "Pat", "last_name": "Smith",
           "company": email.split("@")[1], "company_size": "50000",
           "title": "Data Scientist", "start_date": RECENT, "subject": "hi"}
    return {**row, **kw}


def _cfg(tmp_path, **over):
    (tmp_path / "targets.csv").write_text(TARGETS)
    (tmp_path / "dnc.csv").write_text(DO_NOT_CONTACT)
    cfg = {
        "files": {"queue": str(tmp_path / "q.csv"), "database": str(tmp_path / "db.csv"),
                  "sent_log": str(tmp_path / "sent.csv"),
                  "targets": str(tmp_path / "targets.csv"),
                  "do_not_contact": str(tmp_path / "dnc.csv")},
        "pacing": {"batch_size": 50},
        "company_limits": dict(common.DEFAULT_COMPANY_LIMITS),
        "daily": {"coffee_chat": 50, "referral": 50},
        "priority": {"schools": ["Emory"], "tags": ["chinese"], "surname_lists": {}},
    }
    cfg.update(over)
    return cfg


def _stage(cfg, queue, n=None):
    common.write_db(cfg["files"]["queue"], FIELDS, queue)
    common.write_db(cfg["files"]["database"], FIELDS, [])
    common.write_db(cfg["files"]["sent_log"], FIELDS, [])
    outreach_cli.cmd_stage(argparse.Namespace(n=n), cfg)
    _, staged = common.read_rows(cfg["files"]["database"])
    _, left = common.read_rows(cfg["files"]["queue"])
    return staged, left


# --- tier lookup --------------------------------------------------------------

def test_tier_from_company_name_or_email_domain(tmp_path):
    cfg = _cfg(tmp_path)
    assert common.company_tier({"company": "capital one"}, cfg) == 2
    assert common.company_tier({"contact_email": "a@fb.com"}, cfg) == 1
    assert common.company_tier({"contact_email": "a@mail.google.com"}, cfg) == 1  # subdomain
    assert common.company_tier({"company": "Startup", "contact_email": "a@s.io"}, cfg) == \
        common.UNKNOWN_TIER


def test_tier_typed_in_the_row_wins(tmp_path):
    assert common.company_tier({"company": "Google", "tier": "3"}, _cfg(tmp_path)) == 3


# --- staging order ------------------------------------------------------------

def test_stage_order_open_roles_then_tier_then_priority(tmp_path):
    queue = [_person("a@publix.com"),                                        # tier 3
             _person("b@unknown.io"),                                        # not listed
             _person("c@capitalone.com", school="Emory"),                    # tier 2, Emory
             _person("d@google.com"),                                        # tier 1
             _person("e@meta.com", open_intern_roles="yes"),                 # tier 1, open
             _person("f@google.com", school="Emory University")]             # tier 1, Emory
    staged, _ = _stage(_cfg(tmp_path), queue)
    assert [r["contact_email"] for r in staged] == [
        "e@meta.com",        # tier 1 + open roles
        "f@google.com",      # tier 1 + Emory
        "d@google.com",      # tier 1
        "c@capitalone.com",  # tier 2
        "a@publix.com",      # tier 3
        "b@unknown.io",      # not on the target list -> last
    ]
    assert staged[0]["tier"] == "1"  # tier is recorded on the staged row


def test_tier_beats_emory(tmp_path):
    queue = [_person("alum@publix.com", school="Emory", last_name="Zhang"),
             _person("stranger@google.com")]
    staged, _ = _stage(_cfg(tmp_path), queue, n=1)
    assert staged[0]["contact_email"] == "stranger@google.com"


# --- minimum company size -----------------------------------------------------

def test_companies_under_1000_are_skipped_but_kept(tmp_path):
    queue = [_person("a@tiny.io", company_size="800"), _person("b@google.com")]
    staged, left = _stage(_cfg(tmp_path), queue)
    assert [r["contact_email"] for r in staged] == ["b@google.com"]
    assert [r["contact_email"] for r in left] == ["a@tiny.io"]  # not deleted


def test_unknown_size_is_not_skipped(tmp_path):
    staged, _ = _stage(_cfg(tmp_path), [_person("a@google.com", company_size="")])
    assert len(staged) == 1


def test_size_floor_is_adjustable(tmp_path):
    cfg = _cfg(tmp_path, company_limits={**common.DEFAULT_COMPANY_LIMITS, "min_employees": 0})
    staged, _ = _stage(cfg, [_person("a@tiny.io", company_size="800")])
    assert len(staged) == 1


# --- do-not-contact -----------------------------------------------------------

def test_do_not_contact_matching(tmp_path):
    cfg = _cfg(tmp_path)
    assert common.on_do_not_contact(
        {"first_name": "Kevin", "last_name": "Wu", "company": "Capital One Financial"}, cfg)
    assert common.on_do_not_contact(  # first name + company is enough when no last name
        {"first_name": "leah", "last_name": "Anything", "company": "Tesla"}, cfg)
    assert common.on_do_not_contact({"contact_email": "KNOWN@x.com"}, cfg)
    assert not common.on_do_not_contact(  # same name, different company
        {"first_name": "Kevin", "last_name": "Wu", "company": "Google"}, cfg)


def test_stage_removes_do_not_contact_people(tmp_path, capsys):
    queue = [_person("kw@capitalone.com", first_name="Kevin", last_name="Wu",
                     company="Capital One"),
             _person("ok@google.com")]
    staged, left = _stage(_cfg(tmp_path), queue)
    assert [r["contact_email"] for r in staged] == ["ok@google.com"]
    assert left == []
    assert "do-not-contact list" in capsys.readouterr().out


def test_send_skips_do_not_contact_even_if_staged(monkeypatch, tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "referral.txt").write_text(
        "TO: {{contact_email}}\nSUBJECT: {{subject}}\n\nHi {{first_name}}\n")
    shutil.copy(os.path.join(ROOT, "tests", "fixtures", "resume.pdf"), tmp_path / "resume.pdf")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "do_not_contact.csv").write_text(DO_NOT_CONTACT)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[email]\naddress = "me@x.com"\nsmtp_port = 587\n'
                        '[files]\ndatabase = "db.csv"\nsent_log = "sent.csv"\n'
                        '[templates]\nreferral = "templates/referral.txt"\n')
    rows = [_person("kw@capitalone.com", type="referral", first_name="Kevin", last_name="Wu",
                    company="Capital One"),
            _person("ok@google.com", type="referral")]
    common.write_db(str(tmp_path / "db.csv"), FIELDS, rows)

    sent = []

    class FakeSMTP:
        def send_message(self, msg):
            sent.append(msg["To"])

    @contextlib.contextmanager
    def fake_session(*a, **k):
        yield FakeSMTP()

    monkeypatch.setattr(common, "get_password", lambda *a, **k: "pw")
    monkeypatch.setattr(common, "smtp_session", fake_session)
    monkeypatch.setattr(sys, "argv", ["send.py", "--config", str(cfg_path), "--send", "--yes"])
    send.main()
    assert sent == ["ok@google.com"]


# --- reply rates by tier ------------------------------------------------------

def test_status_shows_reply_rates_by_tier(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    common.write_db(cfg["files"]["sent_log"], FIELDS,
                    [_person("a@google.com", type="referral", last_reply="2026-10-30"),
                     _person("b@publix.com", type="referral")])
    common.write_db(cfg["files"]["queue"], FIELDS, [])
    common.write_db(cfg["files"]["database"], FIELDS, [])
    outreach_cli.cmd_status(argparse.Namespace(), cfg)
    out = capsys.readouterr().out
    assert "tier: 1" in out and "tier: 3" in out


# --- daily share per tier ([tier_share]) ---------------------------------------

def _mixed_queue(t1, t2, t3):
    return ([_person(f"g{i}@google.com") for i in range(t1)]
            + [_person(f"c{i}@capitalone.com") for i in range(t2)]
            + [_person(f"p{i}@publix.com") for i in range(t3)])


def _tiers(staged):
    return collections.Counter(r["tier"] for r in staged)


def test_each_tier_gets_its_share(tmp_path):
    cfg = _cfg(tmp_path, daily={"referral": 14},
               company_limits={**common.DEFAULT_COMPANY_LIMITS, "large": 50})
    staged, _ = _stage(cfg, _mixed_queue(20, 10, 5))
    # 60/30/10% of 14 -> 8/4/1; the 1 slot left over goes to the best remaining (tier 1)
    assert _tiers(staged) == {"1": 9, "2": 4, "3": 1}


def test_every_tier_gets_at_least_one(tmp_path):
    cfg = _cfg(tmp_path, daily={"referral": 3},
               company_limits={**common.DEFAULT_COMPANY_LIMITS, "large": 50})
    staged, _ = _stage(cfg, _mixed_queue(10, 10, 10))
    assert _tiers(staged) == {"1": 1, "2": 1, "3": 1}  # even though 10% of 3 rounds to 0


def test_unused_tier_slots_go_to_others(tmp_path):
    cfg = _cfg(tmp_path, daily={"referral": 10},
               company_limits={**common.DEFAULT_COMPANY_LIMITS, "large": 50})
    staged, _ = _stage(cfg, _mixed_queue(20, 0, 0))  # only big tech in the queue
    assert _tiers(staged) == {"1": 10}


def test_tier_share_is_adjustable(tmp_path):
    cfg = _cfg(tmp_path, daily={"referral": 4}, tier_share={1: 0, 2: 100, 3: 0},
               company_limits={**common.DEFAULT_COMPANY_LIMITS, "large": 50})
    staged, _ = _stage(cfg, _mixed_queue(10, 10, 10))
    assert _tiers(staged) == {"2": 4}


# --- hand-picked people (source = hand_picked) ----------------------------------

def test_hand_picked_get_priority_tag(tmp_path):
    cfg = _cfg(tmp_path)
    assert "hand_picked" in common.auto_tags({"source": "hand_picked"}, cfg)
    assert "hand_picked" not in common.auto_tags({"source": "agent"}, cfg)


def test_hand_picked_at_unlisted_company_counts_as_tier_1(tmp_path):
    cfg = _cfg(tmp_path)
    row = {"company": "Dream Startup", "contact_email": "a@dream.ai"}
    assert common.company_tier(row, cfg) == common.UNKNOWN_TIER
    assert common.company_tier({**row, "source": "hand_picked"}, cfg) == 1
    # a listed company keeps its own tier
    assert common.company_tier({"company": "Publix", "source": "hand_picked"}, cfg) == 3


def test_hand_picked_go_first_within_their_tier(tmp_path):
    cfg = _cfg(tmp_path, priority={"schools": [], "tags": ["hand_picked"],
                                   "surname_lists": {}})
    queue = [_person("agent@google.com", source="agent"),
             _person("mine@google.com", source="hand_picked")]
    staged, _ = _stage(cfg, queue, n=1)
    assert staged[0]["contact_email"] == "mine@google.com"
    assert "hand_picked" in staged[0]["priority_tags"]


def test_status_shows_reply_rates_by_source(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    fields = FIELDS
    common.write_db(cfg["files"]["sent_log"], fields,
                    [_person("a@google.com", source="hand_picked", last_reply="2026-10-30"),
                     _person("b@google.com", source="agent")])
    common.write_db(cfg["files"]["queue"], fields, [])
    common.write_db(cfg["files"]["database"], fields, [])
    outreach_cli.cmd_status(argparse.Namespace(), cfg)
    out = capsys.readouterr().out
    assert "source: hand_picked" in out and "source: agent" in out


def test_big_tier_cannot_use_up_a_type_limit_before_small_tiers_get_theirs(tmp_path):
    # 14 emails today, but only 10 referrals allowed and everyone is a referral person:
    # tier 3 and tier 2 still get their shares before big tech fills the rest.
    cfg = _cfg(tmp_path, daily={"coffee_chat": 4, "referral": 10},
               company_limits={**common.DEFAULT_COMPANY_LIMITS, "large": 50})
    staged, _ = _stage(cfg, _mixed_queue(20, 10, 5))
    assert _tiers(staged) == {"3": 1, "2": 4, "1": 5}   # shares 8/4/1 of 14; 10 referrals


# --- covered companies (already have a referral there) -------------------------

def _covered_cfg(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "covered.csv").write_text("company,domains,note\n"
                                          "Capital One,capitalone.com,have referral\n"
                                          "Meta,meta.com;fb.com,\n")
    cfg["files"]["covered_companies"] = str(tmp_path / "covered.csv")
    return cfg


def test_covered_company_matching(tmp_path):
    cfg = _covered_cfg(tmp_path)
    assert common.covered_company({"company": "Capital One Financial Corp."}, cfg)
    assert common.covered_company({"company": "Something", "contact_email": "a@fb.com"}, cfg)
    assert not common.covered_company({"company": "Google", "contact_email": "a@google.com"}, cfg)
    assert common.skip_reason({"company": "Meta"}, cfg) == \
        "company already covered (you have a referral there)"


def test_stage_removes_people_at_covered_companies(tmp_path, capsys):
    cfg = _covered_cfg(tmp_path)
    staged, left = _stage(cfg, [_person("a@capitalone.com", company="Capital One"),
                                _person("b@google.com")])
    assert [r["contact_email"] for r in staged] == ["b@google.com"]
    assert left == []
    assert "company already covered" in capsys.readouterr().out


def test_no_followups_to_covered_companies(tmp_path):
    cfg = _covered_cfg(tmp_path)
    sent = {**_person("a@capitalone.com", company="Capital One"), "type": "referral",
            "sent_at": "2026-10-01T12:00:00Z", "message_id": "<a@x>"}
    assert outreach_cli.due_followups([sent], [], cfg) == []


# --- data/ML/AI roles first ------------------------------------------------------

def test_relevant_role():
    cfg = {"priority": {}}
    for title in ("Senior Data Science Analyst", "ML Engineer", "Applied AI Lead",
                  "Business Intelligence Product Manager", "Quantitative Researcher", ""):
        assert common.relevant_role({"title": title}, cfg), title
    for title in ("Integration Engineer", "Investment Analyst", "Equity Research Associate",
                  "Management Consultant", "Domain Expert"):     # "ai" in "domain" isn't a word
        assert not common.relevant_role({"title": title}, cfg), title
    assert common.relevant_role({"title": "Consultant", "source": common.HAND_PICKED}, cfg)


def test_other_roles_go_last_even_from_a_better_tier(tmp_path):
    queue = [_person("consult@google.com", title="Management Consultant", school="Emory"),
             _person("ds@publix.com", title="Data Scientist")]
    staged, left = _stage(_cfg(tmp_path), queue, n=1)
    assert [r["contact_email"] for r in staged] == ["ds@publix.com"]
    assert [r["contact_email"] for r in left] == ["consult@google.com"]   # kept as a backup
    staged, _ = _stage(_cfg(tmp_path), queue, n=2)                         # room for both
    assert [r["contact_email"] for r in staged] == ["ds@publix.com", "consult@google.com"]


# --- companies hiring interns first (data/open_roles.csv) ----------------------------

OPEN_ROLES = ("company,domain,hiring,role,link,checked\n"
              "Publix,publix.com,yes,Data Science Intern,https://x/1,2026-11-01\n"
              "Google,google.com,no,,,2026-11-01\n")


def _hiring_cfg(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "open_roles.csv").write_text(OPEN_ROLES)
    cfg["files"]["open_roles"] = str(tmp_path / "open_roles.csv")
    common.load_open_roles.cache_clear()
    return cfg


def test_has_open_roles_from_the_hiring_list(tmp_path):
    cfg = _hiring_cfg(tmp_path)
    assert common.has_open_roles({"company": "Publix Super Markets Inc"}, cfg)
    assert common.has_open_roles({"company": "", "contact_email": "a@corp.publix.com"}, cfg)
    assert not common.has_open_roles({"company": "Google", "open_intern_roles": "yes"}, cfg)
    assert common.has_open_roles({"company": "Meta", "open_intern_roles": "yes"}, cfg)
    assert not common.has_open_roles({"company": "Meta"}, cfg)


def test_hiring_beats_tier_but_not_relevance(tmp_path):
    queue = [_person("ds@google.com", company="Google"),                       # tier 1
             _person("ds@publix.com", company="Publix"),                       # tier 3, hiring
             _person("hr@publix.com", company="Publix", title="HR Manager")]   # hiring, not data
    staged, _ = _stage(_hiring_cfg(tmp_path), queue, n=1)
    assert [r["contact_email"] for r in staged] == ["ds@publix.com"]
    staged, _ = _stage(_hiring_cfg(tmp_path), queue)
    assert [r["contact_email"] for r in staged] == ["ds@publix.com", "ds@google.com",
                                                    "hr@publix.com"]


def test_hiring_people_are_not_held_back_by_tier_share(tmp_path):
    # Tier 3's share is 10%, but people at a hiring company still fill the day first.
    queue = [_person(f"ds{i}@publix.com", company="Publix") for i in range(4)]
    queue += [_person(f"ds{i}@google.com", company="Google") for i in range(4)]
    staged, _ = _stage(_hiring_cfg(tmp_path), queue, n=4)
    assert all(r["company"] == "Publix" for r in staged)


def test_hiring_set_and_todo(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cfg["files"]["open_roles"] = str(tmp_path / "open_roles.csv")
    common.load_open_roles.cache_clear()
    common.write_db(cfg["files"]["queue"], FIELDS, [_person("a@publix.com", company="Publix")])

    def run(*a, **kw):
        ns = dict(action="set", company=None, status=None, link=None, role=None,
                  domain=None, n=None)
        ns.update(kw)
        outreach_cli.cmd_hiring(argparse.Namespace(**ns), cfg)

    todo = [c for c, _, _ in outreach_cli.hiring_todo(cfg)]
    assert todo[0] == "Publix"                         # company with people in the queue first
    with contextlib.suppress(SystemExit):
        run(company="Publix", status="yes")            # a yes needs a posting link
    assert not os.path.exists(cfg["files"]["open_roles"])
    run(company="Publix", status="yes", link="https://jobs/1", role="DS Intern")
    run(company="Google", status="no")
    run(company="Google", status="yes", link="https://jobs/2")      # updated, not duplicated
    _, rows = common.read_rows(cfg["files"]["open_roles"])
    assert sorted((r["company"], r["hiring"], r["domain"]) for r in rows) == [
        ("Google", "yes", "google.com"), ("Publix", "yes", "publix.com")]
    assert common.has_open_roles({"company": "Google"}, cfg)
    todo = [c for c, _, _ in outreach_cli.hiring_todo(cfg)]
    assert "Publix" not in todo and "Google" not in todo and "Meta" in todo
    old = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
    for r in rows:
        r["checked"] = old
    common.write_db(cfg["files"]["open_roles"], list(rows[0]), rows)
    assert "Google" in [c for c, _, _ in outreach_cli.hiring_todo(cfg)]   # re-check weekly
