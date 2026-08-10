"""从 append-only 反馈事件生成中文分析和策略提案（只读数据库）。

报告只把客户已终判的 approved/rejected 放入批准率分母；pending 不等于拒绝。没有
填写原因的拒绝仍计入账号结果，但不会被推断成任何原因标签或策略信号。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2.feedback_taxonomy import (  # noqa: E402
    REASON_DEFINITIONS,
    REASON_TAGS,
    TAXONOMY_VERSION,
)


REPORT_SCHEMA_VERSION = 1


class FeedbackAnalysisError(ValueError):
    """反馈事件库无法形成可靠报告。"""


def _connect_read_only(path: str | Path) -> sqlite3.Connection:
    p = Path(path).resolve()
    if not p.is_file():
        raise FeedbackAnalysisError(f"数据库不存在：{p}")
    conn = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _decode_json_array(value: str, *, label: str) -> list:
    try:
        rows = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise FeedbackAnalysisError(f"{label} 不是合法 JSON") from exc
    if not isinstance(rows, list):
        raise FeedbackAnalysisError(f"{label} 必须是数组")
    return rows


def _latest_events(conn: sqlite3.Connection, batch_ids: Iterable[str] | None) -> list[sqlite3.Row]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_feedback_events'"
    ).fetchone()
    if not exists:
        raise FeedbackAnalysisError("数据库尚未迁移 client_feedback_events")
    batches = [str(value).strip() for value in (batch_ids or []) if str(value).strip()]
    where = ""
    params: list[str] = []
    if batches:
        where = "WHERE review_batch IN (" + ",".join("?" for _ in batches) + ")"
        params.extend(batches)
    # 同一评审批次的后续反馈文件视为修订，以最新事件为准；跨批次仍各保留一次结果。
    return conn.execute(
        f"""
        WITH ranked AS (
            SELECT e.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY review_batch, lower(handle)
                       ORDER BY imported_at DESC, rowid DESC
                   ) AS rn
              FROM client_feedback_events e
              {where}
        )
        SELECT * FROM ranked WHERE rn=1
        ORDER BY review_batch, lower(handle)
        """,
        params,
    ).fetchall()


def _event_source_context(row: sqlite3.Row) -> dict:
    """Decode immutable lineage captured from source decisions at import time."""
    if "source_context_json" not in row.keys() or row["source_context_json"] in (
        None,
        "",
    ):
        return {
            "sources": ["unknown/legacy"],
            "golden_seeds": [],
            "legacy": True,
        }
    try:
        context = json.loads(row["source_context_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise FeedbackAnalysisError(
            f"event {row['event_id']} source_context_json 不是合法 JSON"
        ) from exc
    if not isinstance(context, dict):
        raise FeedbackAnalysisError(
            f"event {row['event_id']} source_context_json 必须是对象"
        )

    raw_sources = context.get("discovery_sources")
    if raw_sources in (None, ""):
        raw_sources = []
    if not isinstance(raw_sources, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_sources
    ):
        raise FeedbackAnalysisError(
            f"event {row['event_id']} discovery_sources 必须是字符串数组"
        )
    sources = list(dict.fromkeys(value.strip() for value in raw_sources))
    discovered_via = context.get("discovered_via")
    if discovered_via not in (None, ""):
        if not isinstance(discovered_via, str) or not discovered_via.strip():
            raise FeedbackAnalysisError(
                f"event {row['event_id']} discovered_via 无效"
            )
        if not sources:
            sources = [discovered_via.strip()]

    raw_golden = context.get("golden_seed_handles")
    if raw_golden in (None, ""):
        raw_golden = []
    if not isinstance(raw_golden, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_golden
    ):
        raise FeedbackAnalysisError(
            f"event {row['event_id']} golden_seed_handles 必须是字符串数组"
        )
    golden = list(
        dict.fromkeys(value.strip().lstrip("@").lower() for value in raw_golden)
    )
    return {
        "sources": sources or ["unknown"],
        "golden_seeds": golden,
        "legacy": False,
    }


def _rate_row(stats: Counter) -> dict:
    reviewed = stats["approved"] + stats["rejected"]
    return {
        "approved": stats["approved"],
        "rejected": stats["rejected"],
        "pending": stats["pending"],
        "reviewed_final": reviewed,
        "approval_rate": round(stats["approved"] / reviewed, 4) if reviewed else None,
    }


def _proposal_for(tag: str, stats: dict, *, min_labels: int, min_rounds: int) -> dict:
    definition = next(item for item in REASON_DEFINITIONS if item.code == tag)
    # carryover 账号可能在多个评审批次中被重复提交。策略样本量按唯一账号计算，
    # “覆盖多轮”按最初发现 cohort（origin_batch）计算，不能让同一批账号重复出现
    # 就伪造出新的独立轮次。
    unique_account_count = len(stats["handles"])
    enough = (
        unique_account_count >= min_labels
        and len(stats["origin_batches"]) >= min_rounds
    )
    if definition.change_class == "system_defect":
        action = "verify_and_fix_system"
        next_step = "核验证据；确认后修解析/Gate、补回归测试并精确重算"
    elif definition.change_class == "system_or_policy":
        action = "verify_fact_or_policy"
        next_step = "先区分系统漏判与客户口径变化，再决定修复或发起规则确认"
    elif definition.change_class in {"ranking_signal", "data_quality_or_ranking"}:
        action = "candidate_soft_ranking" if enough else "monitor_signal"
        next_step = (
            "进入软排序实验并做历史回放"
            if enough
            else f"继续观察，初始门槛为 {min_labels} 个标签且覆盖 {min_rounds} 轮"
        )
    elif definition.change_class == "policy_confirmation":
        action = "request_client_confirmation"
        next_step = "向客户确认是否为统一硬要求；确认前只作人工参考/软排序"
    elif definition.change_class == "temporary_recheck":
        action = "temporary_recheck"
        next_step = "进入临时复核队列，不作为永久负样本"
    else:
        action = "manual_review"
        next_step = "人工阅读原始说明后再分类"
    return {
        "reason_tag": tag,
        "label_zh": definition.label_zh,
        "category": definition.category,
        "change_class": definition.change_class,
        "count": unique_account_count,
        "unique_account_count": unique_account_count,
        "event_count": stats["event_count"],
        "repeat_event_count": stats["event_count"] - unique_account_count,
        "batches": sorted(stats["review_batches"]),
        "origin_batches": sorted(stats["origin_batches"]),
        "policy_signal_count": stats["policy_signal_count"],
        "verified_count": stats["verified_count"],
        "source_event_ids": stats["event_ids"],
        "threshold_met": enough,
        "proposal_action": action,
        "next_step": next_step,
        "eligible_for_automatic_hard_gate": False,
        "requires_replay_before_activation": True,
    }


def analyze_feedback(
    db_path: str | Path,
    *,
    batch_ids: Iterable[str] | None = None,
    min_labels: int = 20,
    min_rounds: int = 2,
) -> dict:
    if min_labels <= 0 or min_rounds <= 0:
        raise FeedbackAnalysisError("min_labels/min_rounds 必须是正整数")
    conn = _connect_read_only(db_path)
    try:
        events = _latest_events(conn, batch_ids)
    finally:
        conn.close()

    verdicts: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    rejection_scopes: Counter[str] = Counter()
    batches: Counter[str] = Counter()
    source_stats: defaultdict[str, Counter] = defaultdict(Counter)
    golden_stats: defaultdict[str, Counter] = defaultdict(Counter)
    reason_stats: dict[str, dict] = {}
    invalid_tags: list[dict] = []
    unstructured_reason_queue: list[dict] = []
    unreasoned_rejections = 0
    reasoned_rejections = 0
    missing_source = 0
    legacy_source_context = 0

    for row in events:
        action = row["verdict"]
        verdicts[action] += 1
        scopes[row["feedback_scope"]] += 1
        batches[row["review_batch"]] += 1
        if row["rejection_scope"]:
            rejection_scopes[row["rejection_scope"]] += 1
        tags = _decode_json_array(
            row["reason_tags_json"], label=f"event {row['event_id']} reason_tags_json"
        )
        known_tags = []
        for tag in tags:
            if tag not in REASON_TAGS:
                invalid_tags.append({"event_id": row["event_id"], "reason_tag": tag})
            elif tag not in known_tags:
                known_tags.append(tag)
        if action == "rejected":
            if row["reason_raw"].strip() or known_tags:
                reasoned_rejections += 1
            else:
                unreasoned_rejections += 1
            if row["reason_raw"].strip() and not known_tags:
                unstructured_reason_queue.append(
                    {
                        "event_id": row["event_id"],
                        "review_batch": row["review_batch"],
                        "handle": row["handle"],
                        "reason_raw": row["reason_raw"],
                        "suggested_action": "manual_tagging_required",
                    }
                )
            for tag in known_tags:
                stats = reason_stats.setdefault(
                    tag,
                    {
                        "event_count": 0,
                        "handles": set(),
                        "review_batches": set(),
                        "origin_batches": set(),
                        "policy_signal_count": 0,
                        "verified_count": 0,
                        "event_ids": [],
                    },
                )
                stats["event_count"] += 1
                stats["handles"].add(str(row["handle"]).lower())
                stats["review_batches"].add(row["review_batch"])
                stats["origin_batches"].add(row["origin_batch"])
                stats["policy_signal_count"] += int(row["feedback_scope"] == "policy_signal")
                stats["verified_count"] += int(row["evidence_status"] == "verified")
                stats["event_ids"].append(row["event_id"])

        profile = _event_source_context(row)
        if profile["sources"] in (["unknown"], ["unknown/legacy"]):
            missing_source += 1
        legacy_source_context += int(profile["legacy"])
        for source in profile["sources"]:
            source_stats[source][action] += 1
        for seed in profile["golden_seeds"]:
            golden_stats[seed][action] += 1

    proposals = [
        _proposal_for(tag, reason_stats[tag], min_labels=min_labels, min_rounds=min_rounds)
        for tag in sorted(
            reason_stats,
            key=lambda key: (-len(reason_stats[key]["handles"]), key),
        )
    ]
    total_rejected = verdicts["rejected"]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "db_path": str(Path(db_path).resolve()),
        "selected_batches": sorted(batches),
        "event_count": len(events),
        "batch_event_counts": dict(sorted(batches.items())),
        "verdicts": dict(verdicts),
        "reviewed_final": verdicts["approved"] + verdicts["rejected"],
        "approval_rate": round(
            verdicts["approved"] / (verdicts["approved"] + verdicts["rejected"]), 4
        )
        if verdicts["approved"] + verdicts["rejected"]
        else None,
        "feedback_scope_counts": dict(scopes),
        "rejection_scope_counts": dict(rejection_scopes),
        "reason_coverage": {
            "reasoned_rejections": reasoned_rejections,
            "unreasoned_rejections": unreasoned_rejections,
            "coverage_rate": round(reasoned_rejections / total_rejected, 4)
            if total_rejected
            else None,
        },
        "reason_distribution": [
            {
                "reason_tag": item["reason_tag"],
                "label_zh": item["label_zh"],
                "count": item["count"],
                "batches": item["batches"],
                "origin_batches": item["origin_batches"],
                "event_count": item["event_count"],
                "repeat_event_count": item["repeat_event_count"],
            }
            for item in proposals
        ],
        "source_performance": {
            source: _rate_row(stats)
            for source, stats in sorted(source_stats.items())
        },
        "golden_seed_performance": {
            seed: _rate_row(stats) for seed, stats in sorted(golden_stats.items())
        },
        "strategy_proposals": proposals,
        "unstructured_reason_queue": unstructured_reason_queue,
        "quality_warnings": {
            "invalid_reason_tags": invalid_tags,
            "profiles_missing_source_lineage": missing_source,
            "events_missing_frozen_source_lineage": missing_source,
            "legacy_events_without_source_context": legacy_source_context,
            "empty_rejection_reasons_are_not_strategy_signals": unreasoned_rejections,
            "legacy_free_text_waiting_for_manual_tags": len(unstructured_reason_queue),
        },
        "governance": {
            "pending_excluded_from_approval_rate": True,
            "empty_reason_excluded_from_strategy_learning": True,
            "automatic_hard_gate_changes": False,
            "replay_required": True,
            "strategy_threshold_uses_unique_accounts": True,
            "strategy_round_coverage_uses_origin_batches": True,
            "source_attribution_uses_frozen_event_context": True,
        },
    }


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _escape(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    verdicts = report["verdicts"]
    coverage = report["reason_coverage"]
    lines = [
        "# 客户反馈分析与策略提案",
        "",
        f"- 批次：{', '.join(report['selected_batches']) or '无'}",
        f"- 有效事件：{report['event_count']}",
        f"- 批准 {verdicts.get('approved', 0)} / 拒绝 {verdicts.get('rejected', 0)} / "
        f"待定 {verdicts.get('pending', 0)}",
        f"- 终判批准率：{_pct(report['approval_rate'])}（待定不进入分母）",
        f"- 拒绝原因覆盖：{_pct(coverage['coverage_rate'])}；"
        f"空原因 {coverage['unreasoned_rejections']} 条只记录账号结果，不参与策略学习",
        f"- 历史自由文本待结构化标注：{len(report.get('unstructured_reason_queue') or [])} 条；"
        "标注前不会自动生成策略提案",
        "",
        "## 拒绝原因",
        "",
        "| 原因 | 标签 | 数量 | 覆盖批次 |",
        "|---|---|---:|---|",
    ]
    for item in report["reason_distribution"]:
        lines.append(
            f"| {_escape(item['label_zh'])} | `{item['reason_tag']}` | {item['count']} | "
            f"{_escape(', '.join(item['batches']))} |"
        )
    if not report["reason_distribution"]:
        lines.append("| 暂无结构化原因 | — | 0 | — |")
    lines.extend(
        [
            "",
            "## 来源效果（多触点归因）",
            "",
            "| 来源 | 批准 | 拒绝 | 待定 | 终判批准率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for source, stats in report["source_performance"].items():
        lines.append(
            f"| {_escape(source)} | {stats['approved']} | {stats['rejected']} | "
            f"{stats['pending']} | {_pct(stats['approval_rate'])} |"
        )
    if not report["source_performance"]:
        lines.append("| 暂无来源数据 | 0 | 0 | 0 | — |")
    lines.extend(
        [
            "",
            "## 策略提案",
            "",
            "> 所有提案都只是 proposed；不能自动修改硬门槛，生效前必须确认并回放历史交付。",
            "",
            "| 原因 | 建议动作 | 样本 | 是否达到初始观察门槛 | 下一步 |",
            "|---|---|---:|---|---|",
        ]
    )
    for proposal in report["strategy_proposals"]:
        lines.append(
            f"| {_escape(proposal['label_zh'])} | `{proposal['proposal_action']}` | "
            f"{proposal['count']} | {'是' if proposal['threshold_met'] else '否'} | "
            f"{_escape(proposal['next_step'])} |"
        )
    if not report["strategy_proposals"]:
        lines.append("| 暂无可提案标签 | — | 0 | 否 | 继续积累明确反馈 |")
    return "\n".join(lines) + "\n"


def _write_with_sha(path: Path, content: str) -> str:
    data = content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成客户反馈分析、来源效果和策略提案")
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch-id", action="append", dest="batch_ids")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--min-labels", type=int, default=20)
    parser.add_argument("--min-rounds", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        report = analyze_feedback(
            args.db,
            batch_ids=args.batch_ids,
            min_labels=args.min_labels,
            min_rounds=args.min_rounds,
        )
        json_content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        json_sha = _write_with_sha(Path(args.out_json), json_content)
        md_sha = _write_with_sha(Path(args.out_md), render_markdown(report))
    except (OSError, sqlite3.Error, FeedbackAnalysisError) as exc:
        print(f"✗ 反馈分析失败：{exc}", file=sys.stderr)
        return 2
    print(
        f"✓ 反馈分析 {report['event_count']} 条 · "
        f"批准率 {_pct(report['approval_rate'])} · "
        f"提案 {len(report['strategy_proposals'])} 条"
    )
    print(f"  JSON SHA-256 {json_sha} → {args.out_json}")
    print(f"  Markdown SHA-256 {md_sha} → {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
