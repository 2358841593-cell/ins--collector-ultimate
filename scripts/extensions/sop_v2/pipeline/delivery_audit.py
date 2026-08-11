"""Fresh, read-only B4 audit for an already-decided delivery cohort.

The collection barrier (B3) runs before paid enrichment.  Formal remediation can
legitimately update a cohort whose rows are already ``decided``; rewinding those
rows to replay B3 would destroy that state transition.  This module therefore
recomputes every delivery validator from one read-only SQLite snapshot, binds the
external counters to that snapshot's content hash, and evaluates B4.

The command never changes the creator cache.  Its output is append-only: callers
must choose a new path for every formal revision.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from extensions.sop_v2 import comment_translation
from extensions.sop_v2 import creator_cache as cc
from extensions.sop_v2 import pricing
from extensions.sop_v2.config import config_sha256, load_config
from extensions.sop_v2.pipeline import barriers
from extensions.sop_v2.pipeline.stage4_decide import (
    _enrichment_completeness,
)


class DeliveryAuditError(RuntimeError):
    """The cohort could not be audited without guessing or mutating state."""


def _read_candidates_ro(
    db_path: str | Path,
    batch_id: str,
) -> tuple[list[dict[str, Any]], str]:
    path = Path(db_path).expanduser().resolve()
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise DeliveryAuditError(f"候选库无法只读打开：{path}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT handle,status,stage_error,locked_at,stage_json "
            "FROM creator_profiles WHERE discovery_batch=? "
            "ORDER BY lower(handle),handle",
            (batch_id,),
        ).fetchall()
        conn.rollback()
    except sqlite3.Error as exc:
        raise DeliveryAuditError("读取正式候选 cohort 失败") from exc
    finally:
        conn.close()

    candidates: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    for row in rows:
        raw = row["stage_json"]
        try:
            candidate = json.loads(raw) if raw else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise DeliveryAuditError(
                f"@{row['handle']} stage_json 不是有效 JSON"
            ) from exc
        if not isinstance(candidate, dict):
            raise DeliveryAuditError(f"@{row['handle']} stage_json 不是对象")
        stored_handle = candidate.get("handle")
        if stored_handle is not None and barriers.normalize_handle(
            stored_handle
        ) != barriers.normalize_handle(row["handle"]):
            raise DeliveryAuditError(
                f"@{row['handle']} stage_json.handle 身份不一致"
            )
        candidate.setdefault("handle", row["handle"])
        candidate["_status"] = row["status"]
        candidate["_stage_error"] = row["stage_error"]
        candidates.append(candidate)
        state_rows.append(dict(row))
    return candidates, barriers.delivery_state_sha256(state_rows)


def _candidate_failure_report(
    candidates: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    target_posts: int,
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    details: dict[str, list[dict[str, Any]]] = {
        key: [] for key in barriers.REQUIRED_B4_FAILURE_KEYS
    }

    for cand in candidates:
        handle = str(cand.get("handle") or "<missing>")
        audit_reasons = cc.strict_deep_reasons(
            cand,
            status=cand.get("_status"),
            target_posts=target_posts,
            require_full_deep=True,
        )
        if cand.get("_stage_error") is not None:
            audit_reasons.append(f"stage_error={cand['_stage_error']}")
        if audit_reasons:
            details["audit"].append(
                {"handle": handle, "reasons": sorted(set(audit_reasons))}
            )

        pricing_reasons = pricing.completion_reasons(cand, cfg)
        if pricing_reasons:
            details["pricing"].append(
                {"handle": handle, "reasons": pricing_reasons}
            )

    translation_summary = comment_translation.validate_translation_cohort(candidates)
    for row in translation_summary.get("failures") or []:
        details["translation"].append(
            {
                "handle": str(row.get("handle") or "<missing>"),
                "reasons": list(row.get("failures") or ["translation_invalid"]),
            }
        )

    enrichment_stats, enrichment_failures = _enrichment_completeness(candidates)
    by_handle = {str(c.get("handle") or "<missing>"): c for c in candidates}
    for handle, reasons in enrichment_failures:
        cand = by_handle.get(handle, {})
        if cand.get("modash_report") is not True:
            details["modash"].append(
                {"handle": handle, "reasons": ["modash_report_missing"]}
            )
        from extensions.sop_v2 import storefront

        effective_storefront = storefront.effective_status(cand)
        if effective_storefront not in {"confirmed_yes", "confirmed_no"}:
            details["storefront"].append(
                {
                    "handle": handle,
                    "reasons": [f"effective_status={effective_storefront}"],
                }
            )
        if any(reason.startswith("赞助") for reason in reasons):
            details["sponsorship"].append(
                {"handle": handle, "reasons": ["sponsorship_window_inconsistent"]}
            )

    counts = {key: len(value) for key, value in details.items()}
    return counts, details, {
        "translation": {
            key: translation_summary.get(key)
            for key in (
                "candidate_count",
                "valid_candidate_count",
                "invalid_candidate_count",
                "requested_count",
                "translated_count",
                "failed_count",
                "source_unavailable_count",
            )
        },
        "enrichment": enrichment_stats,
    }


def evaluate_delivery(
    *,
    db_path: str | Path,
    batch_id: str,
    stage1_artifact: str | Path,
    config_path: str | None = None,
    target_posts: int = 10,
) -> dict[str, Any]:
    if target_posts <= 0:
        raise DeliveryAuditError("target_posts 必须为正整数")
    candidates, state_sha = _read_candidates_ro(db_path, batch_id)
    cfg = load_config(config_path)
    counts, details, summaries = _candidate_failure_report(
        candidates,
        cfg=cfg,
        target_posts=target_posts,
    )
    result = barriers.evaluate_b4(
        db_path,
        stage1_artifact=stage1_artifact,
        batch_id=batch_id,
        failure_counts=counts,
        validated_delivery_state_sha256=state_sha,
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch_id": batch_id,
        "config_sha256": config_sha256(config_path),
        "validated_delivery_state_sha256": state_sha,
        "failure_counts": counts,
        "failure_details": details,
        "summaries": summaries,
        "barrier": result.as_dict(),
    }


def _write_new_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError as exc:
            raise DeliveryAuditError(f"审计工件已存在，拒绝覆盖：{path}") from exc
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="正式交付 B4 只读完整性审计")
    parser.add_argument("--db", required=True, help="显式 creator_cache.db 路径")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--stage1-artifact", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--target-posts", type=int, default=10)
    parser.add_argument("--out", required=True, help="新的、不可覆盖的 B4 JSON 路径")
    args = parser.parse_args()
    try:
        artifact = evaluate_delivery(
            db_path=args.db,
            batch_id=args.batch_id,
            stage1_artifact=args.stage1_artifact,
            config_path=args.config,
            target_posts=args.target_posts,
        )
        _write_new_json(Path(args.out), artifact)
    except (DeliveryAuditError, barriers.BarrierInputError, OSError) as exc:
        print(f"✗ B4 审计失败：{exc}")
        return 1
    barrier = artifact["barrier"]
    print(
        f"B4 {'PASS' if barrier['passed'] else 'FAIL'} · "
        f"batch={args.batch_id} · failures={artifact['failure_counts']}"
    )
    return 0 if barrier["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
