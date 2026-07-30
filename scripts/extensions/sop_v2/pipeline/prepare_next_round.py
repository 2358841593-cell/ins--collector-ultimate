"""Prepare an auditable carry-over manifest for the next client review round.

The creator cache stores one immutable ``discovery_batch`` per handle.  Carrying
an unresolved creator into a later review round must therefore be represented
in a manifest, not by rewriting the creator's original batch.

Example:
    cd scripts
    ../.venv/bin/python -m extensions.sop_v2.pipeline.prepare_next_round \
      --source-decisions ../reports/deliveries/SKIN3-20260717/decisions_interim.json \
      --feedback ../data/source/SKIN3-20260717/client_decisions_SKIN3-20260717.json \
      --next-batch SKIN4-20260723 \
      --out ../data/batches/SKIN4-20260723/carryover_manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from pathlib import Path

from extensions.sop_v2 import creator_cache as cc


_APPROVE = {"合适", "approve", "approved", "yes", "y"}
_REJECT = {"不合适", "reject", "rejected", "no", "n"}
_PENDING = {"待定", "pending", "maybe", "later"}
_NONTERMINAL = {"seed", "qualified", "collected"}


class ManifestError(ValueError):
    """Input files cannot be reconciled safely."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _handle(value) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _verdict(value, reason: str = "") -> str:
    raw = str(value or "").strip()
    low = raw.lower()
    if raw in _APPROVE or low in _APPROVE:
        return "approved"
    if raw in _REJECT or low in _REJECT:
        return "rejected"
    if raw in _PENDING or low in _PENDING:
        return "pending"
    # The HTML intentionally exports a reason-only row with an empty verdict.
    if not raw and reason.strip():
        return "undecided"
    raise ManifestError(f"unsupported verdict: {raw!r}")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"JSON root must be an object: {path}")
    return data


def _load_source(path: Path) -> tuple[str, list[dict], dict[str, dict]]:
    data = _read_json(path)
    manifest = data.get("manifest")
    rows = data.get("candidates")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ManifestError("source decisions must contain manifest and candidates[]")
    batch_id = str(manifest.get("batch_id") or "").strip()
    if not batch_id:
        raise ManifestError("source manifest.batch_id is empty")
    ordered, by_handle = [], {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ManifestError(f"source candidate {index} is not an object")
        handle = _handle(row.get("handle"))
        if not handle:
            raise ManifestError(f"source candidate {index} has an empty handle")
        if handle in by_handle:
            raise ManifestError(f"duplicate source handle: {handle}")
        origin = row.get("_discovery_batch")
        if origin in (None, ""):
            origin = row.get("discovery_batch")
        if origin in (None, ""):
            origin = batch_id
        if not isinstance(origin, str) or not origin.strip():
            raise ManifestError(f"source candidate {index} has an invalid discovery batch")
        if origin != origin.strip():
            raise ManifestError(f"source candidate {index} discovery batch has whitespace")
        normalized = dict(row)
        # Internal carry-over metadata. Keep the source review batch and immutable
        # creator origin separate so mixed-review deliveries can roll forward.
        normalized["_carryover_origin_batch"] = origin
        ordered.append(normalized)
        by_handle[handle] = normalized
    return batch_id, ordered, by_handle


def _load_feedback(path: Path, source_batch: str, source_handles: set[str]) -> dict[str, dict]:
    data = _read_json(path)
    if str(data.get("batch") or "").strip() != source_batch:
        raise ManifestError(
            f"feedback batch {data.get('batch')!r} does not match {source_batch!r}"
        )
    rows = data.get("decisions")
    if not isinstance(rows, list) or not rows:
        raise ManifestError("feedback decisions[] is empty or missing")
    out = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ManifestError(f"feedback decision {index} is not an object")
        handle = _handle(row.get("handle"))
        if not handle:
            raise ManifestError(f"feedback decision {index} has an empty handle")
        if handle in out:
            raise ManifestError(f"duplicate feedback handle: {handle}")
        if handle not in source_handles:
            raise ManifestError(f"feedback handle is outside source delivery: {handle}")
        reason = row.get("reason", "")
        if not isinstance(reason, str):
            raise ManifestError(f"feedback reason must be a string: {handle}")
        out[handle] = {
            "verdict": _verdict(row.get("verdict"), reason),
            "reason_present": bool(reason.strip()),
        }
    return out


def _db_rows(db_path: Path, expected_origins: dict[str, str]) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        handles = sorted(expected_origins)
        # Stay below SQLite's common bind-variable limit for future larger reviews.
        for offset in range(0, len(handles), 500):
            chunk = handles[offset:offset + 500]
            placeholders = ",".join("?" * len(chunk))
            rows.extend(
                conn.execute(
                    "SELECT handle, discovery_batch, status, stage_error, stage_json "
                    f"FROM creator_profiles WHERE lower(handle) IN ({placeholders})",
                    chunk,
                ).fetchall()
            )
    finally:
        conn.close()
    by_handle = {}
    for row in rows:
        handle = _handle(row["handle"])
        if handle in by_handle:
            raise ManifestError(f"creator_cache has case-insensitive duplicate handle: {handle}")
        by_handle[handle] = dict(row)
    missing = sorted(set(expected_origins) - set(by_handle))
    if missing:
        raise ManifestError(
            f"{len(missing)} source handles are missing from creator_cache "
            f"(first: {missing[0]})"
        )
    for handle, expected_origin in expected_origins.items():
        actual_origin = by_handle[handle].get("discovery_batch")
        if actual_origin != expected_origin:
            raise ManifestError(
                f"creator_cache origin mismatch for {handle}: "
                f"source={expected_origin!r}, db={actual_origin!r}"
            )
    return by_handle


def _incomplete_reasons(db_row: dict) -> list[str]:
    """Mirror ``creator_cache.incomplete_items`` without consulting global cc.DB."""
    try:
        stage_json = json.loads(db_row.get("stage_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        stage_json = {}
    if not isinstance(stage_json, dict):
        stage_json = {}

    reasons = []
    if db_row.get("stage_error"):
        reasons.append(f"错误:{db_row['stage_error']}")
    if db_row.get("status") in {"collected", "decided"} and stage_json.get("comments_read"):
        posts = stage_json.get("sampled_posts") or []
        codes = stage_json.get("codes") or []
        if codes and not posts:
            reasons.append(f"帖子全失败(网格 {len(codes)} 帖，0 采到)")
        elif posts:
            posts_with_comments = sum(
                1 for post in posts
                if isinstance(post, dict) and (post.get("comment_count") or 0) >= 5
            )
            if posts_with_comments and not (stage_json.get("comments_analyzed") or 0):
                reasons.append(f"评论抽取失败({posts_with_comments} 帖有评论却抽 0)")
            has_likes = any(
                isinstance(post, dict) and post.get("like_count") is not None
                for post in posts
            )
            if stage_json.get("real_er") is None and has_likes:
                reasons.append("采到赞数却算不出 ER")
    return reasons


def _set_hash(handles) -> str:
    payload = "\n".join(sorted(handles)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_manifest(
    source_path: Path,
    feedback_path: Path,
    next_batch: str,
    db_path: Path,
    generated_at: str | None = None,
) -> dict:
    source_batch, source_rows, source_by_handle = _load_source(source_path)
    feedback = _load_feedback(feedback_path, source_batch, set(source_by_handle))
    origins = {
        handle: row["_carryover_origin_batch"]
        for handle, row in source_by_handle.items()
    }
    db_rows = _db_rows(db_path, origins)

    final_handles = {
        handle
        for handle, decision in feedback.items()
        if decision["verdict"] in {"approved", "rejected"}
    }
    unresolved_handles = set(source_by_handle) - final_handles
    feedback_counts = Counter(d["verdict"] for d in feedback.values())

    carryover = []
    retry_handles = []
    for source in source_rows:
        handle = _handle(source.get("handle"))
        if handle not in unresolved_handles:
            continue
        db_row = db_rows[handle]
        feedback_state = feedback.get(handle, {}).get("verdict", "unreviewed")
        reason = "client_pending" if feedback_state in {"pending", "undecided"} else "client_unreviewed"
        incomplete_reasons = _incomplete_reasons(db_row)
        needs_retry = db_row.get("status") in _NONTERMINAL or bool(incomplete_reasons)
        if needs_retry:
            retry_handles.append(handle)
        carryover.append(
            {
                "handle": handle,
                "origin_batch": source["_carryover_origin_batch"],
                "source_pool": source.get("final_pool"),
                "source_score": source.get("ai_vetting_score"),
                "source_status": db_row.get("status"),
                "carryover_reason": reason,
                "needs_pipeline_retry": needs_retry,
                "stage_error": db_row.get("stage_error"),
                "incomplete_reasons": incomplete_reasons,
            }
        )

    next_batch = str(next_batch or "").strip()
    if not next_batch or next_batch == source_batch:
        raise ManifestError("next batch must be non-empty and differ from source batch")
    return {
        "schema_version": 1,
        "next_batch_id": next_batch,
        "generated_at": generated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": {
            "batch_id": source_batch,
            "decisions_file": str(source_path.resolve()),
            "decisions_sha256": _sha256(source_path),
            "candidate_count": len(source_rows),
            "handle_set_sha256": _set_hash(source_by_handle),
        },
        "feedback": {
            "file": str(feedback_path.resolve()),
            "sha256": _sha256(feedback_path),
            "record_count": len(feedback),
            "approved": feedback_counts["approved"],
            "rejected": feedback_counts["rejected"],
            "pending": feedback_counts["pending"],
            "undecided": feedback_counts["undecided"],
        },
        "counts": {
            "source_candidates": len(source_rows),
            "client_final": len(final_handles),
            "carryover": len(carryover),
            "pipeline_retry": len(retry_handles),
        },
        "carryover_handle_set_sha256": _set_hash(unresolved_handles),
        "source_batches": sorted(set(origins.values()) | {next_batch}),
        "retry_handles": retry_handles,
        "carryover": carryover,
    }


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-decisions", required=True)
    parser.add_argument("--feedback", required=True)
    parser.add_argument("--next-batch", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--db", default=str(cc.DB))
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()

    try:
        manifest = build_manifest(
            Path(args.source_decisions),
            Path(args.feedback),
            args.next_batch,
            Path(args.db),
            args.generated_at,
        )
    except ManifestError as exc:
        print(f"✗ {exc}")
        return 1
    _atomic_write(Path(args.out), manifest)
    counts = manifest["counts"]
    print(
        f"下一轮 {manifest['next_batch_id']}："
        f"上一轮 {counts['source_candidates']} · "
        f"客户终判 {counts['client_final']} · "
        f"结转 {counts['carryover']} · "
        f"需补采 {counts['pipeline_retry']}"
    )
    print(f"manifest → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
