from contact_enrichment import company_domain, normalize_name, title_score


def test_company_domain_normalizes_urls():
    assert company_domain("https://www.example.com/about") == "example.com"
    assert company_domain("example.com") == "example.com"
    assert company_domain(None) is None


def test_normalize_name_is_stable():
    assert normalize_name("  Jane O'Connor-Smith ") == "jane o connor smith"


def test_operational_decision_makers_rank_above_generic_managers():
    assert title_score("Chief Operating Officer", "c_suite") > title_score("Marketing Manager", "manager")
    assert title_score("General Manager Operations", "manager") > title_score("Managing Director")
