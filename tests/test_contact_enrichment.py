import contact_enrichment
from contact_enrichment import (
    _public_emails,
    company_domain,
    hunter_contacts,
    normalize_name,
    snapshot_contact_records,
    title_score,
)


def test_company_domain_normalizes_urls():
    assert company_domain("https://www.example.com/about") == "example.com"
    assert company_domain("example.com") == "example.com"
    assert company_domain(None) is None


def test_normalize_name_is_stable():
    assert normalize_name("  Jane O'Connor-Smith ") == "jane o connor smith"


def test_operational_decision_makers_rank_above_generic_managers():
    assert title_score("Chief Operating Officer", "c_suite") > title_score("Marketing Manager", "manager")
    assert title_score("General Manager Operations", "manager") > title_score("Managing Director")


def test_hunter_returns_two_ranked_contacts(monkeypatch):
    response = {
        "data": {
            "emails": [
                {"value": "manager@example.com", "first_name": "M", "last_name": "Manager", "position": "Marketing Manager", "seniority": "manager"},
                {"value": "coo@example.com", "first_name": "C", "last_name": "Operator", "position": "Chief Operating Officer", "seniority": "executive"},
                {"value": "ops@example.com", "first_name": "O", "last_name": "Leader", "position": "General Manager Operations", "seniority": "manager"},
            ]
        }
    }
    monkeypatch.setattr(contact_enrichment, "_request_json", lambda *args, **kwargs: response)

    contacts = hunter_contacts("example.com", "test-key", limit=2)

    assert [contact["email"] for contact in contacts] == ["coo@example.com", "ops@example.com"]


def test_snapshot_management_contacts_are_normalized_for_contact_table():
    contacts = snapshot_contact_records({
        "management_contacts": [{
            "name": "Alex Operator",
            "title": "Chief Operating Officer",
            "email": "Alex.Operator@Example.com",
            "phone": "+61 2 0000 0000",
            "priority": 1,
        }]
    })

    assert contacts == [{
        "source_provider": "snapshot",
        "source_contact_id": "alex.operator@example.com",
        "full_name": "Alex Operator",
        "title": "Chief Operating Officer",
        "seniority": None,
        "departments": [],
        "email": "alex.operator@example.com",
        "email_status": "available",
        "phone": "+61 2 0000 0000",
        "linkedin_url": None,
        "confidence_score": 0.95,
        "raw_data": {
            "name": "Alex Operator",
            "title": "Chief Operating Officer",
            "email": "Alex.Operator@Example.com",
            "phone": "+61 2 0000 0000",
            "priority": 1,
        },
    }]


def test_public_email_extraction_deduplicates_and_ignores_asset_noise():
    page = """
        <a href="mailto:investors@exampleminerals.com.au">Contact</a>
        INVESTORS@exampleminerals.com.au
        tracking@sentry.io
        logo@exampleminerals.com.au.png
    """

    assert _public_emails(page) == ["investors@exampleminerals.com.au"]
