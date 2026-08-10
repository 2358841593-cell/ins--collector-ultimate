import json

import pytest

from extensions.sop_v2.pipeline import modash_golden_lookalikes as golden
from extensions.sop_v2.pipeline import modash_search


def _manifest(batch="B-GOLD", seeds=("seed_a", "seed_b")):
    return modash_search.build_golden_seed_manifest(
        batch, list(seeds), generated_at="2026-08-10T12:00:00+0800"
    )


def _valid_row(handle, *, er=2.5, followers=20_000, description="skincare reviews"):
    return {
        "username": handle,
        "follower_count": followers,
        "engagement_rate": er,
        "is_brand": False,
        "is_private": False,
        "creator_description": description,
        "account_category": "Digital creator",
    }


def _response(seed, rows, total=100):
    return {
        "status": 200,
        "text": json.dumps(
            {
                "results": rows,
                "seedResults": [{"username": seed}],
                "total": total,
            }
        ),
    }


class FakePage:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.waits = []

    def evaluate(self, script, body):
        assert script == golden._FETCH_JS
        key = (body["usernames"][0], body["skip"])
        self.requests.append(body)
        return self.responses[key]

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


@pytest.fixture
def cfg():
    return {
        "track": {
            "paid": {"min_followers": 10_000, "max_followers": 150_000},
            "gifting": {"standard_min": 5_000, "priority_max": 50_000},
        },
        "discovery": {
            "search_er_min": 0.015,
            "niche_keywords": ["skincare", "wellness", "beauty"],
        },
    }


def test_engagement_rate_is_already_percent_not_fraction():
    accepted, reason = golden.quality_candidate(
        _valid_row("accepted", er=1.5),
        seed_handle="seed",
        follower_min=10_000,
        follower_max=150_000,
        er_min_pct=1.5,
        niche_keywords=["skincare"],
    )
    assert reason is None
    assert accepted["er_pct"] == 1.5

    rejected, reason = golden.quality_candidate(
        _valid_row("too_low", er=0.015),
        seed_handle="seed",
        follower_min=10_000,
        follower_max=150_000,
        er_min_pct=1.5,
        niche_keywords=["skincare"],
    )
    assert rejected is None
    assert reason == "er_below_min"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"is_brand": True}, "brand"),
        ({"is_private": True}, "private"),
        ({"is_brand": None}, "account_flags_missing"),
        ({"follower_count": 9_999}, "followers_out_of_range"),
        ({"engagement_rate": None}, "er_missing"),
        ({"creator_description": "only book reviews", "account_category": "Author"}, "off_niche"),
    ],
)
def test_quality_filters_known_bad_rows(changes, expected):
    row = _valid_row("candidate")
    row.update(changes)
    candidate, reason = golden.quality_candidate(
        row,
        seed_handle="seed",
        follower_min=10_000,
        follower_max=150_000,
        er_min_pct=1.5,
        niche_keywords=["skincare", "beauty"],
    )
    assert candidate is None
    assert reason == expected


def test_round_robin_reaches_unique_target_and_preserves_multi_seed_hit(cfg):
    manifest = _manifest()
    output = json.loads(json.dumps(manifest))
    golden._collection_state(
        output,
        track="paid",
        target=2,
        page_size=6,
        max_pages_per_seed=2,
        per_seed_max=4,
        per_page_accept=1,
        round_contract_sha256=None,
    )
    page = FakePage(
        {
            ("seed_a", 0): _response(
                "seed_a",
                [_valid_row("shared"), _valid_row("known_creator")],
            ),
            ("seed_b", 0): _response(
                "seed_b",
                [_valid_row("shared"), _valid_row("candidate_b")],
            ),
        }
    )
    checkpoints = []

    result = golden.collect_with_page(
        page,
        output,
        ["seed_a", "seed_b"],
        cfg=cfg,
        track="paid",
        target=2,
        page_size=6,
        max_pages_per_seed=2,
        per_seed_max=4,
        per_page_accept=1,
        checkpoint=lambda value: checkpoints.append(
            json.loads(json.dumps(value))
        ),
        should_ingest=lambda handle: handle != "known_creator",
        page_delay=0,
    )

    assert result["status"] == "complete"
    assert result["accepted_unique"] == 2
    groups = {row["seed_handle"]: row["candidates"] for row in output["results"]}
    assert [row["handle"] for row in groups["seed_a"]] == ["shared"]
    assert [row["handle"] for row in groups["seed_b"]] == [
        "shared",
        "candidate_b",
    ]
    assert output["collection"]["stats"]["known"] == 1
    assert output["collection"]["stats"]["duplicate_observed"] == 1
    assert len(checkpoints) == 3  # one per request plus the terminal checkpoint


def test_short_run_persists_explicit_status(cfg):
    manifest = _manifest(seeds=("seed_a",))
    output = json.loads(json.dumps(manifest))
    golden._collection_state(
        output,
        track="paid",
        target=2,
        page_size=6,
        max_pages_per_seed=1,
        per_seed_max=4,
        per_page_accept=1,
        round_contract_sha256=None,
    )
    page = FakePage({("seed_a", 0): _response("seed_a", [], total=0)})

    result = golden.collect_with_page(
        page,
        output,
        ["seed_a"],
        cfg=cfg,
        track="paid",
        target=2,
        page_size=6,
        max_pages_per_seed=1,
        per_seed_max=4,
        per_page_accept=1,
        checkpoint=lambda _value: None,
        should_ingest=lambda _handle: True,
        page_delay=0,
    )

    assert result["status"] == "short"
    assert output["collection"]["status"] == "short"


def test_resume_rejects_different_cohort(tmp_path):
    first = _manifest(batch="B-ONE")
    second = _manifest(batch="B-TWO")
    out = tmp_path / "completed.json"
    out.write_text(json.dumps(first), encoding="utf-8")

    with pytest.raises(golden.LookalikeCollectionError, match="不属于当前"):
        golden.load_or_initialize_output(
            second,
            [row["handle"] for row in second["seeds"]],
            out,
        )


def test_atomic_checkpoint_is_valid_json(tmp_path):
    out = tmp_path / "nested" / "completed.json"
    golden.atomic_write_json(out, {"hello": "世界"})
    assert json.loads(out.read_text(encoding="utf-8")) == {"hello": "世界"}
    assert not list(out.parent.glob("*.tmp"))


def test_response_seed_mismatch_is_not_accepted():
    rows, total = golden.validate_response(
        _response("somebody_else", [_valid_row("candidate")]),
        "expected_seed",
    )
    assert rows == []
    assert total is None


@pytest.mark.parametrize("bad_total", [None, "12", 1.5, -1, True])
def test_response_total_is_strictly_validated(bad_total):
    response = _response("seed", [], total=bad_total)
    with pytest.raises(golden.LookalikeCollectionError, match="total"):
        golden.validate_response(response, "seed")


def test_formal_cli_stops_before_browser_without_contract(tmp_path):
    manifest = _manifest(seeds=("seed_a",))
    source = tmp_path / "manifest.json"
    out = tmp_path / "completed.json"
    source.write_text(json.dumps(manifest), encoding="utf-8")

    result = golden.main(
        [
            "--manifest",
            str(source),
            "--out",
            str(out),
            "--track",
            "paid",
            "--require-round-contract",
        ]
    )

    assert result == 2
    assert not out.exists()


def test_playwright_failure_is_checkpointed_and_cdp_browser_is_not_closed(
    tmp_path, monkeypatch, cfg
):
    manifest = _manifest(seeds=("seed_a",))
    source = tmp_path / "manifest.json"
    out = tmp_path / "completed.json"
    source.write_text(json.dumps(manifest), encoding="utf-8")

    class ExplodingPage:
        url = "https://app.modash.io/discovery/instagram"

        def evaluate(self, _script, _body):
            raise RuntimeError("transport dropped")

    class Context:
        pages = [ExplodingPage()]

    class Browser:
        contexts = [Context()]
        close_calls = 0

        def close(self):
            self.close_calls += 1

    browser = Browser()

    class Chromium:
        def connect_over_cdp(self, url):
            assert url == "http://127.0.0.1:9222"
            return browser

    class Playwright:
        chromium = Chromium()

    class Manager:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args):
            return False

    import playwright.sync_api

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", Manager)
    monkeypatch.setattr(golden, "load_config", lambda: cfg)

    result = golden.main(
        [
            "--manifest", str(source),
            "--out", str(out),
            "--target", "1",
            "--page-delay", "0",
        ]
    )

    assert result == 2
    checkpoint = json.loads(out.read_text(encoding="utf-8"))
    assert checkpoint["collection"]["status"] == "error"
    assert "RuntimeError" in checkpoint["collection"]["errors"][-1]["error"]
    assert browser.close_calls == 0
