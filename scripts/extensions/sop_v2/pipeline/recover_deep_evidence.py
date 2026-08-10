#!/usr/bin/env python3
"""CAS-protected offline recovery of complementary Stage 3 evidence.

Inspection always uses SQLite URI read-only connections.  The default is a dry
run; ``--apply`` validates the complete cohort and writes it in one
``BEGIN IMMEDIATE`` transaction.  Media/comment merge policy lives in
``deep_evidence_merge`` so normal retry convergence can reuse the same rules.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2.config import config_sha256, load_config  # noqa: E402
from extensions.sop_v2.pipeline import deep_attempts  # noqa: E402
from extensions.sop_v2.pipeline import deep_evidence_merge  # noqa: E402


AUDIT_SCHEMA = "sop-v2-deep-evidence-recovery-v1"
MAX_HANDLES = 1000
_HANDLE_RE = re.compile(r"[A-Za-z0-9._]{1,30}\Z")
_LOCAL_CAPTURE_TZ = timezone(timedelta(hours=8))
_ATTEMPT_META_FIELDS = set(deep_attempts.STAGE3_RESET_FIELDS)

# These four are the deep-derived subset of creator_cache._HOT_COLS.  Recovery
# intentionally leaves every other relational column untouched.
HOT_EVIDENCE_COLUMNS = (
    "real_er",
    "high_intent_count",
    "evidence_dir",
    "core_niche_key",
)
PROTECTED_COLUMNS = (
    "status",
    "stage_error",
    "locked_at",
    "stage_updated_at",
    "times_seen",
    "discovery_batch",
    "source_batch",
    "tier",
    "client_status",
    "approved_at",
    "rejected_reason",
    "client_rejection_scope",
    "client_note",
    "reject_reason",
)


class RecoveryError(RuntimeError):
    """A recovery precondition, evidence check, or atomic CAS failed."""


@dataclass(frozen=True)
class RowSnapshot:
    handle: str
    raw_stage_json: str
    stage: dict[str, Any]
    stage_sha256: str
    stage_error: str | None
    stage_updated_at: str | None
    protected: dict[str, Any]


@dataclass(frozen=True)
class PlannedRow:
    current: RowSnapshot
    backup: RowSnapshot
    after_stage: dict[str, Any]
    after_raw_stage_json: str
    after_stage_sha256: str
    hot_values: dict[str, Any]
    audit: dict[str, Any]
    write_required: bool


@dataclass(frozen=True)
class RecoveryPlan:
    db_path: Path
    backup_db_path: Path
    batch_id: str
    config_sha256: str
    rows: tuple[PlannedRow, ...]
    plan_sha256: str


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


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
        raise RecoveryError(f"recovered evidence is not canonical JSON: {exc}") from exc


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: Any) -> str:
    return _sha_text(_canonical_json(value))


def _current_cas_sha256(snapshot: RowSnapshot) -> str:
    """Bind a reviewed plan to every target value that apply protects."""
    return _sha_json(
        {
            "stage_json_sha256": snapshot.stage_sha256,
            "stage_error": snapshot.stage_error,
            "protected": snapshot.protected,
        }
    )


def _load_stage(raw: Any, *, database: Path, handle: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise RecoveryError(f"@{handle}: missing stage_json in {database.name}")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            f"@{handle}: invalid stage_json in {database.name}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise RecoveryError(
            f"@{handle}: stage_json must be an object in {database.name}"
        )
    return value


def _ro_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RecoveryError(f"database does not exist: {resolved}")
    try:
        conn = sqlite3.connect(
            resolved.as_uri() + "?mode=ro", uri=True, timeout=30
        )
    except sqlite3.Error as exc:
        raise RecoveryError(f"cannot open database read-only: {resolved}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _validate_database(conn: sqlite3.Connection, path: Path) -> None:
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='creator_profiles'"
        ).fetchone()
        if not table:
            raise RecoveryError(f"creator_profiles missing in {path.name}")
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(creator_profiles)")
        }
        required = {
            "handle",
            "stage_json",
            *HOT_EVIDENCE_COLUMNS,
            *PROTECTED_COLUMNS,
        }
        missing = sorted(required - columns)
        if missing:
            raise RecoveryError(f"columns missing in {path.name}: {missing}")
        check = [row[0] for row in conn.execute("PRAGMA quick_check")]
        if check != ["ok"]:
            raise RecoveryError(
                f"SQLite quick_check failed for {path.name}: {check[:3]}"
            )
    except sqlite3.Error as exc:
        raise RecoveryError(f"cannot inspect {path.name}: {exc}") from exc


def _normalize_handle(value: str) -> str:
    handle = str(value or "").strip().lstrip("@")
    if not _HANDLE_RE.fullmatch(handle):
        raise RecoveryError(f"invalid handle: {value!r}")
    return handle


def _normalize_handles(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        handle = _normalize_handle(value)
        folded = handle.casefold()
        if folded in seen:
            raise RecoveryError(f"duplicate handle: @{handle}")
        seen.add(folded)
        output.append(handle)
    if not output:
        raise RecoveryError("at least one explicit handle is required")
    if len(output) > MAX_HANDLES:
        raise RecoveryError(f"too many handles ({len(output)} > {MAX_HANDLES})")
    return tuple(output)


def _load_handles_json(value: str) -> tuple[str, ...]:
    candidate = Path(value).expanduser()
    try:
        raw = candidate.read_text(encoding="utf-8") if candidate.is_file() else value
    except OSError as exc:
        raise RecoveryError(f"cannot read handles JSON: {candidate}") from exc
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("--handles-json must be a JSON array or JSON file") from exc
    if not isinstance(payload, list) or not all(isinstance(row, str) for row in payload):
        raise RecoveryError("--handles-json must contain an array of strings")
    return _normalize_handles(payload)


def _fetch_snapshot(
    conn: sqlite3.Connection,
    *,
    database: Path,
    batch_id: str,
    requested_handle: str,
    require_qualified: bool,
) -> RowSnapshot:
    columns = list(dict.fromkeys(("handle", "stage_json", *PROTECTED_COLUMNS)))
    rows = conn.execute(
        f"SELECT {','.join(columns)} FROM creator_profiles "
        "WHERE handle=? COLLATE NOCASE",
        (requested_handle,),
    ).fetchall()
    if len(rows) != 1:
        raise RecoveryError(
            f"@{requested_handle}: expected exactly one row in {database.name}, "
            f"found {len(rows)}"
        )
    row = rows[0]
    if row["discovery_batch"] != batch_id:
        raise RecoveryError(
            f"@{row['handle']}: batch mismatch in {database.name}; "
            f"expected {batch_id!r}, got {row['discovery_batch']!r}"
        )
    if require_qualified and row["status"] != "qualified":
        raise RecoveryError(
            f"@{row['handle']}: target status must be qualified, got {row['status']!r}"
        )
    if require_qualified and row["locked_at"] is not None:
        raise RecoveryError(f"@{row['handle']}: target row is locked")
    stage = _load_stage(
        row["stage_json"], database=database, handle=row["handle"]
    )
    embedded_handle = str(stage.get("handle") or row["handle"]).lstrip("@")
    if embedded_handle.casefold() != str(row["handle"]).casefold():
        raise RecoveryError(f"@{row['handle']}: stage_json handle mismatch")
    return RowSnapshot(
        handle=row["handle"],
        raw_stage_json=row["stage_json"],
        stage=stage,
        stage_sha256=_sha_text(row["stage_json"]),
        stage_error=row["stage_error"],
        stage_updated_at=row["stage_updated_at"],
        protected={column: row[column] for column in PROTECTED_COLUMNS},
    )


def _time_key(value: Any) -> tuple[int, float, str]:
    raw = str(value or "").strip()
    if not raw:
        return (0, float("-inf"), "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_LOCAL_CAPTURE_TZ)
        return (1, parsed.timestamp(), raw)
    except (ValueError, OverflowError):
        return (0, float("-inf"), raw)


def _attempt_timestamp(stage: Mapping[str, Any], fallback: str | None) -> str:
    translation_summary = stage.get("comment_translation_summary")
    translation_summary = (
        translation_summary if isinstance(translation_summary, Mapping) else {}
    )
    values: list[Any] = [
        stage.get("pricing_captured_at"),
        translation_summary.get("generated_at"),
        fallback,
    ]
    values.extend(
        row.get("captured_at")
        for row in (stage.get("sampled_posts") or [])
        if isinstance(row, Mapping)
    )
    present = [value for value in values if str(value or "").strip()]
    if not present:
        return "1970-01-01T00:00:00+00:00"
    return str(max(present, key=_time_key))


def _restore_current_non_deep(
    current: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    if not hasattr(deep_attempts, "_is_deep_field"):
        raise RecoveryError("deep_attempts field classifier is incompatible")
    result = copy.deepcopy(dict(candidate))

    def mutable_deep(key: str) -> bool:
        return key in _ATTEMPT_META_FIELDS or deep_attempts._is_deep_field(key)  # noqa: SLF001

    for key in list(result):
        if not mutable_deep(str(key)) and key not in current:
            result.pop(key, None)
    for key, value in current.items():
        if not mutable_deep(str(key)):
            result[key] = copy.deepcopy(value)
    return result


def _append_attempt(
    prior: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    attempted_at: str,
) -> dict[str, Any]:
    context = deep_attempts.prepare_stage3_attempt(prior)
    verdict = deep_attempts.finalize_stage3_verdict(
        context,
        ("advance", "qualified", copy.deepcopy(dict(evidence))),
        attempted_at=attempted_at,
        allow_same_window_merge=False,
    )
    if len(verdict) < 3 or not isinstance(verdict[2], Mapping):
        raise RecoveryError("deep_attempts returned an invalid finalized verdict")
    return _restore_current_non_deep(prior, verdict[2])


def _deep_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return deep_attempts._extract_attempt_evidence(candidate)  # noqa: SLF001
    except AttributeError as exc:
        raise RecoveryError("deep_attempts evidence API is incompatible") from exc


def _retry_state_snapshot(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize the prior state once without treating recovery as a retry."""
    try:
        state = deep_attempts._load_comment_retry_state(candidate)  # noqa: SLF001
        return deep_attempts._serialize_comment_retry_state(state)  # noqa: SLF001
    except AttributeError as exc:
        raise RecoveryError("deep_attempts retry-state API is incompatible") from exc


def _assert_canonical_metadata(candidate: Mapping[str, Any], *, handle: str) -> None:
    """Fail closed if ledger IDs, quality summaries, or retry state disagree."""
    ledger = candidate.get(deep_attempts.LEDGER_FIELD)
    if not isinstance(ledger, list) or not ledger:
        raise RecoveryError(f"@{handle}: deep attempt ledger missing")
    by_id: dict[str, tuple[int, Mapping[str, Any]]] = {}
    full_owners: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(ledger):
        if not isinstance(entry, Mapping):
            raise RecoveryError(f"@{handle}: invalid deep ledger row")
        attempt_id = str(entry.get("attempt_id") or "")
        if not attempt_id or attempt_id in by_id:
            raise RecoveryError(f"@{handle}: duplicate/missing deep attempt ID")
        by_id[attempt_id] = (index, entry)
        evidence = entry.get("evidence")
        reference = str(entry.get("evidence_ref_attempt_id") or "")
        if isinstance(evidence, Mapping):
            if reference or _sha_json(evidence) != entry.get("evidence_sha256"):
                raise RecoveryError(f"@{handle}: invalid full ledger evidence")
            full_owners[attempt_id] = evidence
        else:
            owner = full_owners.get(reference)
            if (
                not reference
                or owner is None
                or entry.get("evidence_sha256") != _sha_json(owner)
            ):
                raise RecoveryError(
                    f"@{handle}: dangling/forward/cyclic evidence reference"
                )
        provenance = entry.get("merge_provenance")
        if isinstance(provenance, Mapping):
            sources = provenance.get("sources") or {}
            if isinstance(sources, Mapping):
                for source in sources.values():
                    if (
                        isinstance(source, Mapping)
                        and source.get("kind") == "attempt"
                        and str(source.get("attempt_id") or "") not in by_id
                    ):
                        raise RecoveryError(
                            f"@{handle}: merge provenance has dangling attempt ref"
                        )
    canonical_id = str(candidate.get(deep_attempts.CANONICAL_ATTEMPT_FIELD) or "")
    if canonical_id not in by_id:
        raise RecoveryError(f"@{handle}: deep canonical attempt ID is unresolved")
    canonical_owner = full_owners.get(canonical_id)
    if canonical_owner is None:
        raise RecoveryError(f"@{handle}: deep canonical attempt has no full evidence")
    pricing_fields = (
        "pricing_estimate",
        "pricing_reel_samples",
        "pricing_reels_tab_evidence",
        "pricing_captured_at",
    )
    stage_deep = _deep_evidence(candidate)
    owner_deep = copy.deepcopy(dict(canonical_owner))
    for key in pricing_fields:
        stage_deep.pop(key, None)
        owner_deep.pop(key, None)
    if stage_deep != owner_deep:
        raise RecoveryError(f"@{handle}: deep canonical evidence does not match stage")
    canonical_events = [
        (index, entry)
        for index, entry in enumerate(ledger)
        if isinstance(entry, Mapping)
        and (
            str(entry.get("attempt_id") or "") == canonical_id
            or str(entry.get("evidence_ref_attempt_id") or "") == canonical_id
        )
    ]
    stored_quality = candidate.get(deep_attempts.CANONICAL_QUALITY_FIELD)
    quality_matches = any(
        stored_quality
        == deep_attempts.summarize_quality(
            candidate,
            attempted_at=entry.get("attempted_at"),
            ordinal=index,
        )
        for index, entry in canonical_events
    )
    if not quality_matches:
        raise RecoveryError(f"@{handle}: deep canonical quality is inconsistent")

    if any(key in candidate for key in pricing_fields):
        pricing_id = str(
            candidate.get(deep_attempts.PRICING_CANONICAL_ATTEMPT_FIELD) or ""
        )
        pricing_owner = full_owners.get(pricing_id)
        if pricing_owner is None:
            raise RecoveryError(f"@{handle}: pricing canonical attempt ID is unresolved")
        stage_pricing = {
            key: candidate[key] for key in pricing_fields if key in candidate
        }
        owner_pricing = {
            key: pricing_owner[key] for key in pricing_fields if key in pricing_owner
        }
        if stage_pricing != owner_pricing:
            raise RecoveryError(
                f"@{handle}: pricing canonical evidence does not match stage"
            )
    try:
        pricing_tuple = deep_attempts._pricing_quality(candidate)  # noqa: SLF001
    except AttributeError as exc:
        raise RecoveryError("deep_attempts pricing API is incompatible") from exc
    estimate = candidate.get("pricing_estimate")
    estimate = estimate if isinstance(estimate, Mapping) else {}
    expected_pricing = {
        "selection_tuple": list(pricing_tuple),
        "status": estimate.get("status"),
        "sample_count": pricing_tuple[1],
        "captured_at": candidate.get("pricing_captured_at"),
    }
    if candidate.get(deep_attempts.PRICING_CANONICAL_QUALITY_FIELD) != expected_pricing:
        raise RecoveryError(f"@{handle}: pricing canonical quality is inconsistent")

    state = candidate.get(deep_attempts.COMMENT_RETRY_STATE_FIELD)
    if not isinstance(state, list):
        raise RecoveryError(f"@{handle}: compact comment retry state missing")
    identities: set[str] = set()
    for row in state:
        if not isinstance(row, Mapping):
            raise RecoveryError(f"@{handle}: invalid comment retry state row")
        identity = str(row.get("identity") or "")
        if (
            not identity
            or identity in identities
            or row.get("state")
            not in {"resolved", "retry_pending", "terminal_unavailable"}
        ):
            raise RecoveryError(f"@{handle}: inconsistent comment retry state")
        identities.add(identity)


def _canonical_metadata_valid(candidate: Mapping[str, Any], *, handle: str) -> bool:
    try:
        _assert_canonical_metadata(candidate, handle=handle)
    except RecoveryError:
        return False
    return True


def _plan_row(
    current: RowSnapshot,
    backup: RowSnapshot,
    *,
    config: Mapping[str, Any],
    backup_database_identity_sha256: str,
) -> PlannedRow:
    source_timestamp = _attempt_timestamp(backup.stage, backup.stage_updated_at)
    recovery_timestamp = max(
        (
            _attempt_timestamp(current.stage, current.stage_updated_at),
            source_timestamp,
        ),
        key=_time_key,
    )
    current_quality = deep_attempts.summarize_quality(current.stage)
    backup_quality = deep_attempts.summarize_quality(backup.stage)
    try:
        merge = deep_evidence_merge.merge_evidence(
            backup.stage,
            current.stage,
            old_quality=backup_quality,
            current_quality=current_quality,
            generated_at=recovery_timestamp,
            config=config,
        )
    except deep_evidence_merge.EvidenceMergeError as exc:
        raise RecoveryError(f"@{current.handle}: {exc}") from exc
    evidence_changed = _deep_evidence(current.stage) != _deep_evidence(
        merge.candidate
    )
    metadata_valid = _canonical_metadata_valid(
        current.stage, handle=current.handle
    )
    # Recovery is not a general ledger migration.  If the selected canonical
    # evidence/pricing is already identical, leave even legacy metadata byte-for-
    # byte untouched; the next real Stage 3 attempt may migrate it normally.
    write_required = evidence_changed
    action = "repair_evidence" if evidence_changed else "skip_no_change"
    if write_required:
        prior_retry_state = _retry_state_snapshot(current.stage)
        finalized = _append_attempt(
            current.stage, merge.candidate, attempted_at=recovery_timestamp
        )
        finalized = _restore_current_non_deep(current.stage, finalized)
        ledger = finalized.get(deep_attempts.LEDGER_FIELD) or []
        if not ledger or not isinstance(ledger[-1], dict):
            raise RecoveryError(f"@{current.handle}: synthetic ledger entry missing")
        # Source IDs/qualities belong to the internal event.  The delivery
        # exporter strips this ledger, so they never enter customer JSON.
        ledger[-1]["event_kind"] = "synthetic_evidence_recovery"
        event_provenance = copy.deepcopy(merge.provenance)
        event_provenance["source_stage_json_sha256"] = {
            "current": current.stage_sha256,
            "backup": backup.stage_sha256,
        }
        event_provenance[
            "backup_database_identity_sha256"
        ] = backup_database_identity_sha256
        ledger[-1]["merge_provenance"] = event_provenance
        # Import/repair is not a real Instagram comment attempt.  Normalize the
        # prior state if needed, but never increment/reset any streak from the
        # synthetic evidence.
        finalized[deep_attempts.COMMENT_RETRY_STATE_FIELD] = prior_retry_state
        finalized.pop("stage3_comment_retry_history", None)
        _assert_canonical_metadata(finalized, handle=current.handle)
        finalized["handle"] = current.stage.get("handle") or current.handle
        after_raw = _canonical_json(finalized)
    else:
        finalized = copy.deepcopy(current.stage)
        after_raw = current.raw_stage_json
    current_fp = deep_attempts.window_fingerprint(current.stage)
    backup_fp = deep_attempts.window_fingerprint(backup.stage)
    if merge.same_window and (not current_fp or current_fp != backup_fp):
        raise RecoveryError(
            f"@{current.handle}: window matcher/fingerprint disagreement"
        )
    audit = {
        "handle": current.handle,
        "mode": merge.mode,
        "action": action,
        "metadata_valid_before": metadata_valid,
        "before_stage_json_sha256": current.stage_sha256,
        "backup_stage_json_sha256": backup.stage_sha256,
        "after_stage_json_sha256": _sha_text(after_raw),
        "window_match": merge.same_window,
        "current_window_fingerprint": current_fp,
        "backup_window_fingerprint": backup_fp,
        "target_window_size": (
            len(deep_evidence_merge.ordered_target_window(current.stage)[1])
            if merge.same_window
            else None
        ),
        "before_quality": current_quality,
        "backup_quality": backup_quality,
        "after_quality": deep_attempts.summarize_quality(finalized),
        "pricing_source": merge.pricing_source,
        "merge_provenance": merge.provenance,
        "detail": merge.detail,
    }
    return PlannedRow(
        current=current,
        backup=backup,
        after_stage=finalized,
        after_raw_stage_json=after_raw,
        after_stage_sha256=_sha_text(after_raw),
        hot_values={column: finalized.get(column) for column in HOT_EVIDENCE_COLUMNS},
        audit=audit,
        write_required=write_required,
    )


def build_plan(
    *,
    db: str | os.PathLike[str],
    backup_db: str | os.PathLike[str],
    batch_id: str,
    handles: Iterable[str],
) -> RecoveryPlan:
    db_path = Path(db).expanduser().resolve()
    backup_path = Path(backup_db).expanduser().resolve()
    if db_path == backup_path:
        raise RecoveryError("--db and --backup-db must be different files")
    batch = str(batch_id or "").strip()
    if not batch or len(batch) > 160:
        raise RecoveryError("invalid --batch-id")
    normalized = _normalize_handles(handles)
    config = load_config()
    config_digest = config_sha256()
    planned: list[PlannedRow] = []
    current_conn = _ro_connection(db_path)
    backup_conn = _ro_connection(backup_path)
    try:
        _validate_database(current_conn, db_path)
        _validate_database(backup_conn, backup_path)
        for handle in normalized:
            current = _fetch_snapshot(
                current_conn,
                database=db_path,
                batch_id=batch,
                requested_handle=handle,
                require_qualified=True,
            )
            backup = _fetch_snapshot(
                backup_conn,
                database=backup_path,
                batch_id=batch,
                requested_handle=handle,
                require_qualified=False,
            )
            if current.handle.casefold() != backup.handle.casefold():
                raise RecoveryError(f"@{handle}: source/target handle mismatch")
            planned.append(
                _plan_row(
                    current,
                    backup,
                    config=config,
                    backup_database_identity_sha256=_sha_text(str(backup_path)),
                )
            )
    finally:
        current_conn.close()
        backup_conn.close()
    if config_sha256() != config_digest:
        raise RecoveryError("config SHA-256 drifted during planning")
    plan_payload = {
        "schema": AUDIT_SCHEMA,
        "batch_id": batch,
        "config_sha256": config_digest,
        "target_database_identity_sha256": _sha_text(str(db_path)),
        "backup_database_identity_sha256": _sha_text(str(backup_path)),
        "rows": [
            {
                "handle": row.current.handle.casefold(),
                "before": row.current.stage_sha256,
                "current_cas_sha256": _current_cas_sha256(row.current),
                "backup": row.backup.stage_sha256,
                "after": row.after_stage_sha256,
                "mode": row.audit["mode"],
                "action": row.audit["action"],
                "write_required": row.write_required,
            }
            for row in planned
        ],
    }
    return RecoveryPlan(
        db_path=db_path,
        backup_db_path=backup_path,
        batch_id=batch,
        config_sha256=config_digest,
        rows=tuple(planned),
        plan_sha256=_sha_json(plan_payload),
    )


def _validate_apply_row(row: sqlite3.Row, planned: PlannedRow, batch_id: str) -> None:
    handle = planned.current.handle
    if row["status"] != "qualified":
        raise RecoveryError(f"@{handle}: status CAS failed")
    if row["discovery_batch"] != batch_id:
        raise RecoveryError(f"@{handle}: batch CAS failed")
    if row["locked_at"] is not None:
        raise RecoveryError(f"@{handle}: lock appeared before apply")
    if row["stage_error"] != planned.current.stage_error:
        raise RecoveryError(f"@{handle}: stage_error CAS failed")
    if row["stage_json"] != planned.current.raw_stage_json:
        raise RecoveryError(f"@{handle}: stage_json content CAS failed")
    if _sha_text(row["stage_json"]) != planned.current.stage_sha256:
        raise RecoveryError(f"@{handle}: stage_json SHA-256 CAS failed")
    for column, expected in planned.current.protected.items():
        if row[column] != expected:
            raise RecoveryError(f"@{handle}: protected column changed: {column}")


def apply_plan(plan: RecoveryPlan, *, expected_plan_sha256: str) -> None:
    """Apply a detached plan as one all-or-nothing transaction."""
    expected = str(expected_plan_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RecoveryError("--apply requires a 64-character expected plan SHA-256")
    if expected != plan.plan_sha256:
        raise RecoveryError(
            "expected plan SHA-256 does not match the freshly rebuilt plan"
        )
    conn = sqlite3.connect(str(plan.db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    select_columns = list(dict.fromkeys(("handle", "stage_json", *PROTECTED_COLUMNS)))
    try:
        conn.execute("BEGIN IMMEDIATE")
        if config_sha256() != plan.config_sha256:
            raise RecoveryError("config SHA-256 drifted after planning")
        locks = conn.execute(
            "SELECT handle FROM creator_profiles "
            "WHERE discovery_batch=? AND locked_at IS NOT NULL "
            "ORDER BY handle COLLATE NOCASE LIMIT 10",
            (plan.batch_id,),
        ).fetchall()
        if locks:
            raise RecoveryError(
                "batch has active locks; refusing recovery: "
                + ", ".join(f"@{row['handle']}" for row in locks)
            )

        # Validate the complete cohort before the first UPDATE.
        for planned in plan.rows:
            matches = conn.execute(
                f"SELECT {','.join(select_columns)} FROM creator_profiles "
                "WHERE handle=? COLLATE NOCASE",
                (planned.current.handle,),
            ).fetchall()
            if len(matches) != 1:
                raise RecoveryError(
                    f"@{planned.current.handle}: exact-row CAS failed"
                )
            _validate_apply_row(matches[0], planned, plan.batch_id)

        assignments = ["stage_json=?"] + [
            f"{column}=?" for column in HOT_EVIDENCE_COLUMNS
        ]
        for planned in plan.rows:
            if not planned.write_required:
                continue
            values = [planned.after_raw_stage_json]
            values.extend(planned.hot_values[column] for column in HOT_EVIDENCE_COLUMNS)
            values.extend(
                (
                    planned.current.handle,
                    plan.batch_id,
                    planned.current.raw_stage_json,
                    planned.current.stage_error,
                    planned.current.stage_error,
                )
            )
            changed = conn.execute(
                f"UPDATE creator_profiles SET {','.join(assignments)} "
                "WHERE handle=? COLLATE NOCASE AND status='qualified' "
                "AND discovery_batch=? AND locked_at IS NULL AND stage_json=? "
                "AND (stage_error=? OR (stage_error IS NULL AND ? IS NULL))",
                values,
            ).rowcount
            if changed != 1:
                raise RecoveryError(
                    f"@{planned.current.handle}: update rowcount={changed}; rolling back"
                )

        for planned in plan.rows:
            row = conn.execute(
                f"SELECT {','.join(select_columns)} FROM creator_profiles "
                "WHERE handle=? COLLATE NOCASE",
                (planned.current.handle,),
            ).fetchone()
            if row is None or row["stage_json"] != planned.after_raw_stage_json:
                raise RecoveryError(
                    f"@{planned.current.handle}: post-write verification failed"
                )
            for column, expected in planned.current.protected.items():
                if row[column] != expected:
                    raise RecoveryError(
                        f"@{planned.current.handle}: protected column mutated: {column}"
                    )
        if config_sha256() != plan.config_sha256:
            raise RecoveryError("config SHA-256 drifted during apply")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _audit_payload(plan: RecoveryPlan, *, applied: bool) -> dict[str, Any]:
    return {
        "schema": AUDIT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "applied" if applied else "dry_run",
        "batch_id": plan.batch_id,
        "target_database": plan.db_path.name,
        "backup_database": plan.backup_db_path.name,
        "config_sha256": plan.config_sha256,
        "plan_sha256": plan.plan_sha256,
        "handle_count": len(plan.rows),
        "write_count": sum(row.write_required for row in plan.rows),
        "skip_count": sum(not row.write_required for row in plan.rows),
        "write_contract": {
            "transaction": "BEGIN IMMEDIATE",
            "stage_json_cas": "exact_content_and_sha256",
            "stage_error_cas": True,
            "required_status": "qualified",
            "batch_lock_policy": "no_active_locks",
            "updated_columns": ["stage_json", *HOT_EVIDENCE_COLUMNS],
            "protected_columns": list(PROTECTED_COLUMNS),
        },
        "rows": [copy.deepcopy(row.audit) for row in plan.rows],
    }


def _write_audit(path: Path, payload: Mapping[str, Any], plan: RecoveryPlan) -> None:
    destination = path.expanduser().resolve()
    if destination in {plan.db_path, plan.backup_db_path}:
        raise RecoveryError("audit output must not overwrite either database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def recover(
    *,
    db: str | os.PathLike[str],
    backup_db: str | os.PathLike[str],
    batch_id: str,
    handles: Iterable[str],
    apply: bool = False,
    expected_plan_sha256: str | None = None,
    audit_output: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    plan = build_plan(
        db=db, backup_db=backup_db, batch_id=batch_id, handles=handles
    )
    if apply:
        apply_plan(plan, expected_plan_sha256=expected_plan_sha256 or "")
    audit = _audit_payload(plan, applied=apply)
    if audit_output is not None:
        _write_audit(Path(audit_output), audit, plan)
    return audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recover complementary Stage 3 evidence from a backup. Dry-run is "
            "the default; pass --apply for the single atomic write transaction."
        )
    )
    parser.add_argument("--db", required=True, help="target creator_cache SQLite file")
    parser.add_argument("--backup-db", required=True, help="read-only evidence source")
    parser.add_argument("--batch-id", required=True)
    handles = parser.add_mutually_exclusive_group(required=True)
    handles.add_argument(
        "--handle", action="append", dest="handles", help="repeat for each exact handle"
    )
    handles.add_argument(
        "--handles-json",
        help="JSON array literal or path to a UTF-8 JSON array of handles",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write atomically; without this flag the command is read-only",
    )
    parser.add_argument(
        "--expected-plan-sha256",
        help="required with --apply; copy plan_sha256 from the reviewed dry-run",
    )
    parser.add_argument(
        "--audit-output",
        help="optional sanitized JSON audit artifact (also available in dry-run)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.apply and not args.expected_plan_sha256:
            raise RecoveryError(
                "--apply requires --expected-plan-sha256 from a reviewed dry-run"
            )
        if args.expected_plan_sha256 and not args.apply:
            raise RecoveryError("--expected-plan-sha256 is only valid with --apply")
        handles = (
            _normalize_handles(args.handles)
            if args.handles is not None
            else _load_handles_json(args.handles_json)
        )
        audit = recover(
            db=args.db,
            backup_db=args.backup_db,
            batch_id=args.batch_id,
            handles=handles,
            apply=args.apply,
            expected_plan_sha256=args.expected_plan_sha256,
            audit_output=args.audit_output,
        )
    except (RecoveryError, OSError, sqlite3.Error) as exc:
        print(f"recovery refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
