"""Read-only predicates for the production Stage 1/2/3 DAG barriers.

The module deliberately does not import :mod:`creator_cache`: opening that module's
``_conn`` helper may migrate a database.  Every evaluation instead opens an explicit
SQLite path with ``mode=ro`` and returns a deterministic, JSON-friendly result.

Canonical Stage 1 artifact fields used here are::

    {
      "batch_id": "SKIN6-20260810",
      "target_total": 120,
      "round_contract_sha256": "...",
      "handle_set_sha256": "...",
      "source_counts": {
        "golden_lookalike": 60,
        "generic_commerce": 36,
        "exploration": 24
      },
      "ingest": {
        "new_seeds": 120,
        "audited_new": 120,
        "deduped": 0,
        "rejected_skipped": 0
      }
    }

Mappings and explicit JSON paths are both accepted.  No production path is ever
inferred.  B3's non-database failure counts default to unknown and therefore block;
callers must inject fresh pricing, translation and strict-audit results.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping


SOURCE_KEYS = ("golden_lookalike", "generic_commerce", "exploration")
REQUIRED_B3_FAILURE_KEYS = ("pricing", "translation", "audit")
REQUIRED_B4_FAILURE_KEYS = (
    "pricing",
    "translation",
    "audit",
    "modash",
    "storefront",
    "sponsorship",
)
_REQUIRED_COLUMNS = {
    "handle",
    "status",
    "stage_error",
    "locked_at",
    "discovery_batch",
    "stage_json",
}
_MISSING = object()


class BarrierInputError(ValueError):
    """An input cannot be safely interpreted as a barrier snapshot."""


@dataclass(frozen=True)
class BarrierCheck:
    """One independently inspectable predicate in a barrier evaluation."""

    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
        }
        if self.detail:
            value["detail"] = self.detail
        return value


@dataclass(frozen=True)
class BarrierResult:
    """Structured result; ``passed`` is true only when every check passes."""

    barrier: str
    batch_id: str
    passed: bool
    checks: tuple[BarrierCheck, ...]
    failures: tuple[str, ...]
    observed: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "barrier": self.barrier,
            "batch_id": self.batch_id,
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
            "failures": list(self.failures),
            "observed": dict(self.observed),
        }

    # Convenient for JSON emitters while keeping a named domain object for callers.
    to_dict = as_dict


@dataclass(frozen=True)
class _LoadedJson:
    value: dict[str, Any]
    sha256: str


@dataclass(frozen=True)
class _Stage1Expectation:
    batch_id: str
    target_total: int
    handle_set_sha256: str | None
    round_contract_sha256: str | None
    source_counts: dict[str, int] | None
    ingest: dict[str, int | None]
    artifact_sha256: str


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BarrierInputError("JSON mapping contains a non-serializable value") from exc


def _load_json_object(
    source: Mapping[str, Any] | str | Path,
    *,
    label: str,
) -> _LoadedJson:
    if isinstance(source, Mapping):
        raw = _canonical_json_bytes(source)
        # JSON round-trip gives callers an isolated, plain-dict snapshot.
        value = json.loads(raw.decode("utf-8"))
    elif isinstance(source, (str, Path)):
        path = Path(source)
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BarrierInputError(f"{label} is not readable JSON: {path}") from exc
    else:
        raise BarrierInputError(f"{label} must be a mapping or an explicit JSON path")
    if not isinstance(value, dict):
        raise BarrierInputError(f"{label} root must be an object")
    return _LoadedJson(value=value, sha256=hashlib.sha256(raw).hexdigest())


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BarrierInputError(f"{label} must be a positive integer")
    return value


def _nonnegative_count(value: Any, label: str, *, allow_unknown: bool = False) -> int | None:
    if allow_unknown and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        suffix = " or null" if allow_unknown else ""
        raise BarrierInputError(f"{label} must be a non-negative integer{suffix}")
    return value


def _sha_or_none(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise BarrierInputError(f"{label} must be a 64-character SHA-256")
    normalized = value.lower()
    if any(char not in "0123456789abcdef" for char in normalized):
        raise BarrierInputError(f"{label} must be hexadecimal SHA-256")
    return normalized


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _stage1_expectation(loaded: _LoadedJson) -> _Stage1Expectation:
    artifact = loaded.value
    batch_id = artifact.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise BarrierInputError("stage1_artifact.batch_id must be a non-empty string")
    target = _positive_int(
        _first(artifact, "target_total", "expected_total", "target"),
        "stage1_artifact.target_total",
    )

    source_raw = _first(artifact, "source_counts", "quota_counts")
    source_counts: dict[str, int] | None = None
    if source_raw is not None:
        if not isinstance(source_raw, Mapping) or set(source_raw) != set(SOURCE_KEYS):
            raise BarrierInputError(
                "stage1_artifact.source_counts must contain exactly "
                + ", ".join(SOURCE_KEYS)
            )
        source_counts = {
            key: _nonnegative_count(
                source_raw[key], f"stage1_artifact.source_counts.{key}"
            )
            for key in SOURCE_KEYS
        }

    ingest_raw = artifact.get("ingest")
    if ingest_raw is None:
        ingest_raw = artifact.get("ingest_result")
    if ingest_raw is not None and not isinstance(ingest_raw, Mapping):
        raise BarrierInputError("stage1_artifact.ingest must be an object")
    ingest_map = ingest_raw or {}
    ingest: dict[str, int | None] = {}
    for key in ("new_seeds", "audited_new", "deduped", "rejected_skipped"):
        raw = ingest_map.get(key, artifact.get(key))
        ingest[key] = (
            _nonnegative_count(raw, f"stage1_artifact.ingest.{key}")
            if raw is not None
            else None
        )

    contract_block = artifact.get("round_contract")
    nested_contract_sha = (
        contract_block.get("sha256") if isinstance(contract_block, Mapping) else None
    )
    return _Stage1Expectation(
        batch_id=batch_id.strip(),
        target_total=target,
        handle_set_sha256=_sha_or_none(
            artifact.get("handle_set_sha256"),
            "stage1_artifact.handle_set_sha256",
        ),
        round_contract_sha256=_sha_or_none(
            artifact.get("round_contract_sha256", nested_contract_sha),
            "stage1_artifact.round_contract_sha256",
        ),
        source_counts=source_counts,
        ingest=ingest,
        artifact_sha256=loaded.sha256,
    )


def normalize_handle(handle: Any) -> str:
    """Return the same case-insensitive handle identity used by barrier fingerprints."""
    if not isinstance(handle, str):
        return ""
    return handle.strip().lstrip("@").casefold()


def handle_set_sha256(handles: list[str] | tuple[str, ...] | set[str]) -> str:
    """Hash a normalized, sorted unique handle set using the repository convention."""
    normalized = sorted(
        {
            normalized_handle
            for handle in handles
            if (normalized_handle := normalize_handle(handle))
        }
    )
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def delivery_state_sha256(rows: list[Mapping[str, Any]]) -> str:
    """Bind an external delivery audit to the exact database row contents.

    B4 consumes expensive validators that live outside this module.  A plain set
    of failure counters is not sufficient because the underlying ``stage_json``
    could change between validation and the barrier call.  This digest covers
    every field that controls the immutable delivery state and is recomputed by
    :func:`evaluate_b4` from a fresh read-only transaction.
    """
    normalized: list[dict[str, Any]] = []
    for row in rows:
        normalized.append(
            {
                "handle": normalize_handle(row.get("handle")),
                "status": row.get("status"),
                "stage_error": row.get("stage_error"),
                "locked_at": row.get("locked_at"),
                "stage_json": row.get("stage_json"),
            }
        )
    normalized.sort(key=lambda row: (row["handle"], str(row.get("status") or "")))
    return hashlib.sha256(_canonical_json_bytes({"rows": normalized})).hexdigest()


def _source_bucket(stage: Mapping[str, Any]) -> str | None:
    for key in (
        "discovery_quota_bucket",
        "quota_source",
        "source_bucket",
        "discovery_source_bucket",
    ):
        value = stage.get(key)
        if value in SOURCE_KEYS:
            return str(value)

    tokens: list[str] = []
    via = stage.get("discovered_via")
    if isinstance(via, str):
        tokens.append(via.lower())
    sources = stage.get("discovery_sources")
    if isinstance(sources, list):
        tokens.extend(str(item).lower() for item in sources if isinstance(item, str))

    found: set[str] = set()
    for token in tokens:
        if "generic_commerce" in token:
            found.add("generic_commerce")
        if "exploration" in token:
            found.add("exploration")
        if "golden_lookalike" in token or "lookalike:golden:" in token:
            found.add("golden_lookalike")
    return next(iter(found)) if len(found) == 1 else None


def _read_batch_snapshot(db_path: str | Path, batch_id: str) -> dict[str, Any]:
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise BarrierInputError("batch_id must be a non-empty string")
    path = Path(db_path).expanduser().resolve()
    # URI mode=ro prevents database creation and all accidental writes.  query_only is
    # defense in depth if SQLite's URI behavior changes under a different runtime.
    uri = path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise BarrierInputError(f"creator cache cannot be opened read-only: {path}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creator_profiles'"
        ).fetchone()
        if table is None:
            raise BarrierInputError("creator cache is missing creator_profiles")
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(creator_profiles)")
        }
        missing = sorted(_REQUIRED_COLUMNS - columns)
        if missing:
            raise BarrierInputError(
                "creator_profiles is missing required columns: " + ", ".join(missing)
            )
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT handle,status,stage_error,locked_at,stage_json "
            "FROM creator_profiles WHERE discovery_batch=? ORDER BY lower(handle),handle",
            (batch_id,),
        ).fetchall()
        conn.rollback()
    except sqlite3.Error as exc:
        raise BarrierInputError(f"creator cache read failed: {path}") from exc
    finally:
        conn.close()

    handles = [normalize_handle(row["handle"]) for row in rows]
    nonempty_handles = [handle for handle in handles if handle]
    status_counts: dict[str, int] = {}
    source_counts = {key: 0 for key in SOURCE_KEYS}
    unknown_sources = 0
    invalid_stage_json = 0
    errors = locks = seed_errors = seed_locks = 0
    for row in rows:
        status = row["status"] if isinstance(row["status"], str) else "null"
        status_counts[status] = status_counts.get(status, 0) + 1
        has_error = row["stage_error"] is not None
        has_lock = row["locked_at"] is not None
        errors += int(has_error)
        locks += int(has_lock)
        if status == "seed":
            seed_errors += int(has_error)
            seed_locks += int(has_lock)

        try:
            stage = json.loads(row["stage_json"]) if row["stage_json"] else None
        except (TypeError, json.JSONDecodeError):
            stage = None
        if not isinstance(stage, dict):
            invalid_stage_json += 1
            unknown_sources += 1
            continue
        bucket = _source_bucket(stage)
        if bucket is None:
            unknown_sources += 1
        else:
            source_counts[bucket] += 1

    total = len(rows)
    unique_handles = len(set(nonempty_handles))
    known_stage2_statuses = sum(
        status_counts.get(status, 0)
        for status in ("qualified", "collected", "rejected")
    )
    return {
        "db_path": str(path),
        "total": total,
        "unique_handles": unique_handles,
        "empty_handles": total - len(nonempty_handles),
        "duplicate_handles_case_insensitive": len(nonempty_handles) - unique_handles,
        "handle_set_sha256": handle_set_sha256(nonempty_handles),
        "delivery_state_sha256": delivery_state_sha256(
            [dict(row) for row in rows]
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "seed_count": status_counts.get("seed", 0),
        "qualified_count": status_counts.get("qualified", 0),
        "collected_count": status_counts.get("collected", 0),
        "decided_count": status_counts.get("decided", 0),
        "rejected_count": status_counts.get("rejected", 0),
        "stage2_output_count": known_stage2_statuses,
        "stage2_other_status_count": total - known_stage2_statuses,
        "b3_other_status_count": total - status_counts.get("collected", 0),
        "b4_other_status_count": total - status_counts.get("decided", 0),
        "error_count": errors,
        "lock_count": locks,
        "seed_error_count": seed_errors,
        "seed_lock_count": seed_locks,
        "invalid_stage_json_count": invalid_stage_json,
        "source_counts": source_counts,
        "unknown_source_count": unknown_sources,
    }


def _check(
    name: str,
    actual: Any,
    expected: Any,
    *,
    detail: str | None = None,
) -> BarrierCheck:
    return BarrierCheck(
        name=name,
        passed=actual == expected,
        expected=expected,
        actual=actual,
        detail=detail,
    )


def _result(
    barrier: str,
    batch_id: str,
    checks: list[BarrierCheck],
    observed: dict[str, Any],
) -> BarrierResult:
    failures = tuple(check.name for check in checks if not check.passed)
    return BarrierResult(
        barrier=barrier,
        batch_id=batch_id,
        passed=not failures,
        checks=tuple(checks),
        failures=failures,
        observed=observed,
    )


def evaluate_b1(
    db_path: str | Path,
    *,
    round_contract: Mapping[str, Any] | str | Path,
    stage1_artifact: Mapping[str, Any] | str | Path,
    batch_id: str | None = None,
    round_contract_sha256: str | None = None,
) -> BarrierResult:
    """Evaluate the atomic discovery barrier without modifying the DB or artifacts.

    ``round_contract_sha256`` is useful when a caller passes an already-parsed
    contract but must retain the original file-byte fingerprint.  Otherwise a path
    is hashed byte-for-byte and a mapping is hashed as canonical JSON.
    """
    loaded_contract = _load_json_object(round_contract, label="round_contract")
    loaded_artifact = _load_json_object(stage1_artifact, label="stage1_artifact")
    expected = _stage1_expectation(loaded_artifact)
    selected_batch = batch_id or expected.batch_id
    contract_batch = loaded_contract.value.get("batch_id")
    if not isinstance(contract_batch, str) or not contract_batch.strip():
        raise BarrierInputError("round_contract.batch_id must be a non-empty string")

    quota = (loaded_contract.value.get("sources") or {}).get("quota_pct")
    if not isinstance(quota, Mapping) or set(quota) != set(SOURCE_KEYS):
        raise BarrierInputError(
            "round_contract.sources.quota_pct must contain exactly "
            + ", ".join(SOURCE_KEYS)
        )
    try:
        quota_values = [float(quota[key]) for key in SOURCE_KEYS]
    except (TypeError, ValueError) as exc:
        raise BarrierInputError("round contract source quotas must be numeric") from exc
    if any(not math.isfinite(value) or value < 0 for value in quota_values):
        raise BarrierInputError(
            "round contract source quotas must be finite and non-negative"
        )
    quota_total = sum(quota_values)

    contract_sha = (
        _sha_or_none(round_contract_sha256, "round_contract_sha256")
        if round_contract_sha256 is not None
        else loaded_contract.sha256
    )
    snapshot = _read_batch_snapshot(db_path, selected_batch)
    observed = {
        **snapshot,
        "stage1_artifact_sha256": expected.artifact_sha256,
        "round_contract_sha256": contract_sha,
        "contract_quota_pct": {key: quota[key] for key in SOURCE_KEYS},
        "stage1_expected_total": expected.target_total,
        "stage1_expected_source_counts": expected.source_counts,
        "stage1_ingest": expected.ingest,
    }

    checks = [
        _check("artifact_batch_matches_request", expected.batch_id, selected_batch),
        _check("contract_batch_matches_request", contract_batch.strip(), selected_batch),
        _check(
            "round_contract_fingerprint_matches_artifact",
            expected.round_contract_sha256,
            contract_sha,
            detail=(
                "missing artifact fingerprint blocks B1"
                if expected.round_contract_sha256 is None
                else None
            ),
        ),
        BarrierCheck(
            name="contract_source_quota_totals_100",
            passed=abs(quota_total - 100.0) <= 1e-6,
            expected=100.0,
            actual=quota_total,
        ),
        _check("cohort_total_matches_target", snapshot["total"], expected.target_total),
        _check(
            "cohort_unique_handles_match_target",
            snapshot["unique_handles"],
            expected.target_total,
        ),
        _check("cohort_empty_handles_zero", snapshot["empty_handles"], 0),
        _check(
            "cohort_case_insensitive_duplicates_zero",
            snapshot["duplicate_handles_case_insensitive"],
            0,
        ),
        _check(
            "cohort_handle_fingerprint_matches_artifact",
            snapshot["handle_set_sha256"],
            expected.handle_set_sha256,
            detail=(
                "missing artifact fingerprint blocks B1"
                if expected.handle_set_sha256 is None
                else None
            ),
        ),
        _check("all_candidates_are_seed", snapshot["seed_count"], expected.target_total),
        _check("batch_stage_errors_zero", snapshot["error_count"], 0),
        _check("batch_locks_zero", snapshot["lock_count"], 0),
        _check("stage_json_invalid_zero", snapshot["invalid_stage_json_count"], 0),
        _check("unknown_source_assignments_zero", snapshot["unknown_source_count"], 0),
        _check(
            "artifact_source_counts_total_target",
            sum(expected.source_counts.values()) if expected.source_counts is not None else None,
            expected.target_total,
            detail="missing source counts blocks B1" if expected.source_counts is None else None,
        ),
        _check(
            "database_source_counts_match_artifact",
            snapshot["source_counts"],
            expected.source_counts,
            detail="quota attribution is checked as a disjoint assignment, not multi-source reach",
        ),
        _check(
            "stage1_new_seeds_matches_target",
            expected.ingest["new_seeds"],
            expected.target_total,
        ),
        _check(
            "stage1_audited_new_matches_target",
            expected.ingest["audited_new"],
            expected.target_total,
        ),
        _check("stage1_deduped_zero", expected.ingest["deduped"], 0),
        _check("stage1_rejected_skipped_zero", expected.ingest["rejected_skipped"], 0),
    ]
    return _result("B1", selected_batch, checks, observed)


def evaluate_b2(
    db_path: str | Path,
    *,
    stage1_artifact: Mapping[str, Any] | str | Path,
    batch_id: str | None = None,
    producer_exited: bool | None = None,
) -> BarrierResult:
    """Evaluate Stage 2 producer closure.

    Full-batch ``error_count`` and ``lock_count`` are intentionally observations,
    not predicates: concurrent Stage 3 consumers may lock ``qualified`` rows or
    persist ``deep_incomplete`` errors.  Only the closed ``seed`` workset must have
    no rows, errors or locks.
    """
    loaded_artifact = _load_json_object(stage1_artifact, label="stage1_artifact")
    expected = _stage1_expectation(loaded_artifact)
    selected_batch = batch_id or expected.batch_id
    snapshot = _read_batch_snapshot(db_path, selected_batch)
    observed = {
        **snapshot,
        "stage1_artifact_sha256": expected.artifact_sha256,
        "stage1_expected_total": expected.target_total,
        "producer_exited": producer_exited,
    }
    checks = [
        _check("artifact_batch_matches_request", expected.batch_id, selected_batch),
        _check(
            "stage2_producer_exited",
            producer_exited,
            True,
            detail="unknown producer process state blocks B2" if producer_exited is None else None,
        ),
        _check("cohort_total_matches_target", snapshot["total"], expected.target_total),
        _check(
            "cohort_unique_handles_match_target",
            snapshot["unique_handles"],
            expected.target_total,
        ),
        _check("cohort_empty_handles_zero", snapshot["empty_handles"], 0),
        _check(
            "cohort_case_insensitive_duplicates_zero",
            snapshot["duplicate_handles_case_insensitive"],
            0,
        ),
        _check(
            "cohort_handle_fingerprint_matches_artifact",
            snapshot["handle_set_sha256"],
            expected.handle_set_sha256,
            detail=(
                "missing Stage 1 fingerprint blocks B2"
                if expected.handle_set_sha256 is None
                else None
            ),
        ),
        _check("seed_queue_empty", snapshot["seed_count"], 0),
        _check(
            "stage2_outputs_match_target",
            snapshot["stage2_output_count"],
            expected.target_total,
        ),
        _check("stage2_other_statuses_zero", snapshot["stage2_other_status_count"], 0),
        _check("seed_stage_errors_zero", snapshot["seed_error_count"], 0),
        _check("seed_locks_zero", snapshot["seed_lock_count"], 0),
        _check("stage_json_invalid_zero", snapshot["invalid_stage_json_count"], 0),
    ]
    return _result("B2", selected_batch, checks, observed)


def _normalize_b3_failure_counts(
    failure_counts: Mapping[str, int | None] | None,
) -> dict[str, int | None]:
    if failure_counts is None:
        return {key: None for key in REQUIRED_B3_FAILURE_KEYS}
    if not isinstance(failure_counts, Mapping):
        raise BarrierInputError("failure_counts must be a mapping")
    normalized: dict[str, int | None] = {}
    for key, value in failure_counts.items():
        if not isinstance(key, str) or not key.strip():
            raise BarrierInputError("failure_counts keys must be non-empty strings")
        normalized[key.strip()] = _nonnegative_count(
            value,
            f"failure_counts.{key}",
            allow_unknown=True,
        )
    for key in REQUIRED_B3_FAILURE_KEYS:
        normalized.setdefault(key, None)
    return dict(sorted(normalized.items()))


def _normalize_b4_failure_counts(
    failure_counts: Mapping[str, int | None] | None,
) -> dict[str, int | None]:
    """Normalize the post-enrichment delivery counters.

    B4 is deliberately stricter than B3: Modash report coverage, Storefront
    resolution and sponsorship derivation are first-class delivery evidence.
    Missing counters are unknown and therefore fail closed.
    """
    if failure_counts is None:
        return {key: None for key in REQUIRED_B4_FAILURE_KEYS}
    if not isinstance(failure_counts, Mapping):
        raise BarrierInputError("failure_counts must be a mapping")
    normalized: dict[str, int | None] = {}
    for key, value in failure_counts.items():
        if not isinstance(key, str) or not key.strip():
            raise BarrierInputError("failure_counts keys must be non-empty strings")
        normalized[key.strip()] = _nonnegative_count(
            value,
            f"failure_counts.{key}",
            allow_unknown=True,
        )
    for key in REQUIRED_B4_FAILURE_KEYS:
        normalized.setdefault(key, None)
    return dict(sorted(normalized.items()))


def evaluate_b3(
    db_path: str | Path,
    *,
    stage1_artifact: Mapping[str, Any] | str | Path,
    batch_id: str | None = None,
    failure_counts: Mapping[str, int | None] | None = None,
) -> BarrierResult:
    """Evaluate the full evidence barrier before Modash credit use and Stage 4.

    ``failure_counts`` must at least supply fresh ``pricing``, ``translation`` and
    ``audit`` counts.  Extra named counters are supported and are also required to
    be zero.  Missing/``None`` values are represented as unknown and block B3.
    """
    loaded_artifact = _load_json_object(stage1_artifact, label="stage1_artifact")
    expected = _stage1_expectation(loaded_artifact)
    selected_batch = batch_id or expected.batch_id
    snapshot = _read_batch_snapshot(db_path, selected_batch)
    external = _normalize_b3_failure_counts(failure_counts)
    observed = {
        **snapshot,
        "stage1_artifact_sha256": expected.artifact_sha256,
        "stage1_expected_total": expected.target_total,
        "failure_counts": external,
    }
    checks = [
        _check("artifact_batch_matches_request", expected.batch_id, selected_batch),
        _check("cohort_total_matches_target", snapshot["total"], expected.target_total),
        _check(
            "cohort_unique_handles_match_target",
            snapshot["unique_handles"],
            expected.target_total,
        ),
        _check("cohort_empty_handles_zero", snapshot["empty_handles"], 0),
        _check(
            "cohort_case_insensitive_duplicates_zero",
            snapshot["duplicate_handles_case_insensitive"],
            0,
        ),
        _check(
            "cohort_handle_fingerprint_matches_artifact",
            snapshot["handle_set_sha256"],
            expected.handle_set_sha256,
            detail=(
                "missing Stage 1 fingerprint blocks B3"
                if expected.handle_set_sha256 is None
                else None
            ),
        ),
        _check("seed_queue_empty", snapshot["seed_count"], 0),
        _check("qualified_queue_empty", snapshot["qualified_count"], 0),
        _check("all_candidates_collected", snapshot["collected_count"], expected.target_total),
        _check("rejected_candidates_zero", snapshot["rejected_count"], 0),
        _check("non_collected_statuses_zero", snapshot["b3_other_status_count"], 0),
        _check("batch_stage_errors_zero", snapshot["error_count"], 0),
        _check("batch_locks_zero", snapshot["lock_count"], 0),
        _check("stage_json_invalid_zero", snapshot["invalid_stage_json_count"], 0),
    ]
    for name, count in external.items():
        checks.append(
            _check(
                f"external_{name}_failures_zero",
                count,
                0,
                detail="unknown external result blocks B3" if count is None else None,
            )
        )
    return _result("B3", selected_batch, checks, observed)


def evaluate_b4(
    db_path: str | Path,
    *,
    stage1_artifact: Mapping[str, Any] | str | Path,
    batch_id: str | None = None,
    failure_counts: Mapping[str, int | None] | None = None,
    validated_delivery_state_sha256: str | None = None,
) -> BarrierResult:
    """Evaluate the immutable post-enrichment delivery barrier.

    B3 proves that collection is complete *before* Profile credits are spent.
    B4 proves that the subsequently enriched and routed cohort is still the
    exact Stage 1 cohort, is fully ``decided``, has no errors or locks, and has
    fresh zero-failure counters for deep audit, pricing, translations, Modash,
    Storefront and sponsorship evidence.  This avoids rewinding ``decided`` rows
    merely to replay B3 after a delivery-only remediation.
    """
    loaded_artifact = _load_json_object(stage1_artifact, label="stage1_artifact")
    expected = _stage1_expectation(loaded_artifact)
    selected_batch = batch_id or expected.batch_id
    snapshot = _read_batch_snapshot(db_path, selected_batch)
    external = _normalize_b4_failure_counts(failure_counts)
    validated_state = _sha_or_none(
        validated_delivery_state_sha256,
        "validated_delivery_state_sha256",
    )
    observed = {
        **snapshot,
        "stage1_artifact_sha256": expected.artifact_sha256,
        "stage1_expected_total": expected.target_total,
        "failure_counts": external,
        "validated_delivery_state_sha256": validated_state,
    }
    checks = [
        _check("artifact_batch_matches_request", expected.batch_id, selected_batch),
        _check("cohort_total_matches_target", snapshot["total"], expected.target_total),
        _check(
            "cohort_unique_handles_match_target",
            snapshot["unique_handles"],
            expected.target_total,
        ),
        _check("cohort_empty_handles_zero", snapshot["empty_handles"], 0),
        _check(
            "cohort_case_insensitive_duplicates_zero",
            snapshot["duplicate_handles_case_insensitive"],
            0,
        ),
        _check(
            "cohort_handle_fingerprint_matches_artifact",
            snapshot["handle_set_sha256"],
            expected.handle_set_sha256,
            detail=(
                "missing Stage 1 fingerprint blocks B4"
                if expected.handle_set_sha256 is None
                else None
            ),
        ),
        _check("all_candidates_decided", snapshot["decided_count"], expected.target_total),
        _check("non_decided_statuses_zero", snapshot["b4_other_status_count"], 0),
        _check("batch_stage_errors_zero", snapshot["error_count"], 0),
        _check("batch_locks_zero", snapshot["lock_count"], 0),
        _check("stage_json_invalid_zero", snapshot["invalid_stage_json_count"], 0),
        _check(
            "validated_delivery_state_matches_current",
            snapshot["delivery_state_sha256"],
            validated_state,
            detail=(
                "missing validated delivery-state fingerprint blocks B4"
                if validated_state is None
                else None
            ),
        ),
    ]
    for name, count in external.items():
        checks.append(
            _check(
                f"external_{name}_failures_zero",
                count,
                0,
                detail="unknown external result blocks B4" if count is None else None,
            )
        )
    return _result("B4", selected_batch, checks, observed)


__all__ = [
    "BarrierCheck",
    "BarrierInputError",
    "BarrierResult",
    "SOURCE_KEYS",
    "evaluate_b1",
    "evaluate_b2",
    "evaluate_b3",
    "evaluate_b4",
    "delivery_state_sha256",
    "handle_set_sha256",
    "normalize_handle",
]
