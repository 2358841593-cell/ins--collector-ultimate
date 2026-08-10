"""Language-agnostic comment translation and failure-preservation tests."""
from __future__ import annotations

import copy

from extensions.sop_v2 import comment_translation
from extensions.sop_v2 import comments


def _transport_for(mapping):
    def transport(items):
        return {
            "translations": [
                {
                    "id": item["id"],
                    "source_language": mapping[item["text"]][0],
                    "translated_text": mapping[item["text"]][1],
                }
                for item in items
            ]
        }

    return transport


def test_any_language_becomes_structured_chinese_and_reuses_chinese_semantics():
    originals = [
        ("alice", "Où puis-je acheter ce sérum ?", "fr", "我在哪里可以买这个精华？"),
        ("buyer", "이거 얼마예요?", "ko", "这个多少钱？"),
        ("ira", "Я уже заказала", "ru", "我已经下单了"),
        ("noor", "هل يناسب البشرة الحساسة؟", "ar", "适合敏感肌吗？"),
    ]
    candidate = {
        "handle": "creator",
        "comments_analyzed": 4,
        "comment_records": [
            {
                "username": username,
                "text": original,
                "post_url": "https://www.instagram.com/p/POST/",
            }
            for username, original, _language, _translated in originals
        ],
    }
    mapping = {
        original: (language, translated)
        for _username, original, language, translated in originals
    }

    summary = comment_translation.translate_candidate(
        candidate,
        transport=_transport_for(mapping),
        model="qwen3.5:4b",
    )

    assert summary["translated_count"] == 4
    assert summary["failed_count"] == 0
    records = candidate["comment_translations"]
    assert [row["original_text"] for row in records] == [row[1] for row in originals]
    assert [row["translated_zh"] for row in records] == [row[3] for row in originals]
    assert [row["source_language"] for row in records] == ["fr", "ko", "ru", "ar"]
    assert all(row["status"] == "translated" for row in records)
    assert all(row["model"] == "qwen3.5:4b" for row in records)
    assert all(row["translator_version"] == 1 for row in records)
    assert [row["translated_intent_grade"] for row in records] == [
        "high", "high", "high", "medium"
    ]
    assert candidate["translated_intent_by_grade"] == {
        "high": 3,
        "medium": 1,
        "low": 0,
    }


def test_partial_model_output_keeps_failed_original_as_explicit_record():
    candidate = {
        "comments_analyzed": 2,
        "intent_posts": [
            {
                "post_url": "https://www.instagram.com/p/P/",
                "intent_comments": [
                    {"username": "a", "text": "Où acheter ?", "grade": "high", "grade_zh": "高"},
                    {"username": "b", "text": "Где ссылка?", "grade": "high", "grade_zh": "高"},
                ],
            }
        ],
    }

    def partial(items):
        return {
            "translations": [
                {
                    "id": item["id"],
                    "source_language": "fr",
                    "translated_text": "在哪里买？",
                }
                for item in items
                if item["text"] == "Où acheter ?"
            ]
        }

    summary = comment_translation.translate_candidate(candidate, transport=partial)

    assert summary["translated_count"] == 1
    assert summary["failed_count"] == 1
    failed = candidate["comment_translations"][1]
    assert failed["original_text"] == "Где ссылка?"
    assert failed["translated_zh"] is None
    assert failed["status"] == "failed"
    assert failed["translation_error"] == "missing_model_result"
    assert len(candidate["translated_intent_comments"]) == 2


def test_provider_failure_never_drops_or_relabels_real_comments_as_empty():
    candidate = {
        "comments_analyzed": 1,
        "comment_sample": ["Որտե՞ղ կարող եմ գնել սա"],
    }

    def broken(_items):
        raise RuntimeError("secret provider response must not leak")

    summary = comment_translation.translate_candidate(candidate, transport=broken)

    assert summary["requested_count"] == 1
    assert summary["translated_count"] == 0
    assert summary["failed_count"] == 1
    assert candidate["comments_analyzed"] == 1
    assert candidate["comment_sample"] == ["Որտե՞ղ կարող եմ գնել սա"]
    record = candidate["comment_translations"][0]
    assert record["original_text"] == "Որտե՞ղ կարող եմ գնել սա"
    assert record["status"] == "failed"
    assert "secret" not in record["translation_error"]


def test_successful_content_addressed_translation_is_reused_without_llm_call():
    candidate = {"comments_analyzed": 1, "comment_sample": ["Où acheter ?"]}
    first = _transport_for({"Où acheter ?": ("fr", "在哪里买？")})
    comment_translation.translate_candidate(candidate, transport=first)

    def must_not_run(_items):
        raise AssertionError("cached translation should be reused")

    summary = comment_translation.translate_candidate(candidate, transport=must_not_run)

    assert summary["translated_count"] == 1
    assert candidate["comment_translations"][0]["translated_zh"] == "在哪里买？"


def test_legacy_nonzero_comments_without_saved_source_is_not_called_empty():
    candidate = {"comments_analyzed": 90, "valid_comments": 89, "comment_sample": []}

    summary = comment_translation.translate_candidate(candidate, transport=lambda _: [])

    assert summary["requested_count"] == 0
    assert summary["source_unavailable_count"] == 1
    assert candidate["comment_translation_summary"]["status"] == "source_unavailable"
    assert candidate["comment_translation_summary"]["source_unavailable"] is True


def test_chinese_translation_rules_cover_intent_and_generic_content_praise():
    assert comments.grade_intent("我在哪里可以买这个精华？") == "high"
    assert comments.grade_intent("这个值得买吗，适合敏感肌吗？") == "medium"
    assert comments.grade_intent("已经种草了，我想试试") == "low"
    assert comments.grade_intent("你的内容很棒，继续加油") is None
    assert comments.is_low_quality("很棒的内容") is True


def test_translates_all_saved_comments_and_rebases_scoring_metrics():
    originals = [f"普通评论 {index}" for index in range(9)] + ["第十条：哪里可以买？"]
    candidate = {
        "comments_analyzed": 10,
        "valid_comments": 10,
        "low_quality_ratio": 0.0,
        "high_intent_count": 0,
        "high_intent_ratio": 0.0,
        "intent_by_grade": {"high": 0, "medium": 0, "low": 0},
        "comment_records": [
            {"username": f"u{index}", "text": text, "post_url": f"https://ig/p/{index}/"}
            for index, text in enumerate(originals)
        ],
    }

    def transport(items):
        return {
            "translations": [
                {
                    "id": item["id"],
                    "source_language": "fr",
                    "translated_text": (
                        "在哪里买这个产品？"
                        if item["text"] == originals[-1]
                        else "很棒的内容"
                    ),
                }
                for item in items
            ]
        }

    summary = comment_translation.translate_candidate(candidate, transport=transport)

    assert summary["requested_count"] == 10
    assert len(candidate["comment_translations"]) == 10
    assert candidate["translated_intent_comments"][0]["original_text"] == originals[-1]
    assert candidate["high_intent_count"] == 1
    assert candidate["high_intent_ratio"] == 100.0
    assert candidate["valid_comments"] == 1
    assert candidate["low_quality_ratio"] == 90.0
    assert candidate["comment_translation_summary"]["semantic_coverage_complete"] is True


def test_partial_saved_source_does_not_overwrite_quality_metrics():
    candidate = {
        "comments_analyzed": 20,
        "valid_comments": 18,
        "low_quality_ratio": 10.0,
        "comment_sample": ["Où acheter ?"],
    }
    comment_translation.translate_candidate(
        candidate,
        transport=_transport_for({"Où acheter ?": ("fr", "在哪里买？")}),
    )

    assert candidate["high_intent_count"] == 1
    assert candidate["high_intent_ratio"] == 5.6
    assert candidate["valid_comments"] == 18
    assert candidate["low_quality_ratio"] == 10.0
    assert candidate["comment_translation_summary"]["semantic_coverage_complete"] is False


def test_large_batch_failure_is_bisected_without_losing_good_comments():
    candidate = {
        "comments_analyzed": 4,
        "valid_comments": 4,
        "comment_sample": ["a", "b", "c", "d"],
    }
    calls = []

    def transport(items):
        calls.append([item["text"] for item in items])
        if len(items) > 1:
            raise RuntimeError("oversized")
        item = items[0]
        return {
            "translations": [{
                "id": item["id"],
                "source_language": "fr",
                "translated_text": f"译文-{item['text']}",
            }]
        }

    summary = comment_translation.translate_candidate(
        candidate, transport=transport, batch_size=40
    )

    assert summary["translated_count"] == 4
    assert summary["failed_count"] == 0
    assert calls[0] == ["a", "b", "c", "d"]
    assert sum(len(call) == 1 for call in calls) == 4


def _persisted_candidate(handle: str, count: int) -> dict:
    candidate = {
        "handle": handle,
        "comments_analyzed": count,
        "comment_records": [
            {
                "username": f"u{index}",
                "text": f"commentaire {handle} {index}",
                "post_url": f"https://www.instagram.com/p/{handle}-{index}/",
            }
            for index in range(count)
        ],
    }

    def transport(items):
        return {
            "translations": [
                {
                    "id": item["id"],
                    "source_language": "fr",
                    "translated_text": f"这是完整译文 {item['text']}",
                }
                for item in items
            ]
        }

    comment_translation.translate_candidate(
        candidate,
        transport=transport,
        model="qwen3.5:4b",
        source_limit=max(1, count),
    )
    for row in candidate["comment_translations"]:
        row["provider"] = "ollama"
    candidate["comment_translation_summary"]["provider"] = "ollama"
    return candidate


def test_strict_validator_accepts_skin6_sized_9720_cache_without_mutation():
    cohort = [_persisted_candidate(f"creator{index}", 81) for index in range(120)]
    before = copy.deepcopy(cohort)

    summary = comment_translation.validate_translation_cohort(cohort)

    assert cohort == before
    assert summary["validation_mode"] == "read_only"
    assert summary["candidate_count"] == 120
    assert summary["valid_candidate_count"] == 120
    assert summary["invalid_candidate_count"] == 0
    assert summary["requested_count"] == 9_720
    assert summary["translated_count"] == 9_720
    assert summary["failed_count"] == 0
    assert summary["source_unavailable_count"] == 0
    assert summary["failures"] == []


def test_strict_validator_rejects_source_limit_truncation():
    candidate = _persisted_candidate("truncated", 5)
    # Recreate the persisted bundle exactly as an unsafe Stage 4 rerun with a
    # lower source limit would have done.
    candidate.pop("comment_translations")
    candidate.pop("comment_translation_summary")

    def transport(items):
        return {
            "translations": [
                {
                    "id": item["id"],
                    "source_language": "fr",
                    "translated_text": f"译文 {item['text']}",
                }
                for item in items
            ]
        }

    comment_translation.translate_candidate(
        candidate,
        transport=transport,
        model="qwen3.5:4b",
        source_limit=3,
    )
    for row in candidate["comment_translations"]:
        row["provider"] = "ollama"
    candidate["comment_translation_summary"]["provider"] = "ollama"

    result = comment_translation.validate_candidate_translations(candidate)

    assert result["valid"] is False
    assert any("in_sample 数量必须等于 comments_analyzed" in reason
               for reason in result["failures"])
    assert any("存在截断" in reason for reason in result["failures"])


def test_strict_validator_accepts_complete_offline_cache_union_provenance():
    candidate = _persisted_candidate("union", 4)
    source_pairs = {"ollama:qwen3.5:4b": 0, "anthropic:claude-cache": 0}
    for index, row in enumerate(candidate["comment_translations"]):
        source_provider, source_model = (
            ("ollama", "qwen3.5:4b")
            if index % 2 == 0
            else ("anthropic", "claude-cache")
        )
        row.update(
            {
                "provider": "offline-cache-union",
                "model": "valid-cached-translations-v1",
                "cache_source_provider": source_provider,
                "cache_source_model": source_model,
            }
        )
        source_pairs[f"{source_provider}:{source_model}"] += 1
    candidate["comment_translation_summary"].update(
        {
            "provider": "offline-cache-union",
            "model": "valid-cached-translations-v1",
            "cache_reuse": "valid_cached_union",
            "cache_source_pairs": source_pairs,
        }
    )

    result = comment_translation.validate_candidate_translations(candidate)

    assert result["valid"] is True
    assert result["failures"] == []

    broken = copy.deepcopy(candidate)
    broken["comment_translations"][0].pop("cache_source_model")
    invalid = comment_translation.validate_candidate_translations(broken)
    assert invalid["valid"] is False
    assert any("cache_source_model 缺失" in reason for reason in invalid["failures"])

    forged = copy.deepcopy(candidate)
    forged["comment_translations"][0]["cache_source_provider"] = "forged-provider"
    forged["comment_translation_summary"]["cache_source_pairs"] = {
        "forged-provider:qwen3.5:4b": 1,
        "ollama:qwen3.5:4b": 1,
        "anthropic:claude-cache": 2,
    }
    invalid = comment_translation.validate_candidate_translations(forged)
    assert invalid["valid"] is False
    assert any("必须为 ollama/anthropic" in reason for reason in invalid["failures"])


def test_strict_validator_rejects_stale_hash_translation_and_status():
    candidate = _persisted_candidate("corrupt", 1)
    row = candidate["comment_translations"][0]
    row["source_hash"] = "0" * 64
    row["translated_zh"] = "不一致译文"
    row["status"] = "failed"

    result = comment_translation.validate_candidate_translations(candidate)

    assert result["valid"] is False
    assert any("source_hash 与原文不一致" in reason for reason in result["failures"])
    assert any("translated_zh 与 translated_text 不一致" in reason
               for reason in result["failures"])
    assert any("状态必须同时为 translated" in reason for reason in result["failures"])


def test_strict_validator_rederives_every_scoring_and_delivery_consumer():
    candidate = _persisted_candidate("derived", 4)
    mutations = {
        "translated_intent_comments": lambda value: [
            *(value or []), {"username": "forged", "original_text": "伪造"}
        ],
        "translated_intent_by_grade": lambda _value: {
            "high": 1, "medium": 0, "low": 0
        },
        "intent_by_grade": lambda _value: {"high": 1, "medium": 0, "low": 0},
        "high_intent_count": lambda value: int(value or 0) + 1,
        "high_intent_ratio": lambda value: 0.0 if value is None else value + 1,
        "intent_total": lambda value: int(value or 0) + 1,
        "intent_posts": lambda value: [
            *(value or []),
            {"post_url": "https://www.instagram.com/p/forged/", "intent_comments": []},
        ],
        "promo_intent_hits": lambda value: int(value or 0) + 1,
        "valid_comments": lambda value: int(value or 0) + 1,
        "low_quality_ratio": lambda value: float(value or 0) + 1,
    }

    for field, mutate in mutations.items():
        broken = copy.deepcopy(candidate)
        broken[field] = mutate(broken.get(field))
        result = comment_translation.validate_candidate_translations(broken)
        assert result["valid"] is False, field
        assert any(f"{field} 与翻译行重派生结果不一致" in reason
                   for reason in result["failures"]), field

    for field in (
        "semantic_coverage_complete",
        "semantic_source_count",
        "canonical_intent_metrics_updated",
        "canonical_quality_metrics_updated",
    ):
        broken = copy.deepcopy(candidate)
        summary = broken["comment_translation_summary"]
        summary[field] = not summary[field] if isinstance(summary[field], bool) else 999
        result = comment_translation.validate_candidate_translations(broken)
        assert result["valid"] is False, field
        assert any(f"summary.{field}" in reason for reason in result["failures"]), field
