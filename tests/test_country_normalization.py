import pytest

from extensions.sop_v2.countries import normalize_country_code
from extensions.sop_v2.pipeline.modash_cdp import parse_report
from extensions.sop_v2.pipeline.modash_enrich import parse_modash_csv


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Ukraine", "UA"),
        ("United Kingdom", "UK"),
        ("UK", "UK"),
        ("GB", "UK"),
        ("South Korea", "KR"),
        ("Russia", "RU"),
        ("Argentina", "AR"),
        ("Syria", "SY"),
        ("Iran", "IR"),
        ("Turkey", "TR"),
        ("Türkiye", "TR"),
    ],
)
def test_normalize_country_code(value, expected):
    assert normalize_country_code(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "Atlantis", "South Pole"])
def test_unknown_country_name_is_not_guessed(value):
    assert normalize_country_code(value) is None


def test_modash_cdp_normalizes_creator_and_audience_country():
    report = {
        "profile": {
            "profileData": {
                "profile": {"username": "country_test"},
                "location": {"country": {"name": "South Korea"}},
                "audience": {
                    "geoCountries": [
                        {"name": "Ukraine", "weight": 0.8},
                        {"name": "Germany", "weight": 0.2},
                    ]
                },
            }
        }
    }

    parsed = parse_report(report, "country_test")

    assert parsed["creator_country"] == "KR"
    assert parsed["top_audience_country"] == "UA"


def test_modash_cdp_preserves_unknown_creator_country_as_review_evidence():
    report = {
        "profile": {
            "profileData": {
                "profile": {"username": "country_test"},
                "location": {"country": {"name": "Atlantis"}},
            }
        }
    }

    parsed = parse_report(report, "country_test")

    assert parsed["creator_country"] == "Atlantis"


def test_modash_csv_normalizes_names_without_prefix_guessing(tmp_path):
    csv_path = tmp_path / "modash.csv"
    csv_path.write_text(
        "username,creator country,top audience country\n"
        "valid_user,Ukraine,South Korea\n"
        "unknown_user,Atlantis,United Kingdom\n",
        encoding="utf-8",
    )

    rows = parse_modash_csv(str(csv_path))

    assert rows["valid_user"] == {
        "creator_country": "UA",
        "top_audience_country": "KR",
    }
    assert rows["unknown_user"] == {
        "creator_country": None,
        "top_audience_country": "UK",
    }
