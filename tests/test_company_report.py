from company_report import build_csv, report_rows, safe_filename


def _report():
    return {
        "prospect": {
            "company_name": "Example Mining Ltd",
            "ticker": "EXM",
            "prospect_score": 12.5,
            "outreach_hypothesis": "Improve the operating model before project delivery slips.",
        },
        "signals": [{
            "pressure_type": "production",
            "strength": "strong",
            "summary": "Production guidance reduced.",
            "source_title": "Quarterly report",
        }],
        "contacts": [{
            "full_name": "Alex Operator",
            "title": "Chief Operating Officer",
            "email": "alex@example.com",
            "decision_rank": 100,
        }],
        "snapshot": None,
        "v3_analysis": None,
    }


def test_report_csv_carries_intelligence_signals_and_contacts():
    rows = report_rows(_report())
    values = "\n".join(value for row in rows for value in row)

    assert "Improve the operating model" in values
    assert "Production guidance reduced" in values
    assert "Alex Operator" in values
    assert build_csv(_report()).startswith(b"\xef\xbb\xbfSection,Field,Value")


def test_safe_filename_strips_unsafe_characters():
    assert safe_filename("A/B: Mining Ltd") == "A_B_Mining_Ltd"
