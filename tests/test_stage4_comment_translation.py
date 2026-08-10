"""Formal Stage 4 translation backfill and gate behavior."""
from __future__ import annotations

import copy
import json
import sys

from extensions.sop_v2 import comment_translation
from extensions.sop_v2.pipeline import stage4_decide

from _fixtures import clean_full


def _candidate():
    return clean_full(
        handle="multilingual",
        _status="collected",
        _stage_error=None,
        _reject_reason=None,
        _discovery_batch="NEW",
        comments_analyzed=1,
        valid_comments=1,
        comment_sample=["Où acheter ?"],
        comment_records=[
            {
                "username": "buyer",
                "text": "Où acheter ?",
                "post_url": "https://www.instagram.com/p/POST/",
            }
        ],
    )


def _argv(out_path, *extra):
    return [
        "stage4_decide",
        "--batch-id", "NEW",
        "--track", "paid",
        "--out", str(out_path),
        "--no-xlsx",
        *extra,
    ]


def _install_valid_translation(candidate):
    comment_translation.translate_candidate(
        candidate,
        transport=lambda items: {
            "translations": [
                {
                    "id": item["id"],
                    "source_language": "fr",
                    "translated_text": "在哪里买？",
                }
                for item in items
            ]
        },
        model="qwen3.5:4b",
    )
    for row in candidate["comment_translations"]:
        row["provider"] = "ollama"
    candidate["comment_translation_summary"]["provider"] = "ollama"


def test_strict_validation_failure_blocks_without_calling_translation_or_advance(
    tmp_path, monkeypatch
):
    candidate = _candidate()
    advances = []
    monkeypatch.setattr(
        stage4_decide.cc, "export_all_with_data", lambda _scope: [candidate]
    )
    monkeypatch.setattr(
        stage4_decide.cc, "advance", lambda *args: advances.append(args)
    )
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda _batch: {})

    monkeypatch.setattr(
        comment_translation,
        "translate_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict validation must not call translation transport")
        ),
    )
    out_path = tmp_path / "decisions.json"
    monkeypatch.setattr(
        sys, "argv", _argv(out_path, "--strict-comment-translations")
    )

    assert stage4_decide.main() == 1
    assert advances == []
    assert not out_path.exists()


def test_strict_validation_is_manifested_and_preserves_stage_translation_json(
    tmp_path, monkeypatch
):
    candidate = _candidate()
    _install_valid_translation(candidate)
    translation_before = copy.deepcopy(
        {
            "comment_translations": candidate["comment_translations"],
            "comment_translation_summary": candidate["comment_translation_summary"],
            "translated_intent_comments": candidate["translated_intent_comments"],
            "translated_intent_by_grade": candidate["translated_intent_by_grade"],
        }
    )
    advances = []
    monkeypatch.setattr(
        stage4_decide.cc, "export_all_with_data", lambda _scope: [candidate]
    )
    monkeypatch.setattr(
        stage4_decide.cc,
        "advance",
        lambda handle, status, value: advances.append((handle, status, value)),
    )
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda _batch: {})

    monkeypatch.setattr(
        comment_translation,
        "translate_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict validation must not call translation transport")
        ),
    )
    out_path = tmp_path / "decisions.json"
    monkeypatch.setattr(
        sys, "argv", _argv(out_path, "--strict-comment-translations")
    )

    assert stage4_decide.main() == 0
    result = json.loads(out_path.read_text(encoding="utf-8"))
    manifested = result["manifest"]["comment_translation"]
    assert manifested["validation_mode"] == "read_only"
    assert manifested["valid_candidate_count"] == 1
    assert manifested["invalid_candidate_count"] == 0
    assert manifested["translated_count"] == 1
    assert advances[0][0:2] == ("multilingual", "decided")
    persisted = advances[0][2]
    for field, value in translation_before.items():
        assert persisted[field] == value
        assert candidate[field] == value


def test_explicit_draft_translation_remains_compatible(tmp_path, monkeypatch):
    candidate = _candidate()
    advances = []
    monkeypatch.setattr(
        stage4_decide.cc, "export_all_with_data", lambda _scope: [candidate]
    )
    monkeypatch.setattr(
        stage4_decide.cc,
        "advance",
        lambda handle, status, value: advances.append((handle, status, value)),
    )
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda _batch: {})
    calls = []
    expected_summary = {
        "candidate_count": 1,
        "requested_count": 1,
        "translated_count": 1,
        "failed_count": 0,
        "source_unavailable_count": 0,
        "source_languages": {"fr": 1},
        "provider": "ollama",
        "model": "qwen3.5:4b",
    }

    def draft_translation(candidates, **kwargs):
        calls.append(kwargs)
        candidates[0]["comment_translations"] = [{"translated_zh": "在哪里买？"}]
        candidates[0]["comment_translation_summary"] = {"status": "complete"}
        return expected_summary

    monkeypatch.setattr(
        comment_translation, "translate_candidates", draft_translation
    )
    out_path = tmp_path / "decisions.json"
    monkeypatch.setattr(sys, "argv", _argv(out_path, "--translate-comments"))

    assert stage4_decide.main() == 0
    assert len(calls) == 1
    assert advances[0][2]["comment_translations"][0]["translated_zh"] == "在哪里买？"
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result["manifest"]["comment_translation"] == expected_summary


def test_translate_and_strict_are_unconditionally_mutually_exclusive(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda _scope: (_ for _ in ()).throw(
            AssertionError("formal conflict must fail before reading candidates")
        ),
    )
    monkeypatch.setattr(
        comment_translation,
        "translate_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("formal conflict must fail before translation")
        ),
    )
    out_path = tmp_path / "decisions.json"
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(
            out_path,
            "--strict-comment-translations",
            "--translate-comments",
        ),
    )

    assert stage4_decide.main() == 1
    assert not out_path.exists()


def test_require_round_contract_also_makes_inline_translation_formal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda _scope: (_ for _ in ()).throw(
            AssertionError("formal conflict must fail before reading candidates")
        ),
    )
    monkeypatch.setattr(
        comment_translation,
        "translate_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("formal conflict must fail before translation")
        ),
    )
    out_path = tmp_path / "decisions.json"
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(out_path, "--require-round-contract", "--translate-comments"),
    )

    assert stage4_decide.main() == 1
    assert not out_path.exists()
