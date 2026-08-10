"""在历史正式交付上回放候选配置，输出新旧分流差异。

该工具只读输入文件，不接触 Instagram/Modash，也不更新 creator_cache。任何规则提案
在改配置前都应先运行回放；客户已批准账号被新规则排除时属于显式回归，不能静默生效。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import run_v2  # noqa: E402
from extensions.sop_v2.config import config_sha256, load_config  # noqa: E402
from extensions.sop_v2.pipeline.ingest_client_decisions import (  # noqa: E402
    load_validated_decisions,
)


class PolicyReplayError(ValueError):
    """历史交付或反馈无法形成唯一、可复现的回放集合。"""


def _handle(value) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _read_decisions(path: str | Path) -> tuple[dict, str]:
    p = Path(path)
    raw = p.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyReplayError(f"历史 decisions 不是合法 JSON：{p}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("manifest"), dict):
        raise PolicyReplayError("历史 decisions 缺少 manifest")
    if not isinstance(value.get("candidates"), list):
        raise PolicyReplayError("历史 decisions 缺少 candidates[]")
    return value, hashlib.sha256(raw).hexdigest()


def _feedback_map(
    path: str | Path | None,
    *,
    source_decisions_path: str | Path | None = None,
) -> dict[str, str]:
    if path is None:
        return {}
    # 回放标签必须和本次回放的冻结 decisions 做同一套 batch、白名单、pool/score
    # 校验。只解析 feedback 本身会让拿错批次但 handle 重叠的文件静默污染回放。
    if source_decisions_path is None:
        raise PolicyReplayError("加载客户反馈时必须同时提供对应的冻结 decisions")
    payload = load_validated_decisions(path, source_decisions_path)
    result: dict[str, str] = {}
    for row in payload["decisions"]:
        key = _handle(row.get("handle"))
        if not key or key in result:
            raise PolicyReplayError("反馈文件包含空账号或重复账号")
        result[key] = row["action"]
    return result


def replay_decisions(
    source: dict,
    new_config: dict,
    *,
    client_verdicts: dict[str, str] | None = None,
) -> dict:
    """以内嵌原池为 baseline，用候选配置重新决策并汇总客户回归。"""
    manifest = source.get("manifest")
    candidates = source.get("candidates")
    if not isinstance(manifest, dict) or not isinstance(candidates, list):
        raise PolicyReplayError("source 必须包含 manifest 与 candidates[]")
    track = manifest.get("campaign_track")
    if track not in {"paid", "gifting"}:
        raise PolicyReplayError("manifest.campaign_track 必须是 paid 或 gifting")
    labels = {_handle(k): v for k, v in (client_verdicts or {}).items()}
    seen: set[str] = set()
    old_dist: Counter[str] = Counter()
    new_dist: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    changed: list[dict] = []
    approved_to_exclude: list[str] = []
    approved_to_noninclude: list[str] = []
    rejected_to_include: list[str] = []
    approved_not_replayable: list[str] = []
    not_replayable: list[dict] = []

    for index, raw in enumerate(candidates, 1):
        if not isinstance(raw, dict):
            raise PolicyReplayError(f"candidates[{index}] 必须是对象")
        handle = _handle(raw.get("handle"))
        if not handle or handle in seen:
            raise PolicyReplayError(f"candidates[{index}] handle 为空或重复")
        seen.add(handle)
        old_pool = raw.get("final_pool")
        if not isinstance(old_pool, str) or not old_pool:
            raise PolicyReplayError(f"@{handle} 缺少原 final_pool")
        verdict = labels.get(handle)
        old_dist[old_pool] += 1
        candidate = dict(raw)
        candidate.setdefault("campaign_track", track)
        if candidate.get("_reject_reason"):
            # Stage 2/3 的机器淘汰行通常没有淘汰后才会采集的完整字段。继续调用
            # decide_rejected 只会把旧结论原样抄回，不能证明候选配置修复了旧 Gate。
            # 在没有淘汰前冻结快照的情况下，必须明确标为不可回放并禁止给出
            # “可安全提升硬门槛”的结论。
            reason = str(candidate.get("_reject_reason"))
            not_replayable.append(
                {
                    "handle": handle,
                    "client_verdict": verdict,
                    "old_pool": old_pool,
                    "reason": "historical_machine_rejection_missing_pre_gate_snapshot",
                    "historical_reject_reason": reason,
                }
            )
            new_dist["Not-Replayable"] += 1
            transitions[f"{old_pool} -> Not-Replayable"] += 1
            if verdict == "approved":
                approved_not_replayable.append(handle)
            continue

        replayed = run_v2.decide(candidate, new_config)
        new_pool = replayed["final_pool"]
        new_dist[new_pool] += 1
        transitions[f"{old_pool} -> {new_pool}"] += 1
        if old_pool != new_pool:
            changed.append(
                {
                    "handle": handle,
                    "client_verdict": verdict,
                    "old_pool": old_pool,
                    "new_pool": new_pool,
                    "old_review_reasons": raw.get("review_reasons") or [],
                    "old_exclude_reasons": raw.get("exclude_reasons") or [],
                    "new_review_reasons": replayed.get("review_reasons") or [],
                    "new_exclude_reasons": replayed.get("exclude_reasons") or [],
                }
            )
        if verdict == "approved" and new_pool == "Exclude":
            approved_to_exclude.append(handle)
        if verdict == "approved" and not new_pool.startswith("Include"):
            approved_to_noninclude.append(handle)
        if verdict == "rejected" and new_pool.startswith("Include"):
            rejected_to_include.append(handle)

    unknown_labels = sorted(set(labels) - seen)
    if unknown_labels:
        shown = "、".join(f"@{handle}" for handle in unknown_labels[:5])
        raise PolicyReplayError(f"客户反馈包含本次冻结 decisions 外账号：{shown}")

    replay_complete = not not_replayable
    no_approved_exclusions = not approved_to_exclude
    no_approved_downgrades = not approved_to_noninclude

    return {
        "batch_id": manifest.get("batch_id"),
        "campaign_track": track,
        "candidate_count": len(candidates),
        "labeled_count": sum(1 for handle in seen if handle in labels),
        "old_pool_distribution": dict(old_dist),
        "new_pool_distribution": dict(new_dist),
        "transitions": dict(transitions),
        "changed_count": len(changed),
        "approved_to_exclude": approved_to_exclude,
        "approved_to_noninclude": approved_to_noninclude,
        "approved_not_replayable": approved_not_replayable,
        "rejected_to_include": rejected_to_include,
        "not_replayable_count": len(not_replayable),
        "not_replayable": not_replayable,
        "replay_complete": replay_complete,
        "no_approved_exclusions": no_approved_exclusions,
        "no_approved_downgrades": no_approved_downgrades,
        "safe_to_promote_hard_gate": (
            replay_complete and no_approved_exclusions and no_approved_downgrades
        ),
        "changed": changed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用候选配置回放历史正式交付")
    parser.add_argument("--decisions", required=True, help="历史正式 decisions JSON")
    parser.add_argument("--new-config", required=True, help="候选 sop_v2.toml")
    parser.add_argument("--feedback", help="对应客户反馈 JSON（可选，用于回归标记）")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--fail-on-approved-exclude",
        action="store_true",
        help="若候选配置会把客户批准账号送入 Exclude，则以退出码 3 失败",
    )
    args = parser.parse_args(argv)
    try:
        source, source_sha = _read_decisions(args.decisions)
        labels = _feedback_map(
            args.feedback,
            source_decisions_path=args.decisions if args.feedback else None,
        )
        report = replay_decisions(source, load_config(args.new_config), client_verdicts=labels)
        report["source_decisions_sha256"] = source_sha
        report["new_config_sha256"] = config_sha256(args.new_config)
        report["new_config_path"] = str(Path(args.new_config).resolve())
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, PolicyReplayError, ValueError) as exc:
        print(f"✗ 历史回放失败：{exc}", file=sys.stderr)
        return 2
    print(
        f"✓ 历史回放 {report['candidate_count']} 个候选 · "
        f"分流变化 {report['changed_count']} · "
        f"批准→Exclude {len(report['approved_to_exclude'])} → {args.out}"
    )
    if args.fail_on_approved_exclude and report["approved_to_exclude"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
