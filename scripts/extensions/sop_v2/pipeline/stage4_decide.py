"""④ 决策+交付：collected → decided + 五池。复用 run_v2.decide（不改其口径）。

Modash 补数（fake_pct/creator_country/top_audience_country）：PIPELINE_SPEC 坑B——
不补则 routing 的 modash_core_missing 把候选钉 Review、Include 恒空。--modash-enrich 走 CDP 补数
（尊重 export_only_shortlist）；无 Modash 会话时候选诚实落 Review（非 bug）。

用法：cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.stage4_decide \
        --batch-id SKINCARE-20260716 --track paid --out data/runs/B/decisions.json
    出表：../.venv/bin/python scripts/export_v2_xlsx.py --decisions data/runs/B/decisions.json ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import (  # noqa: E402
    creator_cache as cc,
    pricing as pricing_mod,
    round_contract as round_contract_mod,
    run_v2,
)
from extensions.sop_v2.config import config_sha256, load_config  # noqa: E402


class CarryoverManifestError(ValueError):
    """The carry-over manifest cannot be reconciled with the creator cache."""


_CARRYOVER_RETRY_READY_STATUSES = {"collected", "rejected"}
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MODASH_REPORT_FIELDS = (
    "fake_pct",
    "creator_country",
    "top_audience_country",
    "general_er",
    "target_countries_audience_pct",
    "top_language_pct",
)
_POOL_PROGRESS_RANK = {
    "Exclude": 0,
    "Review": 1,
    "Priority-Review": 2,
    "Include-Without-Storefront": 3,
    "Include-With-Storefront": 3,
}


def _delivery_file_reference(path: Path) -> str:
    """Return a stable customer-safe file reference without host paths."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return resolved.name


def _modash_route_projection(cand: dict, cfg: dict) -> tuple[str, str]:
    """Return current and optimistic post-Modash decision pools."""
    countries = cfg.get("country", {})
    allowed = list(countries.get("tier1") or countries.get("tier2") or [])
    if not allowed:
        return "Review", "Review"
    audience = cfg.get("audience", {})
    optimistic = dict(cand)
    optimistic_values = {
        "fake_pct": 0.0,
        "creator_country": allowed[0],
        "top_audience_country": allowed[0],
        "target_countries_audience_pct": max(
            float(audience.get("target_share_full", 100.0)),
            float(audience.get("target_share_partial", 0.0)),
        ),
        "top_language_pct": max(
            100.0, float(audience.get("language_min_share", 0.0))
        ),
        "general_er": 100.0,
    }
    # Mirror modash_cdp._apply: an existing observed value is never overwritten.
    for field, value in optimistic_values.items():
        if optimistic.get(field) is None:
            optimistic[field] = value
    current_pool = run_v2.decide(dict(cand), cfg)["final_pool"]
    optimistic_pool = run_v2.decide(optimistic, cfg)["final_pool"]
    return current_pool, optimistic_pool


def _modash_enrichment_actionable(cand: dict, cfg: dict) -> bool:
    """Return whether a best-case Modash report can still change the route.

    Profile reports cannot repair Instagram/browser facts such as too few valid
    comments, unknown storefront, low real ER, sponsorship review, graph audit or
    the lifestyle cap.  Spending a credit on such a row cannot unlock Include.
    We therefore inject only optimistic *Modash-owned* values and require all
    remaining gates/fixed reviews to pass.  The real report may of course still
    reveal an exclusion; this predicate only prevents credits that are guaranteed
    to be non-actionable before the request.
    """
    current_pool, optimistic_pool = _modash_route_projection(cand, cfg)
    return _POOL_PROGRESS_RANK.get(optimistic_pool, -1) > _POOL_PROGRESS_RANK.get(
        current_pool, -1
    )


def _build_modash_shortlist(
    active: list[dict], cfg: dict, cap: int
) -> tuple[list[dict], dict[str, int]]:
    """Build a deterministic, budget-aware Profile Report shortlist."""
    from extensions.sop_v2 import storefront

    modash_core = tuple(cfg["modash_gates"]["core_fields"])
    missing_report = [
        cand
        for cand in active
        if any(cand.get(field) is None for field in _MODASH_REPORT_FIELDS)
    ]
    actionable = [
        cand
        for cand in missing_report
        if _modash_enrichment_actionable(cand, cfg)
    ]
    actionable.sort(
        key=lambda cand: (
            _POOL_PROGRESS_RANK.get(_modash_route_projection(cand, cfg)[1], -1),
            storefront.has_storefront(cand),
            cand.get("real_er") or 0,
        ),
        reverse=True,
    )
    selected = actionable if cap == 0 else actionable[:cap]
    return selected, {
        "active": len(active),
        "missing_core": sum(
            any(cand.get(field) is None for field in modash_core)
            for cand in active
        ),
        "missing_report_fields": len(missing_report),
        "actionable": len(actionable),
        "non_actionable_skipped": len(missing_report) - len(actionable),
        "selected": len(selected),
    }


def _build_modash_all_missing_shortlist(
    candidates: list[dict], cap: int
) -> tuple[list[dict], dict[str, int]]:
    """Select every candidate without a proven Profile Report.

    This is the formal-delivery complement to the budget-aware actionable
    shortlist.  ``modash_report is True`` is the sole report-presence signal:
    reports are not re-fetched merely because an individual source field is
    unavailable.  Input order is preserved so a capped retry is deterministic.
    """
    missing = [
        cand for cand in candidates if cand.get("modash_report") is not True
    ]
    selected = missing if cap == 0 else missing[:cap]
    return selected, {
        "candidate_count": len(candidates),
        "report_present": len(candidates) - len(missing),
        "report_missing": len(missing),
        "selected": len(selected),
    }


def _sponsorship_window_is_consistent(cand: dict) -> bool:
    """Recompute sponsorship saturation from the stored caption window."""
    from extensions.sop_v2 import content

    saturation = cand.get("sponsorship_saturation")
    if (
        isinstance(saturation, bool)
        or not isinstance(saturation, (int, float))
        or not math.isfinite(float(saturation))
        or not 0 <= float(saturation) <= 100
    ):
        return False
    sampled_posts = cand.get("sampled_posts")
    if not isinstance(sampled_posts, list) or not sampled_posts:
        return False
    captions: list[str] = []
    for post in sampled_posts[:15]:
        if not isinstance(post, dict):
            return False
        caption = post.get("caption")
        if caption is None:
            caption = post.get("caption_text")
        if caption is None:
            caption = ""
        if not isinstance(caption, str):
            return False
        captions.append(caption.lower())
    sponsored = sum(
        any(term in caption for term in content.SPONSOR)
        for caption in captions
    )
    expected = round(sponsored / len(captions) * 100, 1)
    return math.isclose(float(saturation), expected, abs_tol=0.05)


def _enrichment_completeness(
    candidates: list[dict],
) -> tuple[dict[str, int], list[tuple[str, list[str]]]]:
    """Return manifest-safe coverage statistics and per-handle failures."""
    from extensions.sop_v2 import storefront

    failures: list[tuple[str, list[str]]] = []
    modash_complete = storefront_complete = sponsorship_complete = 0
    for cand in candidates:
        reasons: list[str] = []
        if cand.get("modash_report") is True:
            modash_complete += 1
        else:
            reasons.append("modash_report未完成")
        storefront_reasons = storefront.delivery_validation_reasons(cand)
        if not storefront_reasons:
            storefront_complete += 1
        else:
            reasons.append(
                "storefront不完整(" + ",".join(storefront_reasons) + ")"
            )
        if _sponsorship_window_is_consistent(cand):
            sponsorship_complete += 1
        else:
            reasons.append("赞助饱和度/赞助帖目标窗口不自洽")
        if reasons:
            failures.append((str(cand.get("handle") or "<missing>"), reasons))
    total = len(candidates)
    stats = {
        "candidate_count": total,
        "complete_count": total - len(failures),
        "incomplete_count": len(failures),
        "modash_report_complete_count": modash_complete,
        "modash_report_missing_count": total - modash_complete,
        "storefront_complete_count": storefront_complete,
        "storefront_incomplete_count": total - storefront_complete,
        "sponsorship_complete_count": sponsorship_complete,
        "sponsorship_incomplete_count": total - sponsorship_complete,
    }
    return stats, failures


def _carryover_retry_is_ready(
    cand: dict,
    *,
    target_posts: int,
    require_full_deep: bool,
) -> bool:
    """Return whether a manifest-marked retry is safe to reuse now.

    ``decided`` is a valid result of an earlier Stage 4 run with the same
    manifest.  It is reusable only when the current database row has no
    collection error and its current evidence still satisfies the strict deep
    contract.  This keeps repeated Stage 4 runs idempotent without allowing an
    old, incomplete ``decided`` row through the carryover gate.
    """
    status = cand.get("_status")
    if status in _CARRYOVER_RETRY_READY_STATUSES:
        return True
    if status != "decided" or cand.get("_stage_error"):
        return False
    return not cc.strict_deep_reasons(
        cand,
        status=status,
        target_posts=target_posts,
        require_full_deep=require_full_deep,
    )


def _handle(value) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _strict_count(counts: dict, key: str) -> int:
    value = counts.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CarryoverManifestError(f"counts.{key} 必须是非负整数")
    return value


def _load_carryover_manifest(path: Path, batch_id: str) -> dict:
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CarryoverManifestError(f"无法读取 carryover manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CarryoverManifestError("carryover manifest 根节点必须是 object")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise CarryoverManifestError("carryover manifest schema_version 必须为 1")
    manifest_batch = manifest.get("next_batch_id")
    if not isinstance(manifest_batch, str) or manifest_batch.strip() != batch_id:
        raise CarryoverManifestError(
            f"carryover manifest next_batch_id 必须等于 --batch-id {batch_id}"
        )

    rows = manifest.get("carryover")
    if not isinstance(rows, list):
        raise CarryoverManifestError("carryover manifest 缺少 carryover[]")
    handles: set[str] = set()
    origins: set[str] = set()
    retry_by_row: set[str] = set()
    normalized_rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CarryoverManifestError(f"carryover[{index}] 必须是 object")
        raw_handle = row.get("handle")
        if not isinstance(raw_handle, str):
            raise CarryoverManifestError(f"carryover[{index}].handle 必须是 string")
        handle = _handle(raw_handle)
        if not handle:
            raise CarryoverManifestError(f"carryover[{index}].handle 为空")
        if handle in handles:
            raise CarryoverManifestError(f"carryover handle 重复: {handle}")
        raw_origin = row.get("origin_batch")
        if not isinstance(raw_origin, str):
            raise CarryoverManifestError(
                f"carryover[{index}].origin_batch 必须是 string"
            )
        origin = raw_origin.strip()
        if not origin:
            raise CarryoverManifestError(f"carryover[{index}].origin_batch 为空")
        if origin == batch_id:
            raise CarryoverManifestError(
                f"carryover handle {handle} 的 origin_batch 不能是当前新批次"
            )
        needs_retry = row.get("needs_pipeline_retry")
        if not isinstance(needs_retry, bool):
            raise CarryoverManifestError(
                f"carryover[{index}].needs_pipeline_retry 必须是 boolean"
            )
        handles.add(handle)
        origins.add(origin)
        if needs_retry:
            retry_by_row.add(handle)
        normalized_rows.append({**row, "handle": handle, "origin_batch": origin})

    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise CarryoverManifestError("carryover manifest 缺少 counts")
    source_count = _strict_count(counts, "source_candidates")
    client_final_count = _strict_count(counts, "client_final")
    carryover_count = _strict_count(counts, "carryover")
    retry_count = _strict_count(counts, "pipeline_retry")
    mode_excluded = counts.get("mode_excluded", 0)
    if isinstance(mode_excluded, bool) or not isinstance(mode_excluded, int) or mode_excluded < 0:
        raise CarryoverManifestError("counts.mode_excluded 必须是非负整数")
    if carryover_count != len(normalized_rows):
        raise CarryoverManifestError(
            f"counts.carryover={carryover_count} 与 carryover[]={len(normalized_rows)} 不一致"
        )
    if source_count != client_final_count + carryover_count + mode_excluded:
        raise CarryoverManifestError(
            "counts.source_candidates 必须等于 counts.client_final + "
            "counts.carryover + counts.mode_excluded"
        )

    retry_handles = manifest.get("retry_handles")
    if not isinstance(retry_handles, list):
        raise CarryoverManifestError("carryover manifest 缺少 retry_handles[]")
    if any(not isinstance(handle, str) for handle in retry_handles):
        raise CarryoverManifestError("retry_handles[] 必须全部是 string")
    normalized_retry = [_handle(handle) for handle in retry_handles]
    if any(not handle for handle in normalized_retry):
        raise CarryoverManifestError("retry_handles[] 含空 handle")
    if len(set(normalized_retry)) != len(normalized_retry):
        raise CarryoverManifestError("retry_handles[] 含重复 handle")
    retry_set = set(normalized_retry)
    if retry_set != retry_by_row or retry_count != len(retry_set):
        raise CarryoverManifestError(
            "pipeline_retry 计数、retry_handles[] 与 needs_pipeline_retry 标记不一致"
        )

    batches = manifest.get("source_batches")
    if not isinstance(batches, list) or not batches:
        raise CarryoverManifestError("carryover manifest 缺少 source_batches[]")
    if any(not isinstance(value, str) for value in batches):
        raise CarryoverManifestError("source_batches[] 必须全部是 string")
    normalized_batches = [value.strip() for value in batches]
    if any(not value for value in normalized_batches):
        raise CarryoverManifestError("source_batches[] 含空批次")
    if len(set(normalized_batches)) != len(normalized_batches):
        raise CarryoverManifestError("source_batches[] 含重复批次")
    manifest_batches = set(normalized_batches)
    if batch_id not in manifest_batches or not origins.issubset(manifest_batches):
        raise CarryoverManifestError(
            "source_batches[] 必须包含全部 origin_batch 与 next_batch_id"
        )

    expected_handle_sha = hashlib.sha256(
        "\n".join(sorted(handles)).encode("utf-8")
    ).hexdigest()
    if manifest.get("carryover_handle_set_sha256") != expected_handle_sha:
        raise CarryoverManifestError("carryover_handle_set_sha256 与 carryover[] 不一致")

    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": normalized_rows,
        "source_batches": normalized_batches,
        "manifest": manifest,
    }


def _export_with_carryover(
    path: Path,
    batch_id: str,
    *,
    target_posts: int = 10,
    require_full_deep: bool = False,
    contract: dict | None = None,
    contract_sha256: str | None = None,
) -> tuple[list[dict], dict]:
    meta = _load_carryover_manifest(path, batch_id)
    if contract is not None:
        try:
            round_contract_mod.validate_carryover_for_contract(
                contract,
                meta["manifest"],
                contract_sha256=contract_sha256,
            )
        except round_contract_mod.RoundContractError as exc:
            raise CarryoverManifestError(str(exc)) from exc
    exported = cc.export_all_with_data(meta["source_batches"])
    carryover_by_handle = {row["handle"]: row for row in meta["rows"]}

    relevant: dict[str, dict] = {}
    for cand in exported:
        handle = _handle(cand.get("handle"))
        origin = str(cand.get("_discovery_batch") or "").strip()
        if handle in carryover_by_handle or origin == batch_id:
            if not handle:
                raise CarryoverManifestError("creator_cache 导出候选含空 handle")
            if handle in relevant:
                raise CarryoverManifestError(f"creator_cache 导出 handle 冲突: {handle}")
            relevant[handle] = cand

    carryover = []
    retry_pending = []
    for row in meta["rows"]:
        handle = row["handle"]
        cand = relevant.get(handle)
        if cand is None:
            raise CarryoverManifestError(
                f"carryover handle 在 creator_cache 中缺失或无 stage2/3 数据: {handle}"
            )
        actual_origin = str(cand.get("_discovery_batch") or "").strip()
        if actual_origin != row["origin_batch"]:
            raise CarryoverManifestError(
                f"carryover handle {handle} 来源批次不匹配: "
                f"manifest={row['origin_batch']} db={actual_origin or '<empty>'}"
            )
        carryover.append(cand)
        if row["needs_pipeline_retry"] and not _carryover_retry_is_ready(
            cand,
            target_posts=target_posts,
            require_full_deep=require_full_deep,
        ):
            retry_pending.append(handle)

    carryover_handles = set(carryover_by_handle)
    current = []
    for handle, cand in relevant.items():
        if str(cand.get("_discovery_batch") or "").strip() != batch_id:
            continue
        if handle in carryover_handles:
            raise CarryoverManifestError(
                f"carryover handle 同时属于当前新批次，无法保持 origin: {handle}"
            )
        current.append(cand)

    meta["carryover_count"] = len(carryover)
    meta["new_batch_candidate_count"] = len(current)
    meta["retry_pending_handles"] = retry_pending
    meta["retry_pending_count"] = len(retry_pending)
    return carryover + current, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--track", choices=["paid", "gifting"], required=True)
    scope_group = ap.add_mutually_exclusive_group()
    scope_group.add_argument("--all-batches", action="store_true",
                             help="跨所有 batch 汇总有数据候选（默认仅本 batch）")
    scope_group.add_argument(
        "--carryover-manifest",
        help="下一轮结转清单：仅汇总清单旧候选 + 当前新批次有数据候选",
    )
    ap.add_argument(
        "--allow-incomplete-carryover",
        action="store_true",
        help="允许仍待补采的 carryover 出草稿；默认阻断正式决策交付",
    )
    ap.add_argument(
        "--strict-completeness",
        action="store_true",
        help="正式交付前严格复核帖子覆盖、互动指标及评论零样本证据",
    )
    ap.add_argument(
        "--strict-enrichment-completeness",
        action="store_true",
        help=(
            "正式交付补数门禁：全部候选须有 Modash 报告、Storefront "
            "yes/no 结论及自洽的赞助目标窗口"
        ),
    )
    ap.add_argument(
        "--full-deep-all-candidates",
        action="store_true",
        help="严格模式同时要求 Stage 2 机器淘汰候选也完成深采",
    )
    ap.add_argument(
        "--deep-target-posts",
        type=int,
        default=10,
        help="严格完整性要求的深采帖子数（默认 10）",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--xlsx", default=None, help="交付 XLSX 路径（默认与 --out 同目录 deliverable.xlsx）")
    ap.add_argument("--no-xlsx", action="store_true", help="只出 decisions.json，不出交付表")
    ap.add_argument("--generated-at", default=None, help="固定时间戳（可复现）")
    ap.add_argument(
        "--round-contract",
        default=None,
        help="冻结的 round_contract.json；正式决策前校验 batch/track/config/code/carryover",
    )
    ap.add_argument(
        "--require-round-contract",
        action="store_true",
        help="缺少 --round-contract 时失败（正式交付推荐）",
    )
    ap.add_argument("--modash-csv", default=None,
                    help="Modash Profile Report CSV：补假粉/受众/国家(解除 modash_core_missing→Include 才可能非空)")
    ap.add_argument("--manual-csv", default=None,
                    help="人工核验 CSV：raw_skin_grade/has_vo/paid_cpm(解除 raw_skin_or_vo_unverified→Include)")
    ap.add_argument(
        "--translate-comments",
        action="store_true",
        help=(
            "草稿/补译模式：显式调用 LLM 翻译已存评论并随普通 advance 回写；"
            "正式模式禁用（不重访 IG）"
        ),
    )
    ap.add_argument(
        "--strict-comment-translations",
        action="store_true",
        help=(
            "正式交付只读门禁：仅校验 Stage 3 已存翻译，绝不调用 LLM、"
            "不截断或改写候选"
        ),
    )
    ap.add_argument(
        "--translation-provider",
        choices=["ollama", "anthropic"],
        default="ollama",
        help="评论翻译 LLM；默认使用本机 Ollama，Anthropic 仅为显式备用",
    )
    ap.add_argument("--translation-model", default=None,
                    help="翻译模型；Ollama 默认 qwen3.5:4b")
    ap.add_argument("--translation-api-url", default=None,
                    help="自定义 Ollama / Anthropic API URL")
    ap.add_argument("--translation-batch-size", type=int, default=40)
    ap.add_argument(
        "--translation-source-limit",
        "--translation-display-limit",
        dest="translation_source_limit",
        type=int,
        default=120,
        help=(
            "仅供 --translate-comments：每候选送 LLM 的已存评论上限；"
            "严格只读门禁忽略此值（默认 120）"
        ),
    )
    ap.add_argument("--modash-cdp", action="store_true",
                    help="驱动已登录 Modash Chrome(9222)读 show-profile 补假粉/受众/国家(每个约1 credit)")
    ap.add_argument(
        "--modash-all-missing",
        action="store_true",
        help=(
            "正式补数模式：启用 CDP，选取所有 modash_report 不为 true 的候选；"
            "已有报告不重抓"
        ),
    )
    ap.add_argument(
        "--modash-cap",
        type=int,
        default=None,
        help=(
            "Modash 补数上限；未传沿用 config，显式 0 表示不截断、补完整个 shortlist"
        ),
    )
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = ap.parse_args()
    if args.deep_target_posts <= 0:
        ap.error("--deep-target-posts 必须为正整数")
    if args.modash_cap is not None and args.modash_cap < 0:
        ap.error("--modash-cap 不能为负数")
    if args.translation_batch_size <= 0:
        ap.error("--translation-batch-size 必须为正整数")
    if args.translation_source_limit <= 0:
        ap.error("--translation-source-limit 必须为正整数")
    if (
        args.modash_all_missing
        and not args.strict_enrichment_completeness
    ):
        print(
            "✗ --modash-all-missing 仅允许正式补数，必须同时提供 "
            "--strict-enrichment-completeness"
        )
        return 1
    if args.modash_all_missing and args.modash_cap != 0:
        print(
            "✗ --modash-all-missing 必须显式提供 --modash-cap 0："
            "完整补数不能使用会反复停在同一前缀的预算截断"
        )
        return 1
    if args.translate_comments and args.strict_comment_translations:
        print(
            "✗ --translate-comments 与 --strict-comment-translations 无条件互斥："
            "先独立补译，再单独运行只读验收"
        )
        return 1
    formal_requested = bool(
        args.round_contract
        or args.require_round_contract
        or args.strict_completeness
        or args.strict_enrichment_completeness
        or args.modash_all_missing
        or args.full_deep_all_candidates
    )
    if args.translate_comments and formal_requested:
        print(
            "✗ 正式 Stage 4 禁止 --translate-comments：请先用独立补译流程持久化，"
            "再以 --strict-comment-translations 只读验收，避免覆盖深采 ledger"
        )
        return 1
    cfg = load_config()
    contract = None
    contract_sha = None
    if args.round_contract:
        try:
            contract_path = Path(args.round_contract)
            contract = round_contract_mod.assert_contract_matches_runtime(
                round_contract_mod.load_round_contract(contract_path),
                batch_id=args.batch_id,
                campaign_track=args.track,
            )
            contract_sha = round_contract_mod.round_contract_sha256(contract_path)
        except (OSError, round_contract_mod.RoundContractError) as exc:
            print(f"✗ 轮次合同校验失败：{exc}")
            return 1
        if args.all_batches:
            print("✗ 正式轮次合同禁止 --all-batches；必须按 carryover_mode 精确选取")
            return 1
        if contract["carryover_mode"] != "new_only" and not args.carryover_manifest:
            print(
                f"✗ {contract['carryover_mode']} 轮次必须提供 --carryover-manifest"
            )
            return 1
    elif args.require_round_contract:
        print("✗ 正式决策要求 --round-contract")
        return 1

    # 客户铁律：抓过就纳入——拉所有有数据候选（非 seed），rejected 也进（Exclude 池写明原因，不 silently drop）
    carryover_meta = None
    try:
        if args.carryover_manifest:
            cands, carryover_meta = _export_with_carryover(
                Path(args.carryover_manifest),
                args.batch_id,
                target_posts=args.deep_target_posts,
                require_full_deep=args.full_deep_all_candidates,
                contract=contract,
                contract_sha256=contract_sha,
            )
        else:
            scope = None if args.all_batches else [args.batch_id]
            cands = cc.export_all_with_data(scope)
    except CarryoverManifestError as exc:
        print(f"✗ {exc}")
        return 1
    if args.allow_incomplete_carryover and not carryover_meta:
        print("✗ --allow-incomplete-carryover 只能与 --carryover-manifest 同用")
        return 1
    if (
        carryover_meta
        and carryover_meta["retry_pending_count"]
        and not args.allow_incomplete_carryover
    ):
        print(
            f"✗ carryover 仍有 {carryover_meta['retry_pending_count']} 个需补采/续跑；"
            "完成后再交付，或显式加 --allow-incomplete-carryover 仅生成草稿"
        )
        return 1
    if not cands:
        print("✗ 无有数据候选（先跑 stage2/3）"); return 1
    strict_mode = args.strict_completeness or args.full_deep_all_candidates
    if strict_mode:
        incomplete = []
        for cand in cands:
            status = cand.get("_status")
            reasons = cc.strict_deep_reasons(
                cand,
                status=status,
                target_posts=args.deep_target_posts,
                require_full_deep=args.full_deep_all_candidates,
            )
            if status != "rejected" or args.full_deep_all_candidates:
                reasons.extend(pricing_mod.completion_reasons(cand, cfg))
            if reasons:
                incomplete.append((cand.get("handle"), status, reasons))
        if incomplete:
            mode = (
                "strict/all-candidates"
                if args.full_deep_all_candidates
                else "strict/active"
            )
            print(
                f"✗ {mode} 完整性门禁失败：{len(incomplete)} 个候选的深采/报价证据未闭合",
                flush=True,
            )
            for handle, status, reasons in incomplete[:30]:
                print(
                    f"  @{handle} [{status}] {'；'.join(reasons)}",
                    flush=True,
                )
            if len(incomplete) > 30:
                print(f"  …另有 {len(incomplete) - 30} 个", flush=True)
            return 1
    active = [c for c in cands if c.get("_status") != "rejected"]     # 走完整 decide
    rejected = [c for c in cands if c.get("_status") == "rejected"]   # 直接归 Exclude 写原因
    for c in active:
        c.setdefault("campaign_track", args.track)
    if carryover_meta:
        scope_txt = (
            f"carryover {carryover_meta['carryover_count']} + "
            f"新批 {carryover_meta['new_batch_candidate_count']} + "
            f"待补采 {carryover_meta['retry_pending_count']}"
        )
    else:
        scope_txt = "跨全部 batch" if args.all_batches else f"batch {args.batch_id}"
    print(f"纳入 {len(cands)}（{scope_txt}）：活跃 {len(active)}(qualified/collected/decided) + 机器淘汰 {len(rejected)}")

    # 翻译完全依赖已存评论，应在任何付费 Modash 补数前完成。正式门禁只读
    # Stage 3 已持久化缓存，绝不在 Stage 4 内补译或改写候选/attempt ledger。
    translation_summary = None
    if args.translate_comments:
        from extensions.sop_v2 import comment_translation

        model = args.translation_model or (
            comment_translation.DEFAULT_MODEL
            if args.translation_provider == "ollama"
            else comment_translation.DEFAULT_ANTHROPIC_MODEL
        )
        print(
            f"评论补译（显式草稿模式）：{args.translation_provider}/{model}，"
            f"候选 {len(cands)}（离线处理已存原文，不访问 Instagram）…",
            flush=True,
        )
        try:
            translation_summary = comment_translation.translate_candidates(
                cands,
                provider=args.translation_provider,
                model=model,
                api_url=args.translation_api_url,
                batch_size=args.translation_batch_size,
                source_limit=args.translation_source_limit,
                usage_context={
                    "batch_id": args.batch_id,
                    "stage": "stage4_decide",
                    "feature": "comment_translation",
                },
            )
        except comment_translation.TranslationConfigurationError as exc:
            print(f"✗ 评论翻译配置错误：{exc}")
            return 1
        print(
            "评论补译："
            f"成功 {translation_summary['translated_count']}/"
            f"{translation_summary['requested_count']} · "
            f"失败 {translation_summary['failed_count']} · "
            f"历史原文缺失 {translation_summary['source_unavailable_count']}",
            flush=True,
        )

    if args.strict_comment_translations:
        from extensions.sop_v2 import comment_translation

        print(
            f"评论翻译只读验收：候选 {len(cands)}（不调用 LLM、不改写候选）…",
            flush=True,
        )
        translation_summary = comment_translation.validate_translation_cohort(cands)
        print(
            "评论翻译只读验收："
            f"有效候选 {translation_summary['valid_candidate_count']}/"
            f"{translation_summary['candidate_count']} · "
            f"译文 {translation_summary['translated_count']}/"
            f"{translation_summary['requested_count']} · "
            f"无效候选 {translation_summary['invalid_candidate_count']}",
            flush=True,
        )
        if translation_summary["invalid_candidate_count"]:
            print(
                "✗ 正式评论翻译门禁失败：先用独立补译/恢复流程修复缓存，"
                "Stage 4 未调用翻译服务且未改写翻译字段",
                flush=True,
            )
            for row in translation_summary["failures"]:
                print(
                    f"  @{row['handle']} {'；'.join(row['failures'])}",
                    flush=True,
                )
            return 1

    modash_enrichment_summary = None
    if args.modash_cdp or args.modash_all_missing:
        from extensions.sop_v2.pipeline.modash_cdp import enrich_via_cdp
        cap = (
            int(cfg.get("modash_budget", {}).get("profile_per_round", 20))
            if args.modash_cap is None
            else args.modash_cap
        )
        if args.modash_all_missing:
            # 正式交付模式覆盖 active + rejected；只认报告存在标记，报告内
            # 个别源字段为空不代表需要再次消耗 Profile credit。
            shortlist, shortlist_stats = _build_modash_all_missing_shortlist(
                cands, cap
            )
            # CDP apply 只填充 None，历史显式 False 同样表示“未取得报告”。
            for candidate in shortlist:
                candidate["modash_report"] = None
            shortlist_mode = "all_missing_reports"
        else:
            # 省 credit：只补 active 里缺核心字段、且最优报告仍有机会解除
            # Review 的候选。非 Modash 固定 blocker 不花 credit。
            shortlist, shortlist_stats = _build_modash_shortlist(
                active, cfg, cap
            )
            shortlist_mode = "route_actionable"
        disc = cfg.get("discovery", {})
        t = cfg["track"][args.track]
        lo, hi = ((t["min_followers"], t["max_followers"]) if args.track == "paid"
                  else (t["standard_min"], t["priority_max"]))
        # 补数重搜必须和 discovery 用**同一套筛选**（含 11 国地区 + 受众可信度），否则搜索空间不同、
        # 很多号在重搜的前若干页翻不到 → spid 拿不到 → 假性"待补数"（实测命中率从 15/40 掉下来的根因）。
        filt = {"followers": {"min": lo, "max": hi},
                "engagementRate": {"min": disc.get("search_er_min", 0.015)}}
        geo_ids = disc.get("search_creator_geo_ids")
        if geo_ids:
            filt["geo"] = list(geo_ids)
        cred_min = disc.get("search_audience_credibility_min")
        if cred_min:
            filt["audience"] = {"credibility": float(cred_min)}
        if args.modash_all_missing:
            print(
                "Modash 全量缺报告补数(CDP)："
                f"已有报告 {shortlist_stats['report_present']} · "
                f"缺报告 {shortlist_stats['report_missing']} · "
                f"本次 {len(shortlist)}/{len(cands)}"
                f"（已有报告不重抓，"
                f"{'不设上限' if cap == 0 else f'上限 {cap}'}）…"
            )
        else:
            print(
                "Modash 补数(CDP)："
                f"缺核心字段 {shortlist_stats['missing_core']} · "
                f"报告字段缺口 {shortlist_stats['missing_report_fields']} · "
                f"可改变结论 {shortlist_stats['actionable']} · "
                f"非可晋级跳过 {shortlist_stats['non_actionable_skipped']} · "
                f"本次 {len(shortlist)}/{len(active)}"
                f"（完整旧数据不重抓，"
                f"{'不设上限' if cap == 0 else f'上限 {cap}'}）…"
            )
        cache_dir = str(Path(args.out).with_name("modash_raw"))  # 原始报告落盘→改解析器免重付费
        try:
            r = enrich_via_cdp(
                shortlist,
                disc.get("search_query", ""),
                filt,
                args.cdp,
                cache_dir=cache_dir,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "✗ Modash 补数运行失败："
                f"{type(exc).__name__}；未写入候选库或正式交付文件",
                flush=True,
            )
            return 1
        modash_enrichment_summary = {
            "mode": shortlist_mode,
            "cap": cap,
            "shortlist": shortlist_stats,
            "result": {
                key: r.get(key)
                for key in ("matched", "total", "from_cache", "error")
                if key in r
            },
        }
        print(f"Modash 补数(CDP): 命中 {r.get('matched')}/{r.get('total')}"
              + (f"  ⚠ {r['error']}" if r.get("error") else ""))
    elif args.modash_csv:
        from extensions.sop_v2.pipeline.modash_enrich import enrich
        r = enrich(active, args.modash_csv)
        print(f"Modash 补数(CSV): 匹配 {r['matched']}/{r['total']}（CSV {r['csv_rows']} 行）")
    else:
        print("⚠ 未提供 Modash 补数：缺假粉/受众/国家 → 候选诚实落 Review(modash_core_missing)。")
    if args.manual_csv:
        from extensions.sop_v2.pipeline.modash_enrich import enrich_manual
        r = enrich_manual(active, args.manual_csv)
        print(f"人工核验回填(Raw Skin/VO/报价): 匹配 {r.get('matched')}/{r.get('total')}")

    enrichment_stats, enrichment_failures = _enrichment_completeness(cands)
    if args.strict_enrichment_completeness and enrichment_failures:
        print(
            "✗ enrichment 完整性门禁失败："
            f"{len(enrichment_failures)}/{len(cands)} 个候选仍有补数缺口；"
            "未写入候选库或正式交付文件",
            flush=True,
        )
        for handle, reasons in enrichment_failures:
            print(f"  @{handle} {'；'.join(reasons)}", flush=True)
        return 1

    # 活跃号走完整 decide；机器淘汰号直接归 Exclude 写原因（客户铁律：淘汰也要体现）
    decisions = [run_v2.decide(c, cfg) for c in active] + [run_v2.decide_rejected(c, cfg) for c in rejected]
    # 只推进"真正深采完"的（collected/decided）→ decided；qualified 留 qualified（可再深采）、rejected 留 rejected
    for c, d in zip(active, decisions):
        if c.get("_status") in ("collected", "decided"):
            cc.advance(c["handle"], "decided", {**c, "final_pool": d["final_pool"]})
    assert len(decisions) == len(cands), f"对账失败：决策 {len(decisions)} ≠ 候选 {len(cands)}（疑 silent drop）"

    batches = sorted(
        {args.batch_id}
        | {c.get("_discovery_batch") for c in cands if c.get("_discovery_batch")}
    )
    decision_manifest = {
        "batch_id": args.batch_id,
        "batches": batches,
        "sop_version": cfg.get("sop_version", ""),
        "campaign_track": args.track,
        "config_sha256": config_sha256(None),
        "candidate_count": len(decisions),
        "strict_completeness": strict_mode,
        "strict_pricing": strict_mode,
        "strict_enrichment_completeness": (
            args.strict_enrichment_completeness
        ),
        "enrichment_completeness": enrichment_stats,
        "full_deep_all_candidates": args.full_deep_all_candidates,
        "deep_target_posts": args.deep_target_posts,
    }
    if modash_enrichment_summary is not None:
        decision_manifest["modash_enrichment"] = modash_enrichment_summary
    if contract is not None:
        decision_manifest["round_contract"] = {
            "file": _delivery_file_reference(Path(args.round_contract)),
            "sha256": contract_sha,
            "carryover_mode": contract["carryover_mode"],
        }
    if carryover_meta:
        decision_manifest.update(
            {
                "carryover_manifest": {
                    "file": _delivery_file_reference(
                        Path(carryover_meta["path"])
                    ),
                    "sha256": carryover_meta["sha256"],
                },
                "carryover_count": carryover_meta["carryover_count"],
                "new_batch_candidate_count": carryover_meta["new_batch_candidate_count"],
                "retry_pending_count": carryover_meta["retry_pending_count"],
                "allow_incomplete_carryover": args.allow_incomplete_carryover,
            }
        )
    if translation_summary is not None:
        decision_manifest["comment_translation"] = translation_summary
    out = {
        "generated_at": args.generated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "manifest": decision_manifest,
        "candidates": decisions,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"④ 决策完成 {len(decisions)} → {args.out}")
    for pool, n in Counter(d["final_pool"] for d in decisions).most_common():
        print(f"  {pool}: {n}")
    # 自动出交付 XLSX（一条命令直达交付表）
    if not args.no_xlsx:
        import export_v2_xlsx
        xlsx = args.xlsx or str(Path(args.out).with_name("deliverable.xlsx"))
        wb = export_v2_xlsx.build_workbook(out)
        wb.save(xlsx)
        print(f"   交付表 → {xlsx}")
    print("status:", cc.status_dist(args.batch_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
