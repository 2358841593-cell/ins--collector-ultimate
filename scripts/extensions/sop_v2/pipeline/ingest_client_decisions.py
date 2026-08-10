"""安全回收客户在交付 HTML 中导出的决策 JSON。

写入前必须同时给出原交付决策基线（``--source-decisions``）。程序会校验批次、
白名单、重复账号、verdict、数据库账号和 discovery_batch；全部通过后，才在一个
SQLite 事务中写业务状态与文件 SHA-256 ledger。

兼容旧的只读预览：

    cd scripts
    ../.venv/bin/python -m extensions.sop_v2.pipeline.ingest_client_decisions \
      --file client_decisions_SKIN3-20260717.json --dry-run

安全写入：

    ../.venv/bin/python -m extensions.sop_v2.pipeline.ingest_client_decisions \
      --file client_decisions_SKIN3-20260717.json \
      --source-decisions ../reports/deliveries/SKIN3-20260717/decisions_interim.json

若旧版脚本已经写入业务状态但没有 ledger，先核对后显式加
``--adopt-existing``；该模式不刷新已存在的 approved_at。

若 ledger 已存在但创建于逐条事件表上线之前，显式加 ``--backfill-events``；
该模式只追加历史 events，不改账号投影和原 ledger。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.feedback_taxonomy import (  # noqa: E402
    TAXONOMY_VERSION,
    FeedbackTaxonomyError,
    normalize_evidence_status,
    normalize_feedback_scope,
    normalize_reason_tags,
    normalize_rejection_scope,
)

_APPROVE = {"合适", "approve", "approved", "yes", "y"}
_REJECT = {"不合适", "reject", "rejected", "no", "n"}
_PENDING = {"待定", "pending", "maybe", "later"}


class FeedbackValidationError(ValueError):
    """反馈文件或交付基线不满足安全导入合同。"""


def _read_json(path: str | Path, label: str) -> tuple[dict, str]:
    p = Path(path)
    raw = p.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedbackValidationError(f"{label}不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise FeedbackValidationError(f"{label}顶层必须是对象")
    return data, digest


def _handle(value, *, label: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{label}的 handle 必须是字符串")
    handle = value.strip().lstrip("@").strip()
    if not handle:
        raise FeedbackValidationError(f"{label}的 handle 不能为空")
    return handle, handle.lower()


def _action(value, *, handle: str, reason: str = "") -> str:
    if not isinstance(value, str):
        raise FeedbackValidationError(f"@{handle} 的 verdict 必须是字符串")
    raw = value.strip()
    folded = raw.lower()
    if raw in _APPROVE or folded in _APPROVE:
        return "approved"
    if raw in _REJECT or folded in _REJECT:
        return "rejected"
    if raw in _PENDING or folded in _PENDING:
        return "pending"
    # HTML 会导出“只填原因、尚未点按钮”的行；它属于未决，不应被当成非法终判。
    if not raw and reason.strip():
        return "pending"
    raise FeedbackValidationError(f"@{handle} 的 verdict 未知：{raw!r}")


def _same_score(left, right) -> bool:
    """HTML exports scores as strings while decisions JSON stores numbers."""
    if left in (None, "") or right in (None, ""):
        return left in (None, "") and right in (None, "")
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def load_validated_decisions(
    feedback_path: str | Path,
    source_decisions_path: str | Path | None = None,
) -> dict:
    """读取并严格校验反馈；给出 source 时再做批次与交付白名单校验。"""
    data, file_sha256 = _read_json(feedback_path, "客户反馈")
    batch = data.get("batch")
    if not isinstance(batch, str) or not batch.strip():
        raise FeedbackValidationError("客户反馈 batch 必须是非空字符串")
    if batch != batch.strip():
        raise FeedbackValidationError("客户反馈 batch 不能带首尾空白")
    exported_at = data.get("exported_at")
    if not isinstance(exported_at, str) or not exported_at.strip():
        raise FeedbackValidationError("客户反馈 exported_at 必须是非空字符串")
    feedback_schema_version = data.get("feedback_schema_version")
    if feedback_schema_version is not None and (
        type(feedback_schema_version) is not int or feedback_schema_version != 2
    ):
        raise FeedbackValidationError(
            f"客户反馈 feedback_schema_version 未知：{feedback_schema_version!r}"
        )
    taxonomy_version = data.get("taxonomy_version")
    if feedback_schema_version == 2 and taxonomy_version is None:
        raise FeedbackValidationError("v2 客户反馈缺少 taxonomy_version")
    if taxonomy_version is not None and taxonomy_version != TAXONOMY_VERSION:
        raise FeedbackValidationError(
            f"客户反馈 taxonomy_version 未知：{taxonomy_version!r}"
        )
    raw_decisions = data.get("decisions")
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise FeedbackValidationError("客户反馈 decisions 必须是非空数组")

    decisions = []
    seen = set()
    for index, item in enumerate(raw_decisions, 1):
        if not isinstance(item, dict):
            raise FeedbackValidationError(f"第 {index} 条 decision 必须是对象")
        handle, key = _handle(item.get("handle"), label=f"第 {index} 条 decision")
        if key in seen:
            raise FeedbackValidationError(f"客户反馈存在重复账号：@{handle}")
        seen.add(key)
        reason = item.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise FeedbackValidationError(f"@{handle} 的 reason 必须是字符串")
        action = _action(item.get("verdict"), handle=handle, reason=reason)
        try:
            raw_reason_tags = item.get("reason_tags")
            if raw_reason_tags is not None and not isinstance(raw_reason_tags, list):
                raise FeedbackTaxonomyError("reason_tags 必须是字符串数组")
            reason_tags = normalize_reason_tags(raw_reason_tags)
            feedback_scope = normalize_feedback_scope(item.get("feedback_scope"))
            evidence_status = normalize_evidence_status(item.get("evidence_status"))
            rejection_scope = None
            if action == "rejected":
                rejection_scope = normalize_rejection_scope(
                    item.get("rejection_scope"),
                    # v2 UI 明示“仅当前活动”为默认；旧 JSON 没有这个维度，
                    # 必须保留上线前“拒绝即永久负向”的历史语义。
                    default="campaign" if feedback_schema_version == 2 else "global",
                )
            elif item.get("rejection_scope") not in (None, ""):
                raise FeedbackTaxonomyError(
                    "只有 rejected 事件可以设置 rejection_scope"
                )
        except FeedbackTaxonomyError as exc:
            raise FeedbackValidationError(f"@{handle} 的结构化反馈无效：{exc}") from exc
        if feedback_scope == "confirmed_policy":
            raise FeedbackValidationError(
                f"@{handle} 的客户反馈只能提出 policy_signal，不能直接确认全局策略"
            )
        # 空理由/空标签只是一条账号结果，不能被标成可用于策略学习的信号。
        if feedback_scope == "policy_signal" and not (
            reason.strip() or reason_tags
        ):
            feedback_scope = "account"

        structured_values = {}
        for field in ("target_field", "old_value", "claimed_value"):
            value = item.get(field)
            if value is not None and not isinstance(value, str):
                raise FeedbackValidationError(f"@{handle} 的 {field} 必须是字符串")
            structured_values[field] = value.strip() if isinstance(value, str) else None
        if feedback_scope == "fact_correction" and not structured_values["target_field"]:
            raise FeedbackValidationError(
                f"@{handle} 的 fact_correction 缺少 target_field"
            )
        decisions.append(
            {
                "handle": handle,
                "key": key,
                "action": action,
                "reason": reason.strip(),
                "reason_tags": reason_tags,
                "feedback_scope": feedback_scope,
                "rejection_scope": rejection_scope,
                "evidence_status": evidence_status,
                "taxonomy_version": TAXONOMY_VERSION,
                **structured_values,
            }
        )

    source_sha256 = None
    if source_decisions_path is not None:
        source, source_sha256 = _read_json(source_decisions_path, "交付决策基线")
        manifest = source.get("manifest")
        if not isinstance(manifest, dict):
            raise FeedbackValidationError("交付决策基线缺少 manifest")
        source_batch = manifest.get("batch_id")
        if source_batch != batch:
            raise FeedbackValidationError(
                f"批次不一致：反馈={batch!r}，基线={source_batch!r}"
            )
        candidates = source.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise FeedbackValidationError("交付决策基线 candidates 必须是非空数组")

        manifest_batches = manifest.get("batches")
        if manifest_batches is None:
            manifest_batches = [batch]
        if (
            not isinstance(manifest_batches, list)
            or not manifest_batches
            or any(not isinstance(value, str) or not value.strip()
                   for value in manifest_batches)
        ):
            raise FeedbackValidationError("交付决策基线 manifest.batches 必须是非空字符串数组")
        allowed_batches = {value.strip() for value in manifest_batches}
        if batch not in allowed_batches:
            raise FeedbackValidationError("交付决策基线 manifest.batches 不包含评审批次")

        whitelist = {}
        for index, candidate in enumerate(candidates, 1):
            if not isinstance(candidate, dict):
                raise FeedbackValidationError(f"基线第 {index} 条 candidate 必须是对象")
            candidate_handle, key = _handle(
                candidate.get("handle"), label=f"基线第 {index} 条 candidate"
            )
            if key in whitelist:
                raise FeedbackValidationError(f"交付基线存在重复账号：@{candidate_handle}")
            candidate_batch = candidate.get("_discovery_batch")
            if candidate_batch is None:
                candidate_batch = candidate.get("discovery_batch")
            if candidate_batch in (None, ""):
                candidate_batch = batch
            if not isinstance(candidate_batch, str) or candidate_batch not in allowed_batches:
                raise FeedbackValidationError(
                    f"基线账号 @{candidate_handle} 的来源批次不在 manifest.batches"
                )
            try:
                source_context = cc.freeze_source_context(candidate)
            except cc.ClientDecisionImportError as exc:
                raise FeedbackValidationError(
                    f"基线账号 @{candidate_handle} 的冻结来源上下文无效：{exc}"
                ) from exc
            whitelist[key] = {
                "handle": candidate_handle,
                "origin_batch": candidate_batch,
                "pool": candidate.get("final_pool"),
                "score": candidate.get("ai_vetting_score"),
                "source_context": source_context,
            }

        outside = [item["handle"] for item in decisions if item["key"] not in whitelist]
        if outside:
            shown = "、".join(f"@{handle}" for handle in outside[:5])
            raise FeedbackValidationError(f"反馈包含交付白名单外账号：{shown}")

        # 客户导出会冗余 pool/score，存在时也必须与交付基线一致；它们不参与业务写入，
        # 但可及时发现拿错 HTML、手工拼接或跨版本 JSON。
        for item, raw in zip(decisions, raw_decisions):
            source = whitelist[item["key"]]
            feedback_pool = raw.get("pool")
            if (
                feedback_pool not in (None, "")
                and source["pool"] not in (None, "")
                and str(feedback_pool).strip() != str(source["pool"]).strip()
            ):
                raise FeedbackValidationError(
                    f"@{item['handle']} 的 pool 与交付基线不一致"
                )
            feedback_score = raw.get("score")
            if (
                feedback_score not in (None, "")
                and source["score"] not in (None, "")
                and not _same_score(feedback_score, source["score"])
            ):
                raise FeedbackValidationError(
                    f"@{item['handle']} 的 score 与交付基线不一致"
                )
            # 后续数据库查询沿用基线中的规范 handle，避免客户侧大小写差异；
            # origin_batch 与本轮 review batch 正交，支持不改写历史 discovery_batch 的结转。
            item["handle"] = source["handle"]
            item["origin_batch"] = source["origin_batch"]
            item["source_context"] = source["source_context"]

    # key 只用于本函数内部的大小写无关校验，不进入业务 API。
    for item in decisions:
        item.pop("key", None)
    return {
        "batch": batch,
        "exported_at": exported_at,
        "feedback_schema_version": feedback_schema_version,
        "taxonomy_version": taxonomy_version or TAXONOMY_VERSION,
        "decisions": decisions,
        "file_sha256": file_sha256,
        "source_sha256": source_sha256,
    }


def _print_preview(payload: dict, *, dry_run: bool):
    suffix = "（DRY-RUN 预览）" if dry_run else ""
    print(
        f"客户决策回流：批次 {payload['batch']} · {len(payload['decisions'])} 条 · "
        f"导出于 {payload['exported_at']}{suffix}"
    )
    tally = Counter(item["action"] for item in payload["decisions"])
    for item in payload["decisions"]:
        print(f"  @{item['handle']:<26} {item['action']:<10}")
    print(
        "\n汇总："
        + " · ".join(
            f"{name} {tally.get(name, 0)}"
            for name in ("approved", "rejected", "pending")
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="客户导出的 client_decisions_*.json")
    ap.add_argument(
        "--source-decisions",
        help="原交付 decisions_*.json；真实写入必填，用于批次与原交付候选白名单校验",
    )
    ap.add_argument("--dry-run", action="store_true", help="只预览/校验，不写业务数据和 ledger")
    ap.add_argument(
        "--adopt-existing",
        action="store_true",
        help="接管旧脚本已写入但无 ledger 的状态；严格核对后仅修互斥字段并登记",
    )
    ap.add_argument(
        "--allow-status-change",
        action="store_true",
        help="仅客户明确改判时使用；允许 approved/rejected 覆盖已有相反客户终判",
    )
    ap.add_argument(
        "--backfill-events",
        action="store_true",
        help="仅为已有 ledger 且无逐条事件的历史反馈补 events；不改账号投影",
    )
    args = ap.parse_args(argv)

    if not args.dry_run and not args.source_decisions:
        print("✗ 安全写入必须提供 --source-decisions", file=sys.stderr)
        return 2
    if args.adopt_existing and not args.source_decisions:
        print("✗ --adopt-existing 必须同时提供 --source-decisions", file=sys.stderr)
        return 2
    if args.adopt_existing and args.allow_status_change:
        print(
            "✗ --adopt-existing 与 --allow-status-change 不能同时使用",
            file=sys.stderr,
        )
        return 2
    if args.backfill_events and not args.source_decisions:
        print("✗ --backfill-events 必须同时提供 --source-decisions", file=sys.stderr)
        return 2
    if args.backfill_events and (args.adopt_existing or args.allow_status_change):
        print(
            "✗ --backfill-events 不能与 --adopt-existing/--allow-status-change 同时使用",
            file=sys.stderr,
        )
        return 2

    try:
        payload = load_validated_decisions(args.file, args.source_decisions)
        _print_preview(payload, dry_run=args.dry_run)
        if args.dry_run and not args.source_decisions:
            print("\n⚠ 未提供 --source-decisions：仅完成旧版格式预览，未校验白名单或数据库。")
            return 0

        result = cc.apply_client_decisions(
            payload["decisions"],
            batch_id=payload["batch"],
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
            adopt_existing=args.adopt_existing,
            allow_status_change=args.allow_status_change,
            backfill_events=args.backfill_events,
            dry_run=args.dry_run,
        )
    except (
        OSError,
        sqlite3.Error,
        FeedbackValidationError,
        cc.ClientDecisionImportError,
    ) as exc:
        print(f"✗ 安全导入中止：{exc}", file=sys.stderr)
        return 2

    if args.backfill_events and result.get("events_already_present"):
        print("\n文件对应的逐条 events 已完整存在，本次幂等跳过。")
    elif args.backfill_events and args.dry_run:
        print(
            f"\n✓ 历史反馈与数据库校验通过；将补 "
            f"{result['events_to_backfill']} 条 events，本次未写入。"
        )
    elif args.backfill_events:
        print(
            f"\n✓ 已追加 {result['events_backfilled']} 条历史 events；"
            "账号投影和原 ledger 未改变。"
        )
    elif result["already_imported"]:
        print("\n文件 SHA-256 已存在于 ledger，本次幂等跳过，没有重复写入。")
    elif args.dry_run:
        print("\n✓ 文件、白名单与数据库状态校验通过；未写入。")
    elif args.adopt_existing:
        print("\n✓ 已核对旧状态、修复互斥字段并登记 SHA-256 ledger。")
    else:
        print("\n✓ 已在单个事务中写入客户状态与 SHA-256 ledger。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
