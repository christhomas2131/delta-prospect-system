import contact_enrichment
from contact_enrichment import company_domain, hunter_contacts, normalize_name, title_score


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
