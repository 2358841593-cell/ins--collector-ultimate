"""Carryover retry selection must not requeue client-final or other-origin assets."""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2.pipeline import audit_collect  # noqa: E402


def _manifest(path: Path) -> Path:
    rows = [
        {
            "handle": "retry_one",
            "origin_batch": "OLD",
            "needs_pipeline_retry": True,
        },
        {
            "handle": "retry_two",
            "origin_batch": "OLD",
            "needs_pipeline_retry": True,
        },
        {
            "handle": "other_origin",
            "origin_batch": "OTHER",
            "needs_pipeline_retry": True,
        },
        {
            "handle": "client_final_not_marked",
            "origin_batch": "OLD",
            "needs_pipeline_retry": False,
        },
    ]
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "counts": {"carryover": len(rows), "pipeline_retry": 3},
                "carryover_handle_set_sha256": hashlib.sha256(
                    "\n".join(sorted(row["handle"] for row in rows)).encode("utf-8")
                ).hexdigest(),
                "retry_handles": ["retry_one", "retry_two", "other_origin"],
                "carryover": rows,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_retry_manifest_selects_only_requested_origin(tmp_path):
    path = _manifest(tmp_path / "carryover.json")
    assert audit_collect.retry_handles_for_batch(path, "OLD") == {
        "retry_one",
        "retry_two",
    }
    assert audit_collect.retry_handles_for_batch(path, "OTHER") == {"other_origin"}


def test_requeue_uses_manifest_intersection_not_every_incomplete_item(
    tmp_path, monkeypatch
):
    path = _manifest(tmp_path / "carryover.json")
    incomplete = [
        {"handle": "retry_one", "status": "qualified", "reasons": ["错误:proxy"]},
        {"handle": "retry_two", "status": "decided", "reasons": ["帖子全失败"]},
        {
            "handle": "client_final_not_marked",
            "status": "decided",
            "reasons": ["帖子全失败"],
        },
    ]
    requeue = mock.Mock(return_value=2)
    monkeypatch.setattr(audit_collect.cc, "incomplete_items", lambda batch: incomplete)
    monkeypatch.setattr(audit_collect.cc, "requeue_for_recollect", requeue)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_collect",
            "--batch-id",
            "OLD",
            "--retry-manifest",
            str(path),
            "--requeue",
        ],
    )

    assert audit_collect.main() == 0
    requeue.assert_called_once_with(["retry_one", "retry_two"])


def test_retry_manifest_rejects_flag_list_drift(tmp_path):
    path = _manifest(tmp_path / "carryover.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["retry_handles"].pop()
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(audit_collect.RetryManifestError, match="不一致"):
        audit_collect.retry_handles_for_batch(path)


def test_retry_manifest_rejects_non_string_handle(tmp_path):
    path = _manifest(tmp_path / "carryover.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["carryover"][0]["handle"] = 123
    payload["retry_handles"][0] = 123
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(audit_collect.RetryManifestError, match="必须是 string"):
        audit_collect.retry_handles_for_batch(path)
