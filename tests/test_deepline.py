"""Tests for `outreach from-deepline`: Deepline people search + email waterfall
results -> Emory-alumni leads, verified emails only, people who moved skipped.

Hermetic: in-memory rows, no network or account.
"""
import outreach_cli


def _person(first="Pat", last="Lee", company="Google", title="Data Scientist",
            headline="Data Scientist @ Google", url="https://www.linkedin.com/in/pat"):
    return {"firstname": first, "lastname": last, "headline": headline, "profileUrl": url,
            "lastJobTitle": title, "lastJobStartDate": "06-2024", "lastCompanyName": company,
            "lastCompanyWebsite": "http://www.google.com", "lastCompanySize": "180000"}


def _email(url="https://www.linkedin.com/in/pat", email="pat@google.com", ok="true"):
    return {"linkedin_url": url, "email": email, "email_validated": ok, "domain": "google.com"}


def test_builds_emory_lead_with_users_line():
    leads, skipped = outreach_cli.deepline_to_leads([_person()], [_email()], {})
    assert not skipped
    lead = leads[0]
    assert lead["contact_email"] == "pat@google.com" and lead["email_status"] == "verified"
    assert lead["school"] == "Emory University" and lead["source"] == "deepline"
    assert lead["company_domain"] == "google.com"
    assert lead["personal_line"] == ("I noticed you have also studied in Emory and was genuinely "
                                     "impressed by your work as a Data Scientist at Google.")


def test_title_shortened_and_article():
    leads, _ = outreach_cli.deepline_to_leads(
        [_person(title="Sr Applied Scientist - Ads Ranking, Search")], [_email()], {})
    assert "as an Senior" not in leads[0]["personal_line"]
    assert "as a Senior Applied Scientist at Google." in leads[0]["personal_line"]
    leads, _ = outreach_cli.deepline_to_leads([_person(title="Applied Scientist")], [_email()], {})
    assert "as an Applied Scientist at Google." in leads[0]["personal_line"]


def test_skips_unverified_and_missing_email():
    leads, skipped = outreach_cli.deepline_to_leads(
        [_person(), _person(first="Al", url="https://www.linkedin.com/in/al")],
        [_email(ok="false")], {})
    assert leads == [] and len(skipped) == 2
    assert all("no verified work email" in why for _, why in skipped)


def test_skips_people_whose_headline_says_they_moved():
    moved = _person(headline="Data Scientist, Marketing @Asana | ex-TikTok")
    looking = _person(first="Al", headline="Actively looking for opportunities")
    leads, skipped = outreach_cli.deepline_to_leads([moved, looking], [_email()], {})
    assert leads == []
    assert "asana" in skipped[0][1] and "looking for a job" in skipped[1][1]


def test_keeps_headline_naming_the_same_company_and_strips_nickname():
    p = _person(first="Kailang (Robert)", company="Delta Air Lines",
                headline="Lead Data Scientist @ Delta Air Lines | Emory MSBA")
    leads, skipped = outreach_cli.deepline_to_leads([p], [_email()], {})
    assert not skipped and leads[0]["first_name"] == "Kailang"
