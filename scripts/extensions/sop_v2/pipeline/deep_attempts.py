"""Stage 3 retry evidence ledger and monotonic canonical selection.

Deep collection mutates its candidate in place.  A retry therefore needs a
snapshot boundary outside the collector: each attempt is retained separately.
Different recent windows select one whole canonical attempt; two complete,
strictly identical target descriptors may instead produce a full synthetic
canonical entry via the frozen media-identity merge policy.

The persisted ledger is JSON-only and append-only.  Every new entry contains a
content hash, a deterministic quality tuple and enough evidence to audit or
replay the canonical choice.  Callers must not field-merge attempts whose
strict ordered target descriptors differ or are incomplete/ambiguous.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from extensions.sop_v2 import comments as comment_semantics
from extensions.sop_v2.pipeline import deep_evidence_merge


LEDGER_FIELD = "deep_collection_attempts"
CANONICAL_ATTEMPT_FIELD = "deep_canonical_attempt_id"
CANONICAL_QUALITY_FIELD = "deep_canonical_quality"
COMMENT_RETRY_STATE_FIELD = "stage3_comment_retry_state"
# Public compatibility alias for the first review draft; persisted data uses the
# compact state-machine field above.
COMMENT_RETRY_HISTORY_FIELD = COMMENT_RETRY_STATE_FIELD
_LEGACY_COMMENT_RETRY_HISTORY_FIELD = "stage3_comment_retry_history"
_LEGACY_MERGE_PROVENANCE_FIELD = "deep_evidence_merge_provenance"
PRICING_CANONICAL_ATTEMPT_FIELD = "pricing_canonical_attempt_id"
PRICING_CANONICAL_QUALITY_FIELD = "pricing_canonical_quality"
ATTEMPT_SCHEMA = "sop-v2-stage3-attempt-v1"

_QUEUE_FIELDS = {"_queue_lock_token", "_queue_from_status"}
STAGE3_RESET_FIELDS = (
    LEDGER_FIELD,
    CANONICAL_ATTEMPT_FIELD,
    CANONICAL_QUALITY_FIELD,
    COMMENT_RETRY_HISTORY_FIELD,
    PRICING_CANONICAL_ATTEMPT_FIELD,
    PRICING_CANONICAL_QUALITY_FIELD,
    _LEGACY_COMMENT_RETRY_HISTORY_FIELD,
    _LEGACY_MERGE_PROVENANCE_FIELD,
)
_METADATA_FIELDS = set(STAGE3_RESET_FIELDS)
_PRICING_FIELDS = {
    "pricing_reel_samples",
    "pricing_reels_tab_evidence",
    "pricing_captured_at",
    "pricing_estimate",
}
_STAGE3_INPUT_DEEP_FIELDS = {"codes", "post_refs"} | _PRICING_FIELDS
_LOCAL_CAPTURE_TZ = timezone(timedelta(hours=8))
_WINDOW_FIELDS = {
    "codes",
    "post_refs",
    "primary_grid_refresh_error",
    "primary_refs_source",
    "primary_refs_fallback_reason",
    "sampled_posts",
}
_DERIVED_CONTENT_FIELDS = {
    "amazon_finds_ratio",
    "core_niche_key",
    "device_specs_score",
    "ingredients_score",
    "meets_er_benchmark",
    "organic_relevant_posts",
    "promotional_post_count",
    "reels_er",
    "skin_science_score",
    "sku_categories_hit",
    "sponsorship_saturation",
    "static_er",
    "valid_comments",
    "low_quality_ratio",
    "high_intent_ratio",
    "high_intent_count",
    "high_intent_snippets",
    "promo_intent_hits",
}
_DEEP_EXACT_FIELDS = _WINDOW_FIELDS | _DERIVED_CONTENT_FIELDS | {
    "comments_analyzed",
    "comments_read",
    "evidence_dir",
}
_DEEP_PREFIXES = (
    "comment_",
    "deep_",
    "intent_",
    "pricing_",
    "real_er",
    "translated_intent_",
)
_MEDIA_RE = re.compile(
    r"/(?:[^/?#]+/)?(?:reel|p)/([A-Za-z0-9_-]+)", re.IGNORECASE
)


@dataclass
class Stage3AttemptContext:
    """Immutable-before-run snapshots plus the isolated mutable work copy."""

    prior_snapshot: dict[str, Any]
    input_snapshot: dict[str, Any]
    candidate: dict[str, Any]


def _is_deep_field(key: str) -> bool:
    if key in _METADATA_FIELDS:
        return False
    return key in _DEEP_EXACT_FIELDS or key.startswith(_DEEP_PREFIXES)


def _json_safe(value: Any) -> Any:
    """Return a detached, JSON-friendly value while excluding lease metadata."""
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if str(key) not in _QUEUE_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_safe(item) for item in value]
        return sorted(normalized, key=lambda item: _canonical_json(item))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _as_count(value: Any) -> int:
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _extract_attempt_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _json_safe(value)
        for key, value in candidate.items()
        if _is_deep_field(str(key))
    }


def _evidence_delta(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    old = _extract_attempt_evidence(before)
    new = _extract_attempt_evidence(after)
    return {
        key: value
        for key, value in new.items()
        if key not in old or old[key] != value
    }


def _complete_partial_target_descriptor(
    delta: dict[str, Any],
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Make a real partial deep attempt self-contained without cloning empty input.

    A post-collector failure (for example, the translator raising) is returned as
    an error without an explicit candidate.  Unchanged target refs are absent
    from a normal delta even though sampled output belongs to that window.  Copy
    only the stable descriptor when the delta proves that deep collection ran;
    an unchanged startup/login failure therefore remains an empty evidence event.
    """
    output_markers = {
        "sampled_posts",
        "comments_read",
        "comment_records",
        "deep_collection_status",
        "deep_successful_posts",
        "comment_attempted_posts",
        "comment_completed_posts",
    }
    if not delta or not output_markers.intersection(delta):
        return delta
    completed = dict(delta)
    for key in ("post_refs", "codes", "deep_target_posts"):
        if key in completed:
            continue
        if key in after:
            completed[key] = _json_safe(after[key])
        elif key in before:
            completed[key] = _json_safe(before[key])
    return completed


def _media_identity(value: Any) -> str:
    raw = str(value or "").strip()
    match = _MEDIA_RE.search(raw)
    if match:
        return match.group(1)
    return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def window_fingerprint(evidence: Mapping[str, Any]) -> str | None:
    """Delegate the frozen fail-closed target descriptor fingerprint."""
    return deep_evidence_merge.window_fingerprint(evidence)


def _translation_quality(evidence: Mapping[str, Any]) -> tuple[int, int, int]:
    summary = evidence.get("comment_translation_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    rows = [
        row
        for row in (evidence.get("comment_translations") or [])
        if isinstance(row, Mapping)
    ]
    if "translated_count" in summary:
        translated = _as_count(summary.get("translated_count"))
    else:
        translated = _as_count(summary.get("translated")) or sum(
            (row.get("translation_status") or row.get("status")) == "translated"
            for row in rows
        )
    if "failed_count" in summary:
        failed = _as_count(summary.get("failed_count"))
    else:
        failed = _as_count(summary.get("failed")) or sum(
            (row.get("translation_status") or row.get("status")) == "failed"
            for row in rows
        )
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


def _pricing_quality(evidence: Mapping[str, Any]) -> tuple[int, int, int]:
    return deep_evidence_merge.pricing_quality(evidence)


def _timestamp_rank(value: str | None) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # browser_collect_v2._now() emits naive Asia/Shanghai local time.
            parsed = parsed.replace(tzinfo=_LOCAL_CAPTURE_TZ)
        return int(parsed.timestamp() * 1_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0


def summarize_quality(
    evidence: Mapping[str, Any],
    *,
    attempted_at: str | None = None,
    ordinal: int = 0,
) -> dict[str, Any]:
    """Create the deterministic tuple used to select one whole attempt."""
    posts = [
        post
        for post in (evidence.get("sampled_posts") or [])
        if isinstance(post, Mapping)
    ]
    successful = _as_count(evidence.get("deep_successful_posts")) or len(posts)
    observed_metrics = sum(
        post.get("metrics_status") == "observed"
        or post.get("like_count") is not None
        or post.get("comment_count") is not None
        for post in posts
    )
    deep_failed = _as_count(evidence.get("deep_failed_posts"))
    metric_missing = _as_count(evidence.get("deep_metric_missing_posts"))
    comment_completed = _as_count(evidence.get("comment_completed_posts"))
    comment_failed = _as_count(evidence.get("comment_failed_posts"))
    records = len(
        [
            row
            for row in (evidence.get("comment_records") or [])
            if isinstance(row, Mapping)
        ]
    )
    analyzed = _as_count(evidence.get("comments_analyzed"))
    valid = _as_count(evidence.get("valid_comments"))
    translation = _translation_quality(evidence)
    pricing = _pricing_quality(evidence)
    selection_tuple = (
        int(evidence.get("deep_collection_status") == "complete"),
        successful,
        observed_metrics,
        -deep_failed,
        -metric_missing,
        comment_completed,
        -comment_failed,
        *translation,
        records,
        analyzed,
        valid,
        max(0, int(ordinal)),
    )
    return {
        "selection_tuple": list(selection_tuple),
        "deep_complete": bool(selection_tuple[0]),
        "sampled_posts": len(posts),
        "successful_posts": successful,
        "observed_metric_posts": observed_metrics,
        "deep_failed_posts": deep_failed,
        "metric_missing_posts": metric_missing,
        "comment_completed_posts": comment_completed,
        "comment_failed_posts": comment_failed,
        "comment_records": records,
        "comments_analyzed": analyzed,
        "valid_comments": valid,
        "translation_complete": bool(translation[0]),
        "translated_net": translation[2],
        "pricing_rank": pricing[0],
        "pricing_samples": pricing[1],
        "pricing_captured_epoch_us": pricing[2],
        "window_fingerprint": window_fingerprint(evidence),
        "freshness_epoch_us": _timestamp_rank(attempted_at),
        "attempt_ordinal": max(0, int(ordinal)),
    }


def _has_attempt_evidence(evidence: Mapping[str, Any]) -> bool:
    # Stage 2 refs/codes are valuable retry input even before the first post page
    # succeeds.  An empty failed attempt must never clear them.
    return bool(evidence)


def _best_evidence_timestamp(evidence: Mapping[str, Any], fallback: str) -> str:
    candidates = [evidence.get("pricing_captured_at")]
    candidates.extend(
        post.get("captured_at")
        for post in (evidence.get("sampled_posts") or [])
        if isinstance(post, Mapping)
    )
    return next((str(value) for value in candidates if value), fallback)


def _new_entry(
    *,
    attempted_at: str,
    outcome: str,
    error: str | None,
    evidence: Mapping[str, Any],
    ordinal: int,
    timestamp_source: str = "attempt",
) -> dict[str, Any]:
    safe_evidence = _json_safe(evidence)
    quality = summarize_quality(
        safe_evidence,
        attempted_at=attempted_at,
        ordinal=ordinal,
    )
    evidence_sha = _sha256(safe_evidence)
    identity = {
        "attempted_at": attempted_at,
        "outcome": outcome,
        "error": error,
        "evidence_sha256": evidence_sha,
        "ordinal": ordinal,
    }
    return {
        "schema": ATTEMPT_SCHEMA,
        "attempt_id": _sha256(identity),
        "attempted_at": attempted_at,
        "timestamp_source": timestamp_source,
        "outcome": outcome,
        "error": error,
        "quality": quality,
        "evidence_sha256": evidence_sha,
        "evidence": safe_evidence,
    }


def _resolve_full_entry_id(
    ledger: list[dict[str, Any]], attempt_id: Any
) -> str | None:
    """Resolve a backward-only content reference to its verified full owner."""
    by_id = {
        str(entry.get("attempt_id")): (index, entry)
        for index, entry in enumerate(ledger)
        if entry.get("attempt_id")
    }
    current = str(attempt_id or "")
    seen: set[str] = set()
    while current and current not in seen and current in by_id:
        seen.add(current)
        index, entry = by_id[current]
        evidence = entry.get("evidence")
        if isinstance(evidence, Mapping):
            if entry.get("evidence_sha256") != _sha256(evidence):
                return None
            return current
        referenced = str(entry.get("evidence_ref_attempt_id") or "")
        target = by_id.get(referenced)
        if not target or target[0] >= index:
            return None
        if target[1].get("evidence_sha256") != entry.get("evidence_sha256"):
            return None
        current = referenced
    return None


def _deep_selection_sha(evidence: Mapping[str, Any]) -> str:
    """Hash the homepage/deep view without its independently selected quote."""
    return _sha256(
        {
            key: value
            for key, value in evidence.items()
            if key not in _PRICING_FIELDS
        }
    )


def _pricing_bundle(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: evidence[key]
        for key in _PRICING_FIELDS
        if key in evidence
    }


def _pricing_selection_sha(evidence: Mapping[str, Any]) -> str:
    return _sha256(_pricing_bundle(evidence))


def _full_evidence_for_id(
    ledger: list[dict[str, Any]], attempt_id: Any
) -> Mapping[str, Any] | None:
    owner_id = _resolve_full_entry_id(ledger, attempt_id)
    if not owner_id:
        return None
    return next(
        (
            entry["evidence"]
            for entry in ledger
            if entry.get("attempt_id") == owner_id
            and isinstance(entry.get("evidence"), Mapping)
        ),
        None,
    )


def _find_full_entry_id_by_deep_sha(
    ledger: list[dict[str, Any]], deep_sha: str
) -> str | None:
    """Find the newest verified full owner for an independently selected deep view."""
    return next(
        (
            str(entry.get("attempt_id"))
            for entry in reversed(ledger)
            if isinstance(entry.get("evidence"), Mapping)
            and entry.get("evidence_sha256") == _sha256(entry["evidence"])
            and _deep_selection_sha(entry["evidence"]) == deep_sha
            and _resolve_full_entry_id(ledger, entry.get("attempt_id"))
            == entry.get("attempt_id")
        ),
        None,
    )


def _find_full_entry_id_by_pricing_sha(
    ledger: list[dict[str, Any]], pricing_sha: str
) -> str | None:
    """Find the newest verified full owner for an independent pricing bundle."""
    return next(
        (
            str(entry.get("attempt_id"))
            for entry in reversed(ledger)
            if isinstance(entry.get("evidence"), Mapping)
            and entry.get("evidence_sha256") == _sha256(entry["evidence"])
            and _pricing_selection_sha(entry["evidence"]) == pricing_sha
            and _resolve_full_entry_id(ledger, entry.get("attempt_id"))
            == entry.get("attempt_id")
        ),
        None,
    )


def _synthetic_same_window_entry(
    old: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    old_attempt_id: str,
    current_attempt_id: str,
    attempted_at: str,
    ordinal: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return a full synthetic entry only for a safe, monotonic exact-window merge."""
    if not deep_evidence_merge.windows_match(old, current):
        return None
    old_quality = summarize_quality(old, ordinal=0)
    current_quality = summarize_quality(current, ordinal=0)
    try:
        result = deep_evidence_merge.merge_same_window(
            old,
            current,
            old_attempt_id=old_attempt_id,
            current_attempt_id=current_attempt_id,
            old_quality=old_quality,
            current_quality=current_quality,
            generated_at=attempted_at,
        )
    except deep_evidence_merge.EvidenceMergeError:
        # Missing cached translations or another policy precondition leaves the
        # normal whole-attempt selector in force; never partially merge.
        return None
    merged_candidate = copy.deepcopy(result.candidate)
    merged_candidate.pop(_LEGACY_MERGE_PROVENANCE_FIELD, None)
    merged_evidence = _extract_attempt_evidence(merged_candidate)
    merged_quality = summarize_quality(merged_evidence, ordinal=0)
    if tuple(merged_quality["selection_tuple"][:-1]) <= max(
        tuple(old_quality["selection_tuple"][:-1]),
        tuple(current_quality["selection_tuple"][:-1]),
    ):
        return None
    entry = _new_entry(
        attempted_at=attempted_at,
        outcome="synthetic_merge",
        error=None,
        evidence=merged_evidence,
        ordinal=ordinal,
    )
    # Synthetic canonical evidence is always stored in full.  Its provenance
    # points only to already-persistable attempt IDs, making the ledger closed.
    entry["synthetic"] = True
    entry["merge_provenance"] = _json_safe(result.provenance)
    entry["merge_detail"] = _json_safe(result.detail)
    return merged_evidence, entry


def _compose_merge_candidate(
    evidence: Mapping[str, Any],
    *shallow_sources: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach stable shallow inputs needed to recompute derived deep metrics."""
    candidate: dict[str, Any] = {}
    for source in shallow_sources:
        for key, value in source.items():
            if (
                key not in _QUEUE_FIELDS
                and key not in _METADATA_FIELDS
                and not _is_deep_field(str(key))
            ):
                candidate[str(key)] = copy.deepcopy(value)
    candidate.update(copy.deepcopy(dict(evidence)))
    return candidate


def _comment_ref(entry: Any) -> tuple[str, str] | None:
    if isinstance(entry, Mapping):
        url = entry.get("url") or entry.get("code") or entry.get("post_url")
    else:
        url = entry
    identity = _media_identity(url)
    if not identity:
        return None
    return identity, str(url or f"/p/{identity}/")


def _as_entries(value: Any) -> list[Any]:
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return []


def _comment_observations(
    evidence: Mapping[str, Any],
) -> dict[str, tuple[str, str, int | None]]:
    """Return actual per-media comment outcomes; navigation failures are no-op."""
    observed: dict[str, tuple[str, str, int | None]] = {}

    def set_state(
        value: Any, state: str, reported_count: Any = None
    ) -> None:
        ref = _comment_ref(value)
        if ref:
            try:
                count = int(reported_count) if reported_count is not None else None
            except (TypeError, ValueError):
                count = None
            if state == "terminal_unavailable" and count not in (1, 2):
                state = "retry_pending"
            observed[ref[0]] = (state, ref[1], count)

    # An explicit comment_failed_posts row is a real comment extraction failure.
    # deep_failed_posts is intentionally ignored: navigation is not a comment try.
    for value in _as_entries(evidence.get("comment_failed_posts")):
        set_state(value, "retry_pending")

    unavailable_by_identity: dict[str, Mapping[str, Any]] = {}
    for value in _as_entries(evidence.get("comment_unavailable_posts")):
        reported = value.get("reported_count") if isinstance(value, Mapping) else None
        # The matching per-post status + provenance below is authoritative.
        # An aggregate row alone can never manufacture a terminal state.
        set_state(value, "retry_pending", reported)
        ref = _comment_ref(value)
        if ref and isinstance(value, Mapping):
            unavailable_by_identity[ref[0]] = value

    # Per-post status is the authoritative reset/terminal signal and overrides
    # legacy aggregate lists when both exist.
    for post in _as_entries(evidence.get("sampled_posts")):
        if not isinstance(post, Mapping):
            continue
        status = str(post.get("comment_sampling_status") or "")
        if status in {"collected", "verified_zero"}:
            set_state(post, "resolved", post.get("comment_count"))
        elif status.startswith("failed_"):
            set_state(post, "retry_pending", post.get("comment_count"))
        elif status == "unavailable_after_retry":
            ref = _comment_ref(post)
            entry = unavailable_by_identity.get(ref[0]) if ref else None
            set_state(
                post,
                "terminal_unavailable"
                if comment_semantics.repeated_low_comment_unavailable(
                    post, entry
                )
                else "retry_pending",
                post.get("comment_count"),
            )
        elif status == "verified_empty_thread":
            ref = _comment_ref(post)
            entry = unavailable_by_identity.get(ref[0]) if ref else None
            set_state(
                post,
                "resolved"
                if comment_semantics.verified_empty_thread_evidence(post, entry)
                else "retry_pending",
                post.get("comment_count"),
            )
    return observed


def _load_comment_retry_state(
    prior: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    raw_rows = prior.get(COMMENT_RETRY_STATE_FIELD)
    legacy = False
    if not isinstance(raw_rows, list):
        raw_rows = prior.get(_LEGACY_COMMENT_RETRY_HISTORY_FIELD)
        legacy = isinstance(raw_rows, list)
    state: dict[str, dict[str, Any]] = {}
    for row in raw_rows or []:
        if not isinstance(row, Mapping):
            continue
        identity = str(row.get("identity") or "").strip()
        if not identity:
            continue
        value = str(row.get("state") or "")
        if value not in {"resolved", "retry_pending", "terminal_unavailable"}:
            value = "retry_pending" if legacy else "resolved"
        streak = _as_count(row.get("failure_streak"))
        state[identity] = {
            "identity": identity,
            "url": str(row.get("url") or f"/p/{identity}/"),
            "state": value,
            "failure_streak": streak if value != "resolved" else 0,
        }
    if state:
        return state

    # First migration: reconstruct the current state from the persisted canonical
    # evidence once.  Subsequent attempts advance only from the compact state.
    return _advance_comment_retry_state({}, prior)


def _advance_comment_retry_state(
    existing: Mapping[str, Mapping[str, Any]],
    evidence: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    state = {
        str(identity): _json_safe(copy.deepcopy(row))
        for identity, row in existing.items()
        if identity and isinstance(row, Mapping)
    }
    for identity, (outcome, url, reported_count) in _comment_observations(evidence).items():
        previous = state.get(identity) or {}
        previous_state = previous.get("state")
        previous_streak = _as_count(previous.get("failure_streak"))
        if outcome == "resolved":
            next_state = "resolved"
            streak = 0
        elif outcome == "terminal_unavailable":
            next_state = "terminal_unavailable"
            streak = max(2, previous_streak)
        elif previous_state == "terminal_unavailable" and reported_count in (1, 2, None):
            # Terminal low-count evidence is sticky until a real success/zero reset.
            next_state = "terminal_unavailable"
            streak = previous_streak
        else:
            next_state = "retry_pending"
            streak = previous_streak + 1 if previous_state == "retry_pending" else 1
        state[identity] = {
            "identity": identity,
            "url": url or previous.get("url") or f"/p/{identity}/",
            "state": next_state,
            "failure_streak": streak,
        }
    return state


def _serialize_comment_retry_state(
    state: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [copy.deepcopy(state[identity]) for identity in sorted(state)]


def prepare_stage3_attempt(candidate: Mapping[str, Any]) -> Stage3AttemptContext:
    """Snapshot persisted evidence and return an isolated retry work copy."""
    prior = _json_safe(copy.deepcopy(candidate))
    retry_state = _load_comment_retry_state(prior)
    working = copy.deepcopy(prior)

    # The collector input is deliberately narrower than persisted output.  Old
    # translations, ER, comments and completeness counters must not masquerade as
    # evidence from the new attempt.  Target refs and the independent pricing
    # bundle remain valid retry inputs.
    for key in list(working):
        if (
            key in _METADATA_FIELDS
            or key == _LEGACY_COMMENT_RETRY_HISTORY_FIELD
            or (_is_deep_field(key) and key not in _STAGE3_INPUT_DEEP_FIELDS)
        ):
            working.pop(key, None)

    # Browser convergence consumes these two legacy-compatible inputs.  Pending
    # failures get one more real comment try; terminal low-count media remain
    # terminal across retries.  Resolved media are never injected.
    pending = [
        row["url"]
        for row in retry_state.values()
        if row.get("state") == "retry_pending"
    ]
    terminal = [
        {
            "url": row["url"],
            "reason": "historical_terminal_unavailable",
        }
        for row in retry_state.values()
        if row.get("state") == "terminal_unavailable"
    ]
    if pending:
        working["comment_failed_posts"] = pending
    if terminal:
        working["comment_unavailable_posts"] = terminal

    return Stage3AttemptContext(
        prior_snapshot=copy.deepcopy(prior),
        input_snapshot=copy.deepcopy(working),
        candidate=working,
    )


def finalize_stage3_verdict(
    context: Stage3AttemptContext,
    verdict: tuple,
    *,
    attempted_at: str | None = None,
    allow_same_window_merge: bool = True,
) -> tuple:
    """Append an attempt and select/merge a monotonic canonical deep view."""
    if not verdict or verdict[0] not in {"advance", "reject", "error"}:
        raise ValueError(f"unsupported Stage3 verdict: {verdict!r}")
    now = attempted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    has_explicit_candidate = len(verdict) > 2 and isinstance(verdict[2], Mapping)
    attempt_candidate = (
        verdict[2] if has_explicit_candidate else context.candidate
    )
    attempt_candidate = _json_safe(copy.deepcopy(attempt_candidate))
    if has_explicit_candidate:
        current_evidence = _extract_attempt_evidence(attempt_candidate)
    else:
        current_evidence = _complete_partial_target_descriptor(
            _evidence_delta(context.input_snapshot, attempt_candidate),
            before=context.input_snapshot,
            after=attempt_candidate,
        )
    prior = copy.deepcopy(context.prior_snapshot)
    prior_evidence = _extract_attempt_evidence(prior)
    ledger = [
        _json_safe(entry)
        for entry in (prior.get(LEDGER_FIELD) or [])
        if isinstance(entry, Mapping)
    ]

    baseline_id = _resolve_full_entry_id(
        ledger, prior.get(CANONICAL_ATTEMPT_FIELD)
    )
    prior_deep_sha = _deep_selection_sha(prior_evidence)
    if baseline_id:
        _, baseline_entry = next(
            (index, entry)
            for index, entry in enumerate(ledger)
            if entry.get("attempt_id") == baseline_id
        )
        baseline_evidence = baseline_entry.get("evidence")
        if (
            not isinstance(baseline_evidence, Mapping)
            or _deep_selection_sha(baseline_evidence) != prior_deep_sha
        ):
            baseline_id = None
    if baseline_id is None and _has_attempt_evidence(prior_evidence):
        baseline_id = _find_full_entry_id_by_deep_sha(ledger, prior_deep_sha)
    if baseline_id is None and _has_attempt_evidence(prior_evidence):
        baseline_at = _best_evidence_timestamp(prior_evidence, now)
        baseline = _new_entry(
            attempted_at=baseline_at,
            outcome="historical_baseline",
            error=None,
            evidence=prior_evidence,
            ordinal=len(ledger),
            timestamp_source=(
                "evidence" if baseline_at != now else "ledger_migration"
            ),
        )
        ledger.append(baseline)
        baseline_id = baseline["attempt_id"]

    prior_pricing_id = None
    prior_has_pricing = any(key in prior_evidence for key in _PRICING_FIELDS)
    if prior_has_pricing:
        prior_pricing_sha = _pricing_selection_sha(prior_evidence)
        prior_pricing_id = _resolve_full_entry_id(
            ledger, prior.get(PRICING_CANONICAL_ATTEMPT_FIELD)
        )
        pricing_owner = _full_evidence_for_id(ledger, prior_pricing_id)
        if (
            pricing_owner is None
            or _pricing_selection_sha(pricing_owner) != prior_pricing_sha
        ):
            prior_pricing_id = _find_full_entry_id_by_pricing_sha(
                ledger, prior_pricing_sha
            )
        if prior_pricing_id is None:
            # Legacy/corrupt metadata can leave a composed top-level quote with
            # no full owner.  Materialize it once before recording the new try.
            pricing_baseline_at = _best_evidence_timestamp(prior_evidence, now)
            pricing_baseline = _new_entry(
                attempted_at=pricing_baseline_at,
                outcome="historical_pricing_baseline",
                error=None,
                evidence=prior_evidence,
                ordinal=len(ledger),
                timestamp_source=(
                    "evidence"
                    if pricing_baseline_at != now
                    else "ledger_migration"
                ),
            )
            ledger.append(pricing_baseline)
            prior_pricing_id = pricing_baseline["attempt_id"]

    kind = str(verdict[0])
    error = str(verdict[1]) if kind == "error" else None
    current_entry = _new_entry(
        attempted_at=now,
        outcome=kind,
        error=error,
        evidence=current_evidence,
        ordinal=len(ledger),
    )
    duplicate = next(
        (
            entry
            for entry in ledger
            if entry.get("evidence_sha256") == current_entry["evidence_sha256"]
            and entry.get("attempt_id")
            and isinstance(entry.get("evidence"), Mapping)
        ),
        None,
    )
    if duplicate is not None:
        # Every attempt still has its own immutable event, but identical payloads
        # point at the first copy instead of multiplying stage_json size.
        current_entry["evidence_ref_attempt_id"] = duplicate["attempt_id"]
        current_entry.pop("evidence", None)
    ledger.append(current_entry)

    # ``baseline_id`` was already verified against the exact top-level prior
    # evidence (or freshly materialized for a composed deep/pricing view).  Do
    # not resurrect a stale but structurally valid canonical pointer here.
    prior_id = baseline_id
    prior_entry_index = next(
        (
            index
            for index, entry in enumerate(ledger[:-1])
            if entry.get("attempt_id") == prior_id
        ),
        None,
    )
    if prior_entry_index is None and prior_evidence:
        prior_deep_sha = _deep_selection_sha(prior_evidence)
        recovered_prior_id = _find_full_entry_id_by_deep_sha(
            ledger[:-1], prior_deep_sha
        )
        prior_entry_index = next(
            (
                index
                for index, entry in enumerate(ledger[:-1])
                if entry.get("attempt_id") == recovered_prior_id
            ),
            None,
        )
    prior_entry = (
        ledger[prior_entry_index]
        if prior_entry_index is not None
        else {}
    )
    prior_quality = summarize_quality(
        prior_evidence,
        attempted_at=prior_entry.get("attempted_at"),
        ordinal=prior_entry_index or 0,
    )
    current_quality = current_entry["quality"]
    prior_tuple = tuple(prior_quality["selection_tuple"])
    current_tuple = tuple(current_quality["selection_tuple"])
    prior_has_evidence = _has_attempt_evidence(prior_evidence)
    current_has_evidence = _has_attempt_evidence(current_evidence)
    current_wins = current_has_evidence and (
        not prior_has_evidence or current_tuple > prior_tuple
    )
    canonical_evidence = current_evidence if current_wins else prior_evidence

    # Pricing comes from a distinct Reels-only window.  Select that three-field
    # bundle independently; mixing it with the homepage window would either lose
    # a newer quote or let quote quality decide which comment window survives.
    prior_pricing_view = {
        "handle": prior.get("handle"),
        **prior_evidence,
    }
    current_pricing_view = {
        "handle": attempt_candidate.get("handle") or prior.get("handle"),
        **current_evidence,
    }
    prior_pricing_quality = _pricing_quality(prior_pricing_view)
    current_pricing_quality = _pricing_quality(current_pricing_view)
    pricing_source, _ = deep_evidence_merge.select_pricing(
        prior_pricing_view, current_pricing_view
    )
    current_pricing_wins = pricing_source == "current"
    pricing_evidence = current_evidence if current_pricing_wins else prior_evidence
    pricing_quality = (
        current_pricing_quality
        if current_pricing_wins
        else prior_pricing_quality
    )

    canonical_id = prior_id
    prior_source_id = prior_id
    if not prior_source_id and prior_entry_index is not None:
        prior_source_id = ledger[prior_entry_index].get("attempt_id")
    if current_wins:
        canonical_id = _resolve_full_entry_id(
            ledger, current_entry["attempt_id"]
        )
    elif not canonical_id:
        canonical_id = baseline_id
        if not canonical_id:
            prior_deep_sha = _deep_selection_sha(prior_evidence)
            canonical_id = _find_full_entry_id_by_deep_sha(
                ledger[:-1], prior_deep_sha
            )

    synthetic_entry = None
    runtime_stage3_attempt = kind == "error" or (
        kind == "advance" and verdict[1] == "collected"
    )
    known_attempt_ids = {
        str(entry.get("attempt_id"))
        for entry in ledger
        if entry.get("attempt_id")
    }
    if (
        allow_same_window_merge
        and runtime_stage3_attempt
        and prior_source_id
        and str(prior_source_id) in known_attempt_ids
        and current_entry["attempt_id"] in known_attempt_ids
        and _resolve_full_entry_id(ledger, current_entry["attempt_id"])
    ):
        old_merge_candidate = _compose_merge_candidate(prior_evidence, prior)
        current_merge_candidate = _compose_merge_candidate(
            current_evidence,
            prior,
            attempt_candidate,
        )
        synthetic = _synthetic_same_window_entry(
            old_merge_candidate,
            current_merge_candidate,
            old_attempt_id=str(prior_source_id),
            current_attempt_id=current_entry["attempt_id"],
            attempted_at=now,
            ordinal=len(ledger),
        )
        if synthetic is not None:
            canonical_evidence, synthetic_entry = synthetic
            ledger.append(synthetic_entry)
            canonical_id = synthetic_entry["attempt_id"]

    # Start from the immutable pre-run snapshot, overlay only non-deep refreshes,
    # then install exactly one attempt's deep window.  No cross-window post merge.
    canonical = copy.deepcopy(prior)
    for key, value in attempt_candidate.items():
        if (
            key not in _QUEUE_FIELDS
            and key not in _METADATA_FIELDS
            and not _is_deep_field(key)
        ):
            canonical[key] = copy.deepcopy(value)
    for key in STAGE3_RESET_FIELDS:
        canonical.pop(key, None)
    for key in list(canonical):
        if _is_deep_field(key):
            canonical.pop(key, None)
    canonical.update(copy.deepcopy(canonical_evidence))
    for key in _PRICING_FIELDS:
        canonical.pop(key, None)
        if key in pricing_evidence:
            canonical[key] = copy.deepcopy(pricing_evidence[key])

    canonical[LEDGER_FIELD] = ledger
    if canonical_id:
        canonical[CANONICAL_ATTEMPT_FIELD] = canonical_id
    canonical[CANONICAL_QUALITY_FIELD] = summarize_quality(
        canonical,
        attempted_at=(
            synthetic_entry.get("attempted_at")
            if synthetic_entry is not None
            else (
                current_entry.get("attempted_at")
                if current_wins
                else prior_entry.get("attempted_at")
            )
        ),
        ordinal=(
            len(ledger) - 1
            if synthetic_entry is not None or current_wins
            else prior_entry_index or 0
        ),
    )
    pricing_id = (
        _resolve_full_entry_id(ledger, current_entry["attempt_id"])
        if current_pricing_wins
        else prior_pricing_id
    )
    pricing_id = _resolve_full_entry_id(ledger, pricing_id)
    if pricing_id and any(key in pricing_evidence for key in _PRICING_FIELDS):
        canonical[PRICING_CANONICAL_ATTEMPT_FIELD] = pricing_id
    estimate = pricing_evidence.get("pricing_estimate")
    estimate = estimate if isinstance(estimate, Mapping) else {}
    canonical[PRICING_CANONICAL_QUALITY_FIELD] = {
        "selection_tuple": list(pricing_quality),
        "status": estimate.get("status"),
        "sample_count": pricing_quality[1],
        "captured_at": pricing_evidence.get("pricing_captured_at"),
    }
    retry_state = _advance_comment_retry_state(
        _load_comment_retry_state(prior), current_evidence
    )
    canonical[COMMENT_RETRY_STATE_FIELD] = _serialize_comment_retry_state(
        retry_state
    )
    canonical.pop(_LEGACY_COMMENT_RETRY_HISTORY_FIELD, None)
    for key in _QUEUE_FIELDS:
        canonical.pop(key, None)

    # Canonical pointers must always close onto immutable full evidence.  A
    # ref-only event remains useful audit history, never a canonical owner.
    if canonical_id and _resolve_full_entry_id(ledger, canonical_id) != canonical_id:
        raise ValueError("deep canonical attempt does not resolve to full evidence")
    canonical_owner = _full_evidence_for_id(ledger, canonical_id)
    if canonical_id and (
        canonical_owner is None
        or _deep_selection_sha(canonical_owner)
        != _deep_selection_sha(_extract_attempt_evidence(canonical))
    ):
        raise ValueError("deep canonical attempt evidence does not match canonical")
    if pricing_id and _resolve_full_entry_id(ledger, pricing_id) != pricing_id:
        raise ValueError("pricing canonical attempt does not resolve to full evidence")
    pricing_owner = _full_evidence_for_id(ledger, pricing_id)
    if pricing_id and (
        pricing_owner is None
        or _pricing_selection_sha(pricing_owner)
        != _pricing_selection_sha(_extract_attempt_evidence(canonical))
    ):
        raise ValueError("pricing canonical attempt evidence does not match canonical")

    output_kind = kind
    output_value = verdict[1]
    if (
        synthetic_entry is not None
        and kind == "error"
        and str(verdict[1]).startswith("deep_incomplete:")
    ):
        from extensions.sop_v2 import creator_cache as cc

        target_posts = _as_count(canonical.get("deep_target_posts")) or 10
        strict_reasons = cc.strict_deep_reasons(
            canonical,
            status="collected",
            target_posts=target_posts,
        )
        if not strict_reasons:
            output_kind = "advance"
            output_value = "collected"
            synthetic_entry["resolution"] = {
                "from_error": str(verdict[1]),
                "promoted_to": "collected",
                "strict_target_posts": target_posts,
            }

    return (output_kind, output_value, _json_safe(canonical))


def finalize_pricing_only_attempt(
    prior_candidate: Mapping[str, Any],
    attempted_candidate: Mapping[str, Any],
    *,
    attempted_at: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Append a pricing-only event without advancing any deep/comment state.

    The pricing bundle is selected monotonically and receives its own immutable
    full owner.  The existing deep canonical owner/window stays unchanged; its
    composed quality summary is refreshed only because that summary also exposes
    the independently selected pricing rank.  Comment retry state is copied byte
    for byte and never passed through the Stage 3 retry transition function.
    """
    now = attempted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    prior = _json_safe(copy.deepcopy(prior_candidate))
    attempted = _json_safe(copy.deepcopy(attempted_candidate))
    prior_evidence = _extract_attempt_evidence(prior)
    current_pricing = _pricing_bundle(attempted)
    ledger = [
        _json_safe(entry)
        for entry in (prior.get(LEDGER_FIELD) or [])
        if isinstance(entry, Mapping)
    ]
    if not ledger:
        raise ValueError("pricing-only finalizer requires an existing deep ledger")

    canonical_id = str(prior.get(CANONICAL_ATTEMPT_FIELD) or "")
    canonical_owner = _full_evidence_for_id(ledger, canonical_id)
    if (
        not canonical_id
        or canonical_owner is None
        or _resolve_full_entry_id(ledger, canonical_id) != canonical_id
        or _deep_selection_sha(canonical_owner)
        != _deep_selection_sha(prior_evidence)
    ):
        raise ValueError("pricing-only finalizer found unclosed deep canonical owner")
    canonical_event = next(
        (
            (index, entry)
            for index, entry in enumerate(ledger)
            if entry.get("attempt_id") == canonical_id
        ),
        None,
    )
    if canonical_event is None:
        raise ValueError("pricing-only finalizer cannot locate deep canonical event")

    prior_pricing = _pricing_bundle(prior_evidence)
    prior_pricing_id = _resolve_full_entry_id(
        ledger, prior.get(PRICING_CANONICAL_ATTEMPT_FIELD)
    )
    prior_pricing_owner = _full_evidence_for_id(ledger, prior_pricing_id)
    if prior_pricing and (
        prior_pricing_owner is None
        or _pricing_selection_sha(prior_pricing_owner)
        != _pricing_selection_sha(prior_evidence)
    ):
        # A legacy composed quote may predate the independent pricing pointer.
        # Materialize it as one immutable full owner without changing deep or
        # comment state, then use that owner for monotonic selection.
        baseline_at = _best_evidence_timestamp(prior_evidence, now)
        pricing_baseline = _new_entry(
            attempted_at=baseline_at,
            outcome="historical_pricing_baseline",
            error=None,
            evidence={"handle": prior.get("handle"), **prior_pricing},
            ordinal=len(ledger),
            timestamp_source=(
                "evidence" if baseline_at != now else "ledger_migration"
            ),
        )
        pricing_baseline["pricing_only"] = True
        ledger.append(pricing_baseline)
        prior_pricing_id = pricing_baseline["attempt_id"]

    current_entry = _new_entry(
        attempted_at=now,
        outcome="pricing_only",
        error=str(error) if error else None,
        evidence={
            "handle": prior.get("handle") or attempted.get("handle"),
            **current_pricing,
        },
        ordinal=len(ledger),
    )
    current_entry["pricing_only"] = True
    # Always retain the attempted bundle in full.  A pricing canonical pointer
    # must never depend on a ref-only/deduplicated event created by this tool.
    ledger.append(current_entry)

    prior_pricing_candidate = {
        "handle": prior.get("handle"),
        **copy.deepcopy(prior_pricing),
    }
    current_pricing_candidate = {
        "handle": prior.get("handle") or attempted.get("handle"),
        **copy.deepcopy(current_pricing),
    }
    prior_quality = _pricing_quality(prior_pricing_candidate)
    current_quality = _pricing_quality(current_pricing_candidate)
    source, selected_pricing = deep_evidence_merge.select_pricing(
        prior_pricing_candidate, current_pricing_candidate
    )
    # Integrity failures are rank zero.  They remain audit events but can never
    # become the top-level quote, including when no prior valid quote exists.
    current_wins = source == "current" and current_quality[0] > 0
    if not current_wins:
        selected_pricing = prior_pricing
    selected_quality = current_quality if current_wins else prior_quality
    selected_pricing_id = (
        current_entry["attempt_id"] if current_wins else prior_pricing_id
    )

    canonical = copy.deepcopy(prior)
    for key in _PRICING_FIELDS:
        canonical.pop(key, None)
        if key in selected_pricing:
            canonical[key] = copy.deepcopy(selected_pricing[key])
    canonical[LEDGER_FIELD] = ledger
    if selected_pricing:
        selected_pricing_id = _resolve_full_entry_id(
            ledger, selected_pricing_id
        )
        if not selected_pricing_id:
            raise ValueError("pricing-only canonical owner is unresolved")
        canonical[PRICING_CANONICAL_ATTEMPT_FIELD] = selected_pricing_id
    else:
        canonical.pop(PRICING_CANONICAL_ATTEMPT_FIELD, None)
    estimate = canonical.get("pricing_estimate")
    estimate = estimate if isinstance(estimate, Mapping) else {}
    canonical[PRICING_CANONICAL_QUALITY_FIELD] = {
        "selection_tuple": list(selected_quality),
        "status": estimate.get("status"),
        "sample_count": selected_quality[1],
        "captured_at": canonical.get("pricing_captured_at"),
    }
    canonical_index, canonical_entry = canonical_event
    canonical[CANONICAL_QUALITY_FIELD] = summarize_quality(
        canonical,
        attempted_at=canonical_entry.get("attempted_at"),
        ordinal=canonical_index,
    )

    # Closure assertions run before the caller's lock-token CAS.  Any violation
    # raises and therefore leaves both stage_json and its lock-owned state intact.
    if canonical.get(COMMENT_RETRY_STATE_FIELD) != prior.get(
        COMMENT_RETRY_STATE_FIELD
    ):
        raise ValueError("pricing-only finalizer changed comment retry state")
    if canonical.get(CANONICAL_ATTEMPT_FIELD) != prior.get(
        CANONICAL_ATTEMPT_FIELD
    ):
        raise ValueError("pricing-only finalizer changed deep canonical pointer")
    final_deep = _extract_attempt_evidence(canonical)
    if _deep_selection_sha(canonical_owner) != _deep_selection_sha(final_deep):
        raise ValueError("pricing-only finalizer changed deep canonical evidence")
    pricing_owner = _full_evidence_for_id(
        ledger, canonical.get(PRICING_CANONICAL_ATTEMPT_FIELD)
    )
    if selected_pricing and (
        pricing_owner is None
        or _pricing_selection_sha(pricing_owner)
        != _pricing_selection_sha(final_deep)
    ):
        raise ValueError("pricing-only finalizer pricing owner mismatch")
    return _json_safe(canonical)


__all__ = [
    "ATTEMPT_SCHEMA",
    "CANONICAL_ATTEMPT_FIELD",
    "CANONICAL_QUALITY_FIELD",
    "COMMENT_RETRY_HISTORY_FIELD",
    "COMMENT_RETRY_STATE_FIELD",
    "LEDGER_FIELD",
    "PRICING_CANONICAL_ATTEMPT_FIELD",
    "PRICING_CANONICAL_QUALITY_FIELD",
    "STAGE3_RESET_FIELDS",
    "Stage3AttemptContext",
    "finalize_pricing_only_attempt",
    "finalize_stage3_verdict",
    "prepare_stage3_attempt",
    "summarize_quality",
    "window_fingerprint",
]
