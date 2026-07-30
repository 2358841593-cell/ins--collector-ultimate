import hashlib
import json
import sys
from pathlib import Path

import pytest

from extensions.sop_v2.pipeline import stage4_decide
from extensions.sop_v2.pipeline import modash_cdp

from _fixtures import clean_full


def _handle_set_sha(handles):
    return hashlib.sha256("\n".join(sorted(handles)).encode("utf-8")).hexdigest()


def _write_manifest(path, *, rows=None, **overrides):
    rows = rows if rows is not None else [
        {
            "handle": "old_keep",
            "origin_batch": "OLD",
            "needs_pipeline_retry": False,
        }
    ]
    handles = {
        str(row.get("handle") or "").strip().lstrip("@").lower()
        for row in rows
        if row.get("handle")
    }
    value = {
        "schema_version": 1,
        "next_batch_id": "NEW",
        "counts": {
            "source_candidates": 2 + len(rows),
            "client_final": 2,
            "carryover": len(rows),
            "pipeline_retry": sum(
                row.get("needs_pipeline_retry") is True for row in rows
            ),
        },
        "carryover_handle_set_sha256": _handle_set_sha(handles),
        "source_batches": ["OLD", "NEW"],
        "retry_handles": [
            row["handle"] for row in rows if row.get("needs_pipeline_retry") is True
        ],
        "carryover": rows,
    }
    value.update(overrides)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _candidate(handle, batch, status="decided", **overrides):
    return clean_full(
        handle=handle,
        _status=status,
        _reject_reason=None,
        _discovery_batch=batch,
        **overrides,
    )


def _strict_deep_complete(**overrides):
    value = {
        "comments_read": True,
        "sampled_posts": [
            {
                "url": f"https://www.instagram.com/p/POST{index}/",
                "like_count": 10 + index,
                "comment_count": 0,
            }
            for index in range(10)
        ],
        "comments_analyzed": 0,
        "valid_comments": 0,
        "deep_target_posts": 10,
        "deep_available_posts": 10,
        "deep_successful_posts": 10,
        "deep_failed_posts": [],
        "deep_metric_missing_posts": [],
        "comment_attempted_posts": 10,
        "comment_completed_posts": 10,
        "comment_failed_posts": [],
        "deep_collection_status": "complete",
    }
    value.update(overrides)
    return value


def test_retry_decided_with_complete_current_evidence_is_idempotently_ready(
    tmp_path, monkeypatch
):
    rows = [
        {
            "handle": "old_retry",
            "origin_batch": "OLD",
            "needs_pipeline_retry": True,
        }
    ]
    manifest_path = _write_manifest(tmp_path / "carryover.json", rows=rows)
    candidate = _candidate(
        "old_retry",
        "OLD",
        "decided",
        _stage_error=None,
        **_strict_deep_complete(),
    )
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda scope: [candidate],
    )

    first, first_meta = stage4_decide._export_with_carryover(
        manifest_path,
        "NEW",
        target_posts=10,
        require_full_deep=True,
    )
    second, second_meta = stage4_decide._export_with_carryover(
        manifest_path,
        "NEW",
        target_posts=10,
        require_full_deep=True,
    )

    assert [row["handle"] for row in first] == ["old_retry"]
    assert [row["handle"] for row in second] == ["old_retry"]
    assert first_meta["retry_pending_count"] == 0
    assert second_meta["retry_pending_count"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {**_strict_deep_complete(), "_stage_error": "deep_incomplete"},
    ],
)
def test_retry_decided_with_incomplete_or_errored_current_evidence_stays_pending(
    tmp_path, monkeypatch, overrides
):
    rows = [
        {
            "handle": "old_retry",
            "origin_batch": "OLD",
            "needs_pipeline_retry": True,
        }
    ]
    manifest_path = _write_manifest(tmp_path / "carryover.json", rows=rows)
    candidate = _candidate("old_retry", "OLD", "decided", **overrides)
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda scope: [candidate],
    )

    _, meta = stage4_decide._export_with_carryover(
        manifest_path,
        "NEW",
        target_posts=10,
        require_full_deep=True,
    )

    assert meta["retry_pending_handles"] == ["old_retry"]
    assert meta["retry_pending_count"] == 1


def test_stage4_mixes_exact_carryover_with_current_batch_only(
    tmp_path, monkeypatch
):
    manifest_path = _write_manifest(tmp_path / "carryover.json")
    out_path = tmp_path / "decisions.json"
    exported = [
        _candidate("old_keep", "OLD"),
        _candidate("old_client_final", "OLD"),
        _candidate("old_not_listed", "OLD"),
        _candidate("history", "HISTORY"),
        _candidate("new_one", "NEW", "collected"),
    ]
    export_scopes = []
    advances = []

    def fake_export(scope):
        export_scopes.append(scope)
        return exported

    monkeypatch.setattr(stage4_decide.cc, "export_all_with_data", fake_export)
    monkeypatch.setattr(
        stage4_decide.cc,
        "advance",
        lambda handle, status, cand: advances.append(
            (handle, status, cand["_discovery_batch"])
        ),
    )
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda batch_id: {})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id",
            "NEW",
            "--track",
            "paid",
            "--carryover-manifest",
            str(manifest_path),
            "--out",
            str(out_path),
            "--no-xlsx",
            "--generated-at",
            "2026-01-01T00:00:00+0000",
        ],
    )

    assert stage4_decide.main() == 0
    result = json.loads(out_path.read_text(encoding="utf-8"))

    assert export_scopes == [["OLD", "NEW"]]
    assert [row["handle"] for row in result["candidates"]] == [
        "old_keep",
        "new_one",
    ]
    assert {
        row["handle"]: row["_discovery_batch"] for row in result["candidates"]
    } == {"old_keep": "OLD", "new_one": "NEW"}
    assert set(result["manifest"]["batches"]) == {"OLD", "NEW"}
    assert result["manifest"]["candidate_count"] == 2
    assert result["manifest"]["carryover_count"] == 1
    assert result["manifest"]["new_batch_candidate_count"] == 1
    assert result["manifest"]["retry_pending_count"] == 0
    assert result["manifest"]["allow_incomplete_carryover"] is False
    assert result["manifest"]["carryover_manifest"] == {
        "file": str(manifest_path.resolve()),
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    assert set(advances) == {
        ("old_keep", "decided", "OLD"),
        ("new_one", "decided", "NEW"),
    }


def test_incomplete_carryover_blocks_by_default_and_explicit_flag_is_draft(
    tmp_path, monkeypatch
):
    rows = [
        {
            "handle": "old_retry",
            "origin_batch": "OLD",
            "needs_pipeline_retry": True,
        }
    ]
    manifest_path = _write_manifest(tmp_path / "carryover.json", rows=rows)
    out_path = tmp_path / "decisions.json"
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda scope: [_candidate("old_retry", "OLD", "qualified")],
    )
    monkeypatch.setattr(stage4_decide.cc, "advance", lambda *args: None)
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda batch_id: {})
    base_argv = [
        "stage4_decide",
        "--batch-id",
        "NEW",
        "--track",
        "paid",
        "--carryover-manifest",
        str(manifest_path),
        "--out",
        str(out_path),
        "--no-xlsx",
    ]

    monkeypatch.setattr(sys, "argv", base_argv)
    assert stage4_decide.main() == 1
    assert not out_path.exists()

    monkeypatch.setattr(
        sys, "argv", [*base_argv, "--allow-incomplete-carryover"]
    )
    assert stage4_decide.main() == 0
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result["manifest"]["retry_pending_count"] == 1
    assert result["manifest"]["allow_incomplete_carryover"] is True
    # Even with no NEW candidate yet, NEW is the review batch and must remain ingestible.
    assert result["manifest"]["batches"] == ["NEW", "OLD"]
    assert result["candidates"][0]["_discovery_batch"] == "OLD"


def test_modash_cdp_only_receives_candidates_missing_core_fields(
    tmp_path, monkeypatch
):
    rows = [
        {
            "handle": handle,
            "origin_batch": "OLD",
            "needs_pipeline_retry": False,
        }
        for handle in ("old_complete", "old_missing")
    ]
    manifest_path = _write_manifest(tmp_path / "carryover.json", rows=rows)
    out_path = tmp_path / "decisions.json"
    exported = [
        _candidate("old_complete", "OLD"),
        _candidate("old_missing", "OLD", fake_pct=None),
        _candidate("new_complete", "NEW", "collected"),
        _candidate("new_missing", "NEW", "collected", top_audience_country=None),
    ]
    received = []

    monkeypatch.setattr(
        stage4_decide.cc, "export_all_with_data", lambda scope: exported
    )
    monkeypatch.setattr(stage4_decide.cc, "advance", lambda *args: None)
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda batch_id: {})

    def fake_enrich(cands, query, filters, cdp_url, cache_dir=None):
        received.extend(cand["handle"] for cand in cands)
        return {"matched": 0, "total": len(cands)}

    monkeypatch.setattr(modash_cdp, "enrich_via_cdp", fake_enrich)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id",
            "NEW",
            "--track",
            "paid",
            "--carryover-manifest",
            str(manifest_path),
            "--modash-cdp",
            "--out",
            str(out_path),
            "--no-xlsx",
        ],
    )

    assert stage4_decide.main() == 0
    assert received == ["old_missing", "new_missing"]
    assert "old_complete" not in received
    assert "new_complete" not in received


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda value: value.update(schema_version=2),
            "schema_version",
        ),
        (
            lambda value: value.update(next_batch_id="WRONG"),
            "next_batch_id",
        ),
        (
            lambda value: value["counts"].update(carryover=2),
            "counts.carryover",
        ),
        (
            lambda value: value["carryover"].append(
                {
                    "handle": "@OLD_KEEP",
                    "origin_batch": "OLD",
                    "needs_pipeline_retry": False,
                }
            ),
            "handle 重复",
        ),
        (
            lambda value: value["carryover"][0].pop("origin_batch"),
            "origin_batch",
        ),
    ],
)
def test_manifest_validation_rejects_unsafe_shape(
    tmp_path, mutate, match
):
    path = _write_manifest(tmp_path / "carryover.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(stage4_decide.CarryoverManifestError, match=match):
        stage4_decide._load_carryover_manifest(path, "NEW")


def test_manifest_missing_or_origin_mismatch_stops(tmp_path, monkeypatch):
    path = _write_manifest(tmp_path / "carryover.json")

    monkeypatch.setattr(stage4_decide.cc, "export_all_with_data", lambda scope: [])
    with pytest.raises(stage4_decide.CarryoverManifestError, match="缺失"):
        stage4_decide._export_with_carryover(path, "NEW")

    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda scope: [_candidate("old_keep", "OTHER")],
    )
    with pytest.raises(stage4_decide.CarryoverManifestError, match="来源批次不匹配"):
        stage4_decide._export_with_carryover(path, "NEW")


def test_all_batches_and_carryover_manifest_are_mutually_exclusive(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id",
            "NEW",
            "--track",
            "paid",
            "--all-batches",
            "--carryover-manifest",
            str(tmp_path / "carryover.json"),
            "--out",
            str(tmp_path / "out.json"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        stage4_decide.main()
    assert exc.value.code == 2
