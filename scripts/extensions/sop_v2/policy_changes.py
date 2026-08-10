"""Append-only 策略变更状态机。

分析器只能提出 proposed。批准、拒绝和回滚都通过新增一行表达，绝不更新旧记录；
批准前强制要求历史回放、测试和配置前后 SHA。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


STATUSES = frozenset({"proposed", "approved", "rejected", "rolled_back"})
_TRANSITIONS = {
    "proposed": {"approved", "rejected"},
    "approved": {"rolled_back"},
}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class PolicyChangeError(ValueError):
    """策略变更缺少来源证据、审计哈希或发生非法状态跃迁。"""


def _json_value(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _valid_sha(value: str | None) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def append_policy_change(
    db_path: str | Path,
    *,
    policy_key: str,
    status: str,
    source_event_ids: Iterable[str] = (),
    before_value: Any = None,
    after_value: Any = None,
    confirmed_by: str | None = None,
    confirmed_at: str | None = None,
    config_sha_before: str | None = None,
    config_sha_after: str | None = None,
    impact_report_sha256: str | None = None,
    test_report_sha256: str | None = None,
    effective_batch: str | None = None,
    supersedes_change_id: str | None = None,
    created_at: str | None = None,
) -> dict:
    key = str(policy_key or "").strip()
    if not key:
        raise PolicyChangeError("policy_key 不能为空")
    if status not in STATUSES:
        raise PolicyChangeError(f"未知策略状态：{status!r}")
    event_ids = list(dict.fromkeys(str(value).strip() for value in source_event_ids if str(value).strip()))
    p = Path(db_path).resolve()
    if not p.is_file():
        raise PolicyChangeError(f"数据库不存在：{p}")
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "name IN ('client_feedback_events','policy_change_log')"
            )
        }
        if tables != {"client_feedback_events", "policy_change_log"}:
            raise PolicyChangeError("数据库尚未迁移反馈事件/策略变更表")

        previous = None
        if supersedes_change_id:
            previous = conn.execute(
                "SELECT * FROM policy_change_log WHERE change_id=?",
                (supersedes_change_id,),
            ).fetchone()
            if previous is None:
                raise PolicyChangeError("supersedes_change_id 不存在")
            if previous["policy_key"] != key:
                raise PolicyChangeError("策略状态转换不能跨 policy_key")
            if status not in _TRANSITIONS.get(previous["status"], set()):
                raise PolicyChangeError(
                    f"不允许 {previous['status']} -> {status} 状态转换"
                )
            already_superseded = conn.execute(
                "SELECT change_id FROM policy_change_log WHERE supersedes_change_id=?",
                (supersedes_change_id,),
            ).fetchone()
            if already_superseded:
                raise PolicyChangeError("该策略记录已经存在后继状态")
            previous_event_ids = _decode_event_ids(previous["source_event_ids_json"])
            previous_before = (
                json.loads(previous["before_value"])
                if previous["before_value"] is not None
                else None
            )
            previous_after = (
                json.loads(previous["after_value"])
                if previous["after_value"] is not None
                else None
            )
            # 状态行只能确认/拒绝/回滚原提案，不能趁转换时替换来源证据或
            # before/after。任何策略内容变化都必须另起 proposed 记录。
            if event_ids and event_ids != previous_event_ids:
                raise PolicyChangeError("策略状态转换不能替换 source_event_ids")
            if before_value is not None and _json_value(before_value) != _json_value(previous_before):
                raise PolicyChangeError("策略状态转换不能替换 before_value")
            if after_value is not None and _json_value(after_value) != _json_value(previous_after):
                raise PolicyChangeError("策略状态转换不能替换 after_value")
            event_ids = previous_event_ids
            before_value = previous_before
            after_value = previous_after
        elif status != "proposed":
            raise PolicyChangeError("非 proposed 状态必须提供 supersedes_change_id")

        if not event_ids:
            raise PolicyChangeError("策略变更至少需要一个 source_event_id")
        placeholders = ",".join("?" for _ in event_ids)
        found = {
            row["event_id"]
            for row in conn.execute(
                f"SELECT event_id FROM client_feedback_events WHERE event_id IN ({placeholders})",
                event_ids,
            )
        }
        missing = [event_id for event_id in event_ids if event_id not in found]
        if missing:
            raise PolicyChangeError("source_event_id 不存在：" + "、".join(missing[:5]))

        if status in {"approved", "rolled_back"}:
            required_strings = {
                "confirmed_by": confirmed_by,
                "confirmed_at": confirmed_at,
                "effective_batch": effective_batch,
            }
            missing_fields = [
                field for field, value in required_strings.items()
                if not isinstance(value, str) or not value.strip()
            ]
            sha_fields = {
                "config_sha_before": config_sha_before,
                "config_sha_after": config_sha_after,
                "impact_report_sha256": impact_report_sha256,
                "test_report_sha256": test_report_sha256,
            }
            missing_fields.extend(
                field for field, value in sha_fields.items() if not _valid_sha(value)
            )
            if missing_fields:
                raise PolicyChangeError(
                    f"{status} 策略缺少确认/回放/测试字段："
                    + "、".join(missing_fields)
                )
        elif status == "rejected":
            if not isinstance(confirmed_by, str) or not confirmed_by.strip():
                raise PolicyChangeError(f"{status} 必须记录 confirmed_by")
            if not isinstance(confirmed_at, str) or not confirmed_at.strip():
                raise PolicyChangeError(f"{status} 必须记录 confirmed_at")

        change_id = uuid.uuid4().hex
        created = created_at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        conn.execute(
            """INSERT INTO policy_change_log
               (change_id,policy_key,status,source_event_ids_json,before_value,
                after_value,confirmed_by,confirmed_at,config_sha_before,
                config_sha_after,impact_report_sha256,test_report_sha256,
                effective_batch,created_at,supersedes_change_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                change_id,
                key,
                status,
                json.dumps(event_ids, ensure_ascii=False, separators=(",", ":")),
                _json_value(before_value),
                _json_value(after_value),
                confirmed_by.strip() if isinstance(confirmed_by, str) else None,
                confirmed_at.strip() if isinstance(confirmed_at, str) else None,
                config_sha_before,
                config_sha_after,
                impact_report_sha256,
                test_report_sha256,
                effective_batch.strip() if isinstance(effective_batch, str) else None,
                created,
                supersedes_change_id,
            ),
        )
        conn.commit()
        return {
            "change_id": change_id,
            "policy_key": key,
            "status": status,
            "source_event_ids": event_ids,
            "supersedes_change_id": supersedes_change_id,
            "created_at": created,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decode_event_ids(value: str) -> list[str]:
    try:
        rows = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PolicyChangeError("历史 source_event_ids_json 无效") from exc
    if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
        raise PolicyChangeError("历史 source_event_ids_json 必须是字符串数组")
    return rows


def proposal_from_analysis(
    db_path: str | Path,
    analysis_path: str | Path,
    *,
    reason_tag: str,
    policy_key: str,
    before_value: Any = None,
    after_value: Any = None,
) -> dict:
    try:
        analysis = json.loads(Path(analysis_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyChangeError("反馈分析报告不可读") from exc
    proposals = analysis.get("strategy_proposals")
    if not isinstance(proposals, list):
        raise PolicyChangeError("反馈分析报告缺少 strategy_proposals[]")
    proposal = next(
        (row for row in proposals if isinstance(row, dict) and row.get("reason_tag") == reason_tag),
        None,
    )
    if proposal is None:
        raise PolicyChangeError(f"分析报告中没有原因标签：{reason_tag}")
    return append_policy_change(
        db_path,
        policy_key=policy_key,
        status="proposed",
        source_event_ids=proposal.get("source_event_ids") or [],
        before_value=before_value,
        after_value=after_value,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记录 append-only 策略变更")
    sub = parser.add_subparsers(dest="command", required=True)
    propose = sub.add_parser("propose", help="从反馈分析报告登记 proposed")
    propose.add_argument("--db", required=True)
    propose.add_argument("--analysis", required=True)
    propose.add_argument("--reason-tag", required=True)
    propose.add_argument("--policy-key", required=True)
    propose.add_argument("--before-value")
    propose.add_argument("--after-value")

    transition = sub.add_parser("transition", help="追加 approved/rejected/rolled_back 状态")
    transition.add_argument("--db", required=True)
    transition.add_argument("--policy-key", required=True)
    transition.add_argument("--status", required=True, choices=["approved", "rejected", "rolled_back"])
    transition.add_argument("--supersedes-change-id", required=True)
    transition.add_argument("--confirmed-by", required=True)
    transition.add_argument("--confirmed-at", required=True)
    transition.add_argument("--config-sha-before")
    transition.add_argument("--config-sha-after")
    transition.add_argument("--impact-report-sha256")
    transition.add_argument("--test-report-sha256")
    transition.add_argument("--effective-batch")
    args = parser.parse_args(argv)
    try:
        if args.command == "propose":
            result = proposal_from_analysis(
                args.db,
                args.analysis,
                reason_tag=args.reason_tag,
                policy_key=args.policy_key,
                before_value=args.before_value,
                after_value=args.after_value,
            )
        else:
            result = append_policy_change(
                args.db,
                policy_key=args.policy_key,
                status=args.status,
                supersedes_change_id=args.supersedes_change_id,
                confirmed_by=args.confirmed_by,
                confirmed_at=args.confirmed_at,
                config_sha_before=args.config_sha_before,
                config_sha_after=args.config_sha_after,
                impact_report_sha256=args.impact_report_sha256,
                test_report_sha256=args.test_report_sha256,
                effective_batch=args.effective_batch,
            )
    except (OSError, sqlite3.Error, PolicyChangeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
