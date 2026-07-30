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
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc, run_v2  # noqa: E402
from extensions.sop_v2.config import config_sha256, load_config  # noqa: E402


class CarryoverManifestError(ValueError):
    """The carry-over manifest cannot be reconciled with the creator cache."""


_CARRYOVER_RETRY_READY_STATUSES = {"collected", "rejected"}


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
    if carryover_count != len(normalized_rows):
        raise CarryoverManifestError(
            f"counts.carryover={carryover_count} 与 carryover[]={len(normalized_rows)} 不一致"
        )
    if source_count != client_final_count + carryover_count:
        raise CarryoverManifestError(
            "counts.source_candidates 必须等于 counts.client_final + counts.carryover"
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
    }


def _export_with_carryover(
    path: Path,
    batch_id: str,
    *,
    target_posts: int = 10,
    require_full_deep: bool = False,
) -> tuple[list[dict], dict]:
    meta = _load_carryover_manifest(path, batch_id)
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
    ap.add_argument("--modash-csv", default=None,
                    help="Modash Profile Report CSV：补假粉/受众/国家(解除 modash_core_missing→Include 才可能非空)")
    ap.add_argument("--manual-csv", default=None,
                    help="人工核验 CSV：raw_skin_grade/has_vo/paid_cpm(解除 raw_skin_or_vo_unverified→Include)")
    ap.add_argument("--modash-cdp", action="store_true",
                    help="驱动已登录 Modash Chrome(9222)读 show-profile 补假粉/受众/国家(每个约1 credit)")
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
    cfg = load_config()

    # 客户铁律：抓过就纳入——拉所有有数据候选（非 seed），rejected 也进（Exclude 池写明原因，不 silently drop）
    carryover_meta = None
    try:
        if args.carryover_manifest:
            cands, carryover_meta = _export_with_carryover(
                Path(args.carryover_manifest),
                args.batch_id,
                target_posts=args.deep_target_posts,
                require_full_deep=args.full_deep_all_candidates,
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
            if reasons:
                incomplete.append((cand.get("handle"), status, reasons))
        if incomplete:
            mode = (
                "strict/all-candidates"
                if args.full_deep_all_candidates
                else "strict/active"
            )
            print(
                f"✗ {mode} 完整性门禁失败：{len(incomplete)} 个候选未完成深采",
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

    if args.modash_cdp:
        from extensions.sop_v2 import gates, storefront
        from extensions.sop_v2.contracts import GateVerdict
        from extensions.sop_v2.pipeline.modash_cdp import enrich_via_cdp
        # 省 credit：只补 active 里通过"非 Modash 硬门槛"的候选(补了才够 Include)；
        # 已有 Modash 核心字段的旧 carryover、已被硬淘汰或机器 reject 的，均不重抓/不花 credit。
        cap = (
            int(cfg.get("modash_budget", {}).get("profile_per_round", 20))
            if args.modash_cap is None
            else args.modash_cap
        )
        modash_core = tuple(cfg["modash_gates"]["core_fields"])
        shortlist = [c for c in active
                     if any(c.get(field) is None for field in modash_core)
                     if gates.gate_summary(gates.evaluate_gates(c, cfg)) != GateVerdict.EXCLUDE]
        # 有橱窗优先、实算 ER 高优先（好苗子先补）
        shortlist.sort(key=lambda c: (storefront.has_storefront(c),
                                      c.get("real_er") or 0), reverse=True)
        if cap > 0:
            shortlist = shortlist[:cap]
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
        print(
            f"Modash 补数(CDP)：缺核心字段 shortlist {len(shortlist)}/{len(active)}"
            f"（完整旧数据不重抓，"
            f"{'不设上限' if cap == 0 else f'上限 {cap}'}）…"
        )
        cache_dir = str(Path(args.out).with_name("modash_raw"))  # 原始报告落盘→改解析器免重付费
        r = enrich_via_cdp(shortlist, disc.get("search_query", ""), filt, args.cdp, cache_dir=cache_dir)
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
        "full_deep_all_candidates": args.full_deep_all_candidates,
        "deep_target_posts": args.deep_target_posts,
    }
    if carryover_meta:
        decision_manifest.update(
            {
                "carryover_manifest": {
                    "file": carryover_meta["path"],
                    "sha256": carryover_meta["sha256"],
                },
                "carryover_count": carryover_meta["carryover_count"],
                "new_batch_candidate_count": carryover_meta["new_batch_candidate_count"],
                "retry_pending_count": carryover_meta["retry_pending_count"],
                "allow_incomplete_carryover": args.allow_incomplete_carryover,
            }
        )
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
