"""Pure, offline merge policy for retry evidence from one Stage 3 window.

This module has no database or browser dependency.  It is shared by the
recovery CLI and can be called by the retry ledger: callers provide two
detached candidate snapshots plus stable source-attempt IDs/qualities.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from extensions.sop_v2 import comment_translation
from extensions.sop_v2 import comments as comment_semantics
from extensions.sop_v2 import content as content_signals
from extensions.sop_v2 import pricing as pricing_policy
from extensions.sop_v2.config import load_config


MERGE_SCHEMA = "sop-v2-same-window-deep-merge-v1"
_LOCAL_CAPTURE_TZ = timezone(timedelta(hours=8))
_MEDIA_RE = re.compile(
    r"/(?:[^/?#]+/)?(?:reel|p)/([A-Za-z0-9_-]+)", re.IGNORECASE
)
_COMPLETE_COMMENT_STATUSES = {
    "collected",
    "verified_zero",
    "unavailable_after_retry",
    "verified_empty_thread",
}
_CONTENT_FIELDS = {
    "amazon_finds_ratio",
    "device_specs_score",
    "ingredients_score",
    "meets_er_benchmark",
    "organic_relevant_posts",
    "real_er",
    "real_er_median",
    "real_er_window",
    "reels_er",
    "skin_science_score",
    "sku_categories_hit",
    "sponsorship_saturation",
    "static_er",
}
_INTENT_FIELDS = {
    "high_intent_count",
    "high_intent_ratio",
    "high_intent_snippets",
    "intent_by_grade",
    "intent_posts",
    "intent_total",
    "promo_intent_hits",
    "translated_intent_by_grade",
    "translated_intent_comments",
}
_TRANSLATION_FIELDS = {
    "comment_translation_summary",
    "comment_translations",
}


class EvidenceMergeError(RuntimeError):
    """Evidence cannot be merged without violating the frozen policy."""


class WindowMismatchError(EvidenceMergeError):
    """The two ordered target windows are not identical."""


@dataclass(frozen=True)
class MergeResult:
    candidate: dict[str, Any]
    mode: str
    same_window: bool
    pricing_source: str
    provenance: dict[str, Any]
    detail: dict[str, Any]


@dataclass(frozen=True)
class WindowDescriptor:
    basis: str
    target_posts: int
    available_posts: int
    identities: tuple[str, ...]
    refs: tuple[Any, ...]
    valid: bool


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceMergeError(f"evidence is not canonical JSON: {exc}") from exc


def evidence_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _source_ref(
    evidence: Mapping[str, Any], attempt_id: str | None
) -> dict[str, Any]:
    if attempt_id:
        return {"kind": "attempt", "attempt_id": str(attempt_id)}
    return {"kind": "snapshot", "evidence_sha256": evidence_sha256(evidence)}


def media_identity(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("code") or value.get("post_url")
    raw = str(value or "").strip()
    match = _MEDIA_RE.search(raw)
    if match:
        return match.group(1)
    return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _post_url(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("code") or value.get("post_url")
    return str(value or "").strip()


def describe_window(evidence: Mapping[str, Any]) -> WindowDescriptor:
    """Build the sole fail-closed descriptor used by match and fingerprint.

    A short window is valid only when ``deep_available_posts`` explicitly says
    the profile had fewer posts than the target.  A truncated/partial ref list
    can therefore never masquerade as a complete target window.
    """
    try:
        target = int(evidence.get("deep_target_posts") or 0)
    except (TypeError, ValueError):
        target = 0
    if evidence.get("post_refs"):
        refs = evidence.get("post_refs")
        basis = "post_refs"
    elif evidence.get("codes"):
        refs = evidence.get("codes")
        basis = "codes"
    else:
        refs = []
        basis = ""
    if isinstance(refs, (str, Mapping)):
        refs = [refs]
    if not isinstance(refs, Sequence):
        refs = []
    source = copy.deepcopy(list(refs)[: max(0, target)])
    identities = tuple(media_identity(ref) for ref in source)
    available_present = "deep_available_posts" in evidence
    try:
        available = int(evidence.get("deep_available_posts")) if available_present else target
    except (TypeError, ValueError):
        available = -1
    expected = min(target, available) if target > 0 and available >= 0 else -1
    valid = bool(
        basis
        and target > 0
        and available > 0
        and available <= target
        and expected == len(identities)
        and all(identities)
        and len(set(identities)) == len(identities)
    )
    return WindowDescriptor(
        basis=basis,
        target_posts=target,
        available_posts=available,
        identities=identities,
        refs=tuple(source),
        valid=valid,
    )


def ordered_target_window(
    evidence: Mapping[str, Any],
) -> tuple[int, tuple[str, ...], list[Any]]:
    """Compatibility view of :func:`describe_window`."""
    descriptor = describe_window(evidence)
    identities = descriptor.identities if descriptor.valid else ()
    return descriptor.target_posts, identities, copy.deepcopy(list(descriptor.refs))


def windows_match(old: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    old_window = describe_window(old)
    current_window = describe_window(current)
    return bool(
        old_window.valid
        and current_window.valid
        and old_window.basis == current_window.basis
        and old_window.target_posts == current_window.target_posts
        and old_window.available_posts == current_window.available_posts
        and old_window.identities == current_window.identities
    )


def window_fingerprint(evidence: Mapping[str, Any]) -> str | None:
    descriptor = describe_window(evidence)
    if not descriptor.valid:
        return None
    preimage = {
        "schema": "sop-v2-stage3-window-v1",
        "basis": descriptor.basis,
        "target_posts": descriptor.target_posts,
        "identities": list(descriptor.identities),
    }
    return evidence_sha256(preimage)


def _count(value: Any) -> int:
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _translation_quality(evidence: Mapping[str, Any]) -> tuple[int, int, int]:
    summary = evidence.get("comment_translation_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    translated = _count(summary.get("translated_count"))
    failed = _count(summary.get("failed_count"))
    status = str(summary.get("status") or "")
    complete = int(
        status == "complete"
        and summary.get("semantic_coverage_complete") is not False
        and failed == 0
    )
    rank = {
        "": 0,
        "source_unavailable": 1,
        "failed": 1,
        "partial": 2,
        "complete": 3,
    }.get(status, 0)
    return complete, rank, translated - failed


def _pricing_tuple(evidence: Mapping[str, Any]) -> tuple[int, int]:
    estimate = evidence.get("pricing_estimate")
    estimate = estimate if isinstance(estimate, Mapping) else {}
    status = str(estimate.get("status") or "")
    rank = {
        "": 0,
        "missing": 1,
        "fallback_modash": 1,
        "partial": 2,
        "not_applicable_no_reels": 3,
        "complete_available": 4,
        "complete": 5,
    }.get(status, 0)
    integrity_failed = bool(pricing_policy.estimate_integrity_reasons(
        dict(evidence), load_config(), allow_legacy_schema1=True
    ))
    if integrity_failed:
        rank = 0
    if "sample_count" in estimate:
        samples = _count(estimate.get("sample_count"))
    else:
        rows = evidence.get("pricing_reel_samples") or []
        samples = len(
            [row for row in rows if isinstance(row, Mapping)]
        ) if isinstance(rows, list) else 0
    return rank, 0 if integrity_failed else samples


def summarize_quality(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Retry-compatible deterministic quality summary without ledger imports."""
    posts = [
        post
        for post in (evidence.get("sampled_posts") or [])
        if isinstance(post, Mapping)
    ]
    successful = _count(evidence.get("deep_successful_posts")) or len(posts)
    observed = sum(
        post.get("metrics_status") == "observed"
        or post.get("like_count") is not None
        or post.get("comment_count") is not None
        for post in posts
    )
    translation = _translation_quality(evidence)
    pricing = _pricing_tuple(evidence)
    selection = (
        int(evidence.get("deep_collection_status") == "complete"),
        successful,
        observed,
        -_count(evidence.get("deep_failed_posts")),
        -_count(evidence.get("deep_metric_missing_posts")),
        _count(evidence.get("comment_completed_posts")),
        -_count(evidence.get("comment_failed_posts")),
        *translation,
        len(
            [
                row
                for row in (evidence.get("comment_records") or [])
                if isinstance(row, Mapping)
            ]
        ),
        _count(evidence.get("comments_analyzed")),
        _count(evidence.get("valid_comments")),
    )
    return {
        "selection_tuple": list(selection),
        "window_fingerprint": window_fingerprint(evidence),
        "deep_complete": bool(selection[0]),
        "successful_posts": successful,
        "comment_records": selection[10],
        "pricing_rank": pricing[0],
        "pricing_samples": pricing[1],
    }


def _quality_tuple(quality: Mapping[str, Any]) -> tuple[Any, ...]:
    value = quality.get("selection_tuple") or []
    if not isinstance(value, (list, tuple)):
        raise EvidenceMergeError("source quality has no selection_tuple")
    return tuple(value)


def select_whole_attempt(
    old: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    old_quality: Mapping[str, Any] | None = None,
    current_quality: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Choose exactly one whole attempt; ties retain the current canonical."""
    old_q = dict(old_quality or summarize_quality(old))
    current_q = dict(current_quality or summarize_quality(current))
    if _quality_tuple(old_q) > _quality_tuple(current_q):
        return "old", copy.deepcopy(dict(old))
    return "current", copy.deepcopy(dict(current))


def _time_key(value: Any) -> tuple[int, float, str]:
    raw = str(value or "").strip()
    if not raw:
        return (0, -math.inf, "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_LOCAL_CAPTURE_TZ)
        return (1, parsed.timestamp(), raw)
    except (ValueError, OverflowError):
        return (0, -math.inf, raw)


def _post_quality(post: Mapping[str, Any], source_priority: int) -> tuple[Any, ...]:
    likes = post.get("like_count")
    comments = post.get("comment_count")
    metric_fields = sum(
        post.get(key) is not None
        for key in ("like_count", "comment_count", "play_count")
    )
    field_completeness = sum(
        value not in (None, "", [], {}) for value in post.values()
    )
    return (
        int(
            post.get("metrics_status") == "observed"
            and likes is not None
            and comments is not None
        ),
        metric_fields,
        _time_key(post.get("captured_at")),
        source_priority,
        field_completeness,
        evidence_sha256(post),
    )


def _best_posts(
    old: Mapping[str, Any],
    current: Mapping[str, Any],
    identities: tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    choices: dict[str, list[tuple[dict[str, Any], int, str]]] = {
        identity: [] for identity in identities
    }
    for source, priority, name in ((old, 0, "old"), (current, 1, "current")):
        rows = source.get("sampled_posts") or []
        if not isinstance(rows, list):
            continue
        for value in rows:
            if not isinstance(value, Mapping):
                continue
            identity = media_identity(value)
            if identity in choices:
                choices[identity].append((copy.deepcopy(dict(value)), priority, name))
    selected: list[dict[str, Any]] = []
    sources = {"old": 0, "current": 0, "missing": 0}
    for identity in identities:
        options = choices[identity]
        if not options:
            sources["missing"] += 1
            continue
        post, _, name = max(options, key=lambda row: _post_quality(row[0], row[1]))
        selected.append(post)
        sources[name] += 1
    return selected, sources


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _source_comments(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    records = evidence.get("comment_records") or []
    if isinstance(records, list):
        for value in records:
            if not isinstance(value, Mapping):
                continue
            text = _normalized_text(value.get("original_text") or value.get("text"))
            if not text:
                continue
            output.append(
                {
                    "username": str(value.get("username") or "").lstrip("@"),
                    "text": text,
                    "post_url": value.get("post_url"),
                }
            )
            seen_text.add(text.casefold())
    sample = evidence.get("comment_sample") or []
    if isinstance(sample, list):
        for value in sample:
            text = _normalized_text(value)
            if text and text.casefold() not in seen_text:
                output.append({"username": "", "text": text, "post_url": None})
                seen_text.add(text.casefold())
    return output


def _comment_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("username") or "").casefold(),
        _normalized_text(row.get("text") or row.get("original_text")).casefold(),
        media_identity(row.get("post_url")),
    )


def _merge_comments(
    old: Mapping[str, Any],
    current: Mapping[str, Any],
    identities: tuple[str, ...],
) -> list[dict[str, Any]]:
    position = {identity: index for index, identity in enumerate(identities)}
    staged: list[tuple[int, int, int, dict[str, Any]]] = []
    for source_index, source in enumerate((old, current)):
        for row_index, row in enumerate(_source_comments(source)):
            media = media_identity(row.get("post_url"))
            if media and media not in position:
                continue
            staged.append((position.get(media, len(identities)), source_index, row_index, row))
    staged.sort(key=lambda item: item[:3])
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for _, _, _, row in staged:
        identity = _comment_identity(row)
        if not identity[1] or identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def _translation_pool(
    old: Mapping[str, Any], current: Mapping[str, Any]
) -> list[tuple[int, dict[str, Any]]]:
    output: list[tuple[int, dict[str, Any]]] = []
    for priority, source in ((0, old), (1, current)):
        rows = source.get("comment_translations") or []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping):
                    output.append((priority, copy.deepcopy(dict(row))))
    return output


def _install_cached_translations(
    candidate: dict[str, Any],
    pool: list[tuple[int, dict[str, Any]]],
    generated_at: str,
) -> dict[str, Any]:
    records = candidate.get("comment_records") or []
    source_limit = max(1, len(records))
    targets = comment_translation.collect_translation_targets(
        candidate, source_limit=source_limit
    )
    if not targets:
        candidate["comment_translations"] = []
        comment_translation.translate_candidate(
            candidate,
            provider="offline",
            model="not-needed",
            source_limit=source_limit,
        )
        candidate["comment_translation_summary"]["generated_at"] = generated_at
        return {"provider": None, "model": None, "source_count": 0}

    required = {str(target["source_hash"]) for target in targets}
    by_hash: dict[str, tuple[int, dict[str, Any]]] = {}
    for priority, row in pool:
        provider = str(row.get("provider") or "")
        model = str(row.get("model") or "")
        if not provider or not model:
            continue
        try:
            valid = comment_translation._valid_cached(  # noqa: SLF001
                row, provider=provider, model=model
            )
        except AttributeError as exc:
            raise EvidenceMergeError("cached translation API is incompatible") from exc
        if not valid:
            continue
        source_hash = str(row.get("source_hash") or "")
        previous = by_hash.get(source_hash)
        if previous is None or (priority, evidence_sha256(row)) > (
            previous[0], evidence_sha256(previous[1])
        ):
            by_hash[source_hash] = (priority, row)
    if not required <= set(by_hash):
        raise EvidenceMergeError(
            "same-window merge lacks a valid cached translation for every source comment"
        )
    offline_provider = "offline-cache-union"
    offline_model = "valid-cached-translations-v1"
    source_by_hash: dict[str, tuple[str, str]] = {}
    normalized_cache: list[dict[str, Any]] = []
    for target in targets:
        source_hash = str(target["source_hash"])
        source = copy.deepcopy(by_hash[source_hash][1])
        source_by_hash[source_hash] = (
            str(
                source.get("cache_source_provider")
                or source.get("provider")
                or ""
            ),
            str(
                source.get("cache_source_model")
                or source.get("model")
                or ""
            ),
        )
        source["provider"] = offline_provider
        source["model"] = offline_model
        normalized_cache.append(source)
    candidate["comment_translations"] = normalized_cache
    try:
        comment_translation.translate_candidate(
            candidate,
            provider=offline_provider,
            model=offline_model,
            source_limit=source_limit,
            retries=0,
        )
    except comment_translation.TranslationConfigurationError as exc:
        # The deliberately unsupported provider is reached only when the cache
        # preflight and comment_translation target set disagree.  It cannot
        # open a network transport.
        raise EvidenceMergeError("cached translation replay requested network") from exc
    summary = candidate.get("comment_translation_summary") or {}
    requested = _count(summary.get("requested_count"))
    if (
        summary.get("status") != "complete"
        or requested != len(targets)
        or _count(summary.get("translated_count")) != requested
        or _count(summary.get("failed_count"))
        or summary.get("semantic_coverage_complete") is not True
    ):
        raise EvidenceMergeError("cached translation replay was incomplete")
    summary["generated_at"] = generated_at
    source_pairs: dict[str, int] = {}
    for row in candidate.get("comment_translations") or []:
        source_hash = str(row.get("source_hash") or "")
        source_provider, source_model = source_by_hash[source_hash]
        row["cache_source_provider"] = source_provider
        row["cache_source_model"] = source_model
        key = f"{source_provider}:{source_model}"
        source_pairs[key] = source_pairs.get(key, 0) + 1
    summary["cache_reuse"] = "valid_cached_union"
    summary["cache_source_pairs"] = source_pairs
    return {
        "provider": offline_provider,
        "model": offline_model,
        "source_count": requested,
        "source_pairs": source_pairs,
    }


def _pricing_quality(
    evidence: Mapping[str, Any], source_priority: int
) -> tuple[Any, ...]:
    rank, samples = _pricing_tuple(evidence)
    estimate = evidence.get("pricing_estimate")
    estimate = estimate if isinstance(estimate, Mapping) else {}
    captured = evidence.get("pricing_captured_at") or estimate.get("captured_at")
    bundle = {
        key: evidence.get(key)
        for key in (
            "pricing_estimate",
            "pricing_reel_samples",
            "pricing_reels_tab_evidence",
            "pricing_captured_at",
        )
        if key in evidence
    }
    return rank, samples, _time_key(captured), source_priority, evidence_sha256(bundle)


def pricing_quality(evidence: Mapping[str, Any]) -> tuple[int, int, int]:
    """The exact public pricing tuple used by the Stage 3 finalizer."""
    rank, samples = _pricing_tuple(evidence)
    estimate = evidence.get("pricing_estimate")
    estimate = estimate if isinstance(estimate, Mapping) else {}
    captured = evidence.get("pricing_captured_at") or estimate.get("captured_at")
    time_key = _time_key(captured)
    epoch_us = int(time_key[1] * 1_000_000) if time_key[0] else 0
    return rank, samples, epoch_us


def select_pricing(
    old: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    source, _, name = max(
        ((old, 0, "old"), (current, 1, "current")),
        key=lambda row: _pricing_quality(row[0], row[1]),
    )
    return name, {
        key: copy.deepcopy(source[key])
        for key in (
            "pricing_estimate",
            "pricing_reel_samples",
            "pricing_reels_tab_evidence",
            "pricing_captured_at",
        )
        if key in source
    }


def _ref_for_identity(refs: Sequence[Any], identity: str) -> str:
    for ref in refs:
        if media_identity(ref) == identity:
            return _post_url(ref) or f"/p/{identity}/"
    return f"/p/{identity}/"


def _unavailable_by_media(
    old: Mapping[str, Any], current: Mapping[str, Any], identities: set[str]
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for source in (old, current):
        rows = source.get("comment_unavailable_posts") or []
        if isinstance(rows, (str, Mapping)):
            rows = [rows]
        if not isinstance(rows, list):
            continue
        for row in rows:
            identity = media_identity(row)
            if identity in identities:
                output[identity] = (
                    copy.deepcopy(dict(row))
                    if isinstance(row, Mapping)
                    else {"url": str(row)}
                )
    return output


def _recompute_content(
    candidate: dict[str, Any], config: Mapping[str, Any]
) -> None:
    for key in _CONTENT_FIELDS:
        candidate.pop(key, None)
    posts = candidate.get("sampled_posts") or []
    derived_posts = [
        {
            "caption_text": post.get("caption") or "",
            "media_type": 2 if post.get("is_video") or post.get("is_reel") else 1,
            "like_count": post.get("like_count"),
            "comment_count": post.get("comment_count"),
            "play_count": post.get("play_count"),
            "pinned": post.get("pinned"),
            "taken_at": post.get("taken_at"),
        }
        for post in posts
        if isinstance(post, Mapping)
    ]
    scratch = copy.deepcopy(candidate)
    scratch["posts"] = derived_posts
    derived = content_signals.derive_content_signals(scratch, dict(config))
    for key in _CONTENT_FIELDS:
        if key in derived:
            candidate[key] = derived[key]
    captions = " ".join(str(post.get("caption") or "") for post in posts)
    candidate["core_niche_key"] = content_signals.derive_niche(
        candidate.get("biography"), candidate.get("full_name"), captions
    )
    candidate["promotional_post_count"] = sum(
        comment_semantics.is_promotional(str(post.get("caption") or ""))
        for post in posts
    )


def _recompute_intent_display(candidate: dict[str, Any]) -> None:
    promo = {
        media_identity(post): comment_semantics.is_promotional(
            str(post.get("caption") or "")
        )
        for post in (candidate.get("sampled_posts") or [])
        if isinstance(post, Mapping)
    }
    for post in candidate.get("intent_posts") or []:
        if isinstance(post, dict):
            post["promotional"] = promo.get(media_identity(post.get("post_url")))
    order = {"high": 0, "medium": 1, "low": 2}
    grade_zh = {"high": "高", "medium": "中", "low": "低"}
    rows = [
        row
        for row in (candidate.get("translated_intent_comments") or [])
        if isinstance(row, Mapping)
    ]
    rows.sort(
        key=lambda row: order.get(
            row.get("translated_intent_grade") or row.get("grade"), 3
        )
    )
    candidate["high_intent_snippets"] = [
        f"@{str(row.get('username') or '').lstrip('@')}（"
        f"{grade_zh.get(row.get('translated_intent_grade') or row.get('grade'), '')}）: "
        f"{row.get('original_text') or ''}"
        for row in rows[:8]
    ]


def merge_same_window(
    old: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    old_attempt_id: str | None = None,
    current_attempt_id: str | None = None,
    old_quality: Mapping[str, Any] | None = None,
    current_quality: Mapping[str, Any] | None = None,
    generated_at: str = "1970-01-01T00:00:00+00:00",
    config: Mapping[str, Any] | None = None,
) -> MergeResult:
    """Merge complementary media/comments for one exact ordered window."""
    if not windows_match(old, current):
        raise WindowMismatchError("ordered target windows differ")
    old_window = describe_window(old)
    current_window = describe_window(current)
    current_target = current_window.target_posts
    current_ids = current_window.identities
    refs = copy.deepcopy(list(current_window.refs))
    old_q = dict(old_quality or summarize_quality(old))
    current_q = dict(current_quality or summarize_quality(current))
    _, base = select_whole_attempt(
        old,
        current,
        old_quality=old_q,
        current_quality=current_q,
    )
    merged = copy.deepcopy(base)
    posts, selected_from = _best_posts(old, current, current_ids)
    comments = _merge_comments(old, current, current_ids)
    comment_counts: dict[str, int] = {}
    for row in comments:
        identity = media_identity(row.get("post_url"))
        if identity:
            comment_counts[identity] = comment_counts.get(identity, 0) + 1

    posts_by_identity = {media_identity(post): post for post in posts}
    ordered_posts: list[dict[str, Any]] = []
    failed_posts: list[str] = []
    metric_missing: list[str] = []
    comment_failed: list[str] = []
    unavailable: list[dict[str, Any]] = []
    unavailable_source = _unavailable_by_media(old, current, set(current_ids))
    attempted = completed = 0
    for identity in current_ids:
        post = posts_by_identity.get(identity)
        ref_url = _ref_for_identity(refs, identity)
        if post is None:
            failed_posts.append(ref_url)
            continue
        post = copy.deepcopy(post)
        url = _post_url(post) or ref_url
        associated = comment_counts.get(identity, 0)
        if associated:
            post["comments_collected"] = max(
                associated, _count(post.get("comments_collected"))
            )
            post["comment_sampling_status"] = "collected"
        elif post.get("comment_count") == 0:
            post["comments_collected"] = 0
            post["comment_sampling_status"] = "verified_zero"
        status = str(post.get("comment_sampling_status") or "")
        if post.get("like_count") is not None and post.get("comment_count") is not None:
            post["metrics_status"] = "observed"
        else:
            post["metrics_status"] = "missing"
            metric_missing.append(url)
        if post.get("comment_count") != 0:
            attempted += 1
        unavailable_entry = unavailable_source.get(identity)
        if status == "unavailable_after_retry" and not (
            comment_semantics.repeated_low_comment_unavailable(
                post, unavailable_entry
            )
        ):
            status = "failed_invalid_low_comment_terminal"
            post["comment_sampling_status"] = status
        if status == "verified_empty_thread" and not (
            comment_semantics.verified_empty_thread_evidence(
                post, unavailable_entry
            )
        ):
            status = "failed_invalid_empty_thread_evidence"
            post["comment_sampling_status"] = status
        if status in _COMPLETE_COMMENT_STATUSES:
            completed += 1
            if status == "unavailable_after_retry":
                unavailable.append(
                    copy.deepcopy(
                        unavailable_source.get(identity)
                        or {
                            "url": url,
                            "reported_count": post.get("comment_count"),
                            "reason": "reported_low_count_unavailable_after_retry",
                        }
                    )
                )
            elif status == "verified_empty_thread":
                # This branch is reachable only after the full DOM + endpoint
                # proof and its matching aggregate audit row passed validation.
                unavailable.append(copy.deepcopy(dict(unavailable_entry)))
        else:
            comment_failed.append(url)
        ordered_posts.append(post)

    for key in (*_TRANSLATION_FIELDS, *_INTENT_FIELDS):
        merged.pop(key, None)
    merged.update(
        {
            "sampled_posts": ordered_posts,
            "comment_records": comments,
            "comment_sample": [row["text"] for row in comments],
            "comments_analyzed": len(comments),
            "valid_comments": 0,
            "low_quality_ratio": None,
            "high_intent_ratio": None,
            "intent_posts": [],
            "post_refs": copy.deepcopy(refs),
            "codes": copy.deepcopy(
                list(current.get("codes") or [_post_url(ref) for ref in refs])
            ),
            "deep_target_posts": current_target,
            "deep_available_posts": len(current_ids),
            "deep_successful_posts": len(ordered_posts),
            "deep_failed_posts": failed_posts,
            "deep_metric_missing_posts": metric_missing,
            "comment_attempted_posts": attempted,
            "comment_completed_posts": completed,
            "comment_failed_posts": comment_failed,
            "comment_unavailable_posts": unavailable,
            "comments_read": bool(ordered_posts),
        }
    )
    merged["deep_collection_status"] = (
        "complete"
        if (
            current_ids
            and len(ordered_posts) == len(current_ids)
            and not failed_posts
            and not metric_missing
            and not comment_failed
        )
        else "incomplete"
    )
    translation = _install_cached_translations(
        merged, _translation_pool(old, current), generated_at
    )
    _recompute_intent_display(merged)
    _recompute_content(merged, config or load_config())
    pricing_source, pricing = select_pricing(old, current)
    for key in (
        "pricing_estimate",
        "pricing_reel_samples",
        "pricing_reels_tab_evidence",
        "pricing_captured_at",
    ):
        merged.pop(key, None)
    merged.update(pricing)
    provenance = {
        "schema": MERGE_SCHEMA,
        "sources": {
            "old": _source_ref(old, old_attempt_id),
            "current": _source_ref(current, current_attempt_id),
        },
        "source_quality": {"old": old_q, "current": current_q},
        "window_fingerprint": window_fingerprint(current),
    }
    detail = {
        "selected_posts": selected_from,
        "comment_records_before_current": len(_source_comments(current)),
        "comment_records_before_old": len(_source_comments(old)),
        "comment_records_after_union": len(comments),
        "translation": translation,
    }
    return MergeResult(
        candidate=merged,
        mode="same_window_media_merge",
        same_window=True,
        pricing_source=pricing_source,
        provenance=provenance,
        detail=detail,
    )


def merge_evidence(
    old: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    old_attempt_id: str | None = None,
    current_attempt_id: str | None = None,
    old_quality: Mapping[str, Any] | None = None,
    current_quality: Mapping[str, Any] | None = None,
    generated_at: str = "1970-01-01T00:00:00+00:00",
    config: Mapping[str, Any] | None = None,
) -> MergeResult:
    """Merge an exact window or fail closed to deterministic whole selection."""
    old_q = dict(old_quality or summarize_quality(old))
    current_q = dict(current_quality or summarize_quality(current))
    if windows_match(old, current):
        return merge_same_window(
            old,
            current,
            old_attempt_id=old_attempt_id,
            current_attempt_id=current_attempt_id,
            old_quality=old_q,
            current_quality=current_q,
            generated_at=generated_at,
            config=config,
        )
    selected, candidate = select_whole_attempt(
        old,
        current,
        old_quality=old_q,
        current_quality=current_q,
    )
    pricing_source, pricing = select_pricing(old, current)
    for key in (
        "pricing_estimate",
        "pricing_reel_samples",
        "pricing_reels_tab_evidence",
        "pricing_captured_at",
    ):
        candidate.pop(key, None)
    candidate.update(pricing)
    provenance = {
        "schema": MERGE_SCHEMA,
        "sources": {
            "old": _source_ref(old, old_attempt_id),
            "current": _source_ref(current, current_attempt_id),
        },
        "source_quality": {"old": old_q, "current": current_q},
        "window_fingerprints": {
            "old": window_fingerprint(old),
            "current": window_fingerprint(current),
        },
        "whole_attempt_selected": selected,
    }
    return MergeResult(
        candidate=candidate,
        mode="whole_attempt_selection",
        same_window=False,
        pricing_source=pricing_source,
        provenance=provenance,
        detail={"whole_attempt_selected": selected},
    )


__all__ = [
    "EvidenceMergeError",
    "MERGE_SCHEMA",
    "MergeResult",
    "WindowMismatchError",
    "WindowDescriptor",
    "describe_window",
    "evidence_sha256",
    "media_identity",
    "merge_evidence",
    "merge_same_window",
    "ordered_target_window",
    "pricing_quality",
    "select_pricing",
    "select_whole_attempt",
    "summarize_quality",
    "window_fingerprint",
    "windows_match",
]
