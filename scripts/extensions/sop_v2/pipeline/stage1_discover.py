"""① 找种子：客户批准金种子回流 + Modash 带货查询 → status=seed。无需 IG 号。

用法（Chrome 需已登录 Modash 并打开标签）：
    cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.stage1_discover \
        --batch-id SKINCARE-20260716 [--queries "q1" "q2" | 缺省读 config discovery.modash_queries]

金种子 Lookalike 先导出 approved/collaborated 种子 manifest；随后由
``modash_golden_lookalikes`` 使用已验证的 marketer 同源端点生成可断点的
``golden-lookalikes-v1``，再用 ``--golden-lookalikes-json`` 回导。人工填写同一模板是
UI 契约变化时的显式 fallback；不透明 ``lookalikesToken`` 仍不能当作候选列表。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2 import discovery_strategy as strategy  # noqa: E402
from extensions.sop_v2 import round_contract as round_contract_mod  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.pipeline import barriers  # noqa: E402
from extensions.sop_v2.pipeline import modash_search as ms  # noqa: E402

ROOT = Path(__file__).resolve().parents[3].parent

# Modash AI Search 结果抽取（与 dev/modash_multi_search 同口径）
_EXTRACT = r"""() => { const out=[],seen=new Set();
  const hs=[...document.querySelectorAll('a,span,div')].filter(e=>/^@[\w.]{2,40}$/.test((e.textContent||'').trim()));
  for(const el of hs){ const h=el.textContent.trim(); if(seen.has(h))continue; let box=el;
    for(let i=0;i<8&&box.parentElement;i++){box=box.parentElement; const t=(box.textContent||'').replace(/\s+/g,' ');
      if(/Followers/i.test(t)&&t.length<260){ seen.add(h);
        out.push({handle:h, followers:(t.match(/([\d.,]+\s*[KMkm]?)\s*Followers/i)||[])[1], er:(t.match(/([\d.]+)%/)||[])[1]}); break; } } }
  return out; }"""


def discover_seeds(queries, cdp="http://127.0.0.1:9222", wait=7.0) -> list[dict]:
    """跑 Modash AI Search 多查询，累积去重，返回 [{handle,followers,er}]。只读，不消耗余额。"""
    from playwright.sync_api import sync_playwright
    out: dict[str, dict] = {}
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(cdp)
        try:
            pg = None
            for c in b.contexts:
                for p in c.pages:
                    if "modash.io" in (p.url or ""):
                        pg = p
                        break
                if pg:
                    break
            if not pg:
                raise SystemExit("未找到 Modash 标签（请在已登录的 Chrome 打开 modash.io）")
            try:
                pg.get_by_text("AI Search", exact=True).first.click(timeout=4000)
                pg.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            for q in queries:
                ed = pg.query_selector("[contenteditable='true']")
                if not ed:
                    break
                ed.click()
                pg.keyboard.press("Meta+A")
                pg.keyboard.press("Backspace")
                ed.type(q, delay=15)
                pg.wait_for_timeout(400)
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(int(wait * 1000))
                new = 0
                for cand in pg.evaluate(_EXTRACT):
                    hh = (cand.get("handle") or "").lstrip("@")
                    if hh and hh not in out:
                        out[hh] = {"handle": hh, "followers": cand.get("followers"), "er": cand.get("er")}
                        new += 1
                print(f"  '{q[:44]}' → +{new} (累计 {len(out)})", flush=True)
        finally:
            b.close()
    return list(out.values())


def _tag_source(records: list[dict], source: str) -> list[dict]:
    out = []
    for raw in records:
        rec = dict(raw)
        rec["discovered_via"] = source
        rec["discovery_sources"] = [source]
        out.append(rec)
    return out


def partition_globally_ingestible_seeds(
    records: list[dict], cache=cc
) -> tuple[list[dict], list[dict]]:
    """按全局 creator cache 分出可新入库与已存在账号。

    必须先按账号合并本轮记录，再查全局库；这样同一账号被多个来源命中时只占一个
    unique 名额，但 ``discovery_sources`` / ``golden_seed_handles`` 仍完整保留。
    """
    ingestible = []
    existing = []
    for rec in ms.merge_seed_records(records):
        handle = (rec.get("handle") or "").lstrip("@")
        if handle and cache.should_ingest_seed(handle):
            ingestible.append(rec)
        else:
            existing.append(rec)
    return ingestible, existing


def select_formal_quota(
    records: list[dict],
    target: int,
    occupied_handles: set[str],
    cache=cc,
) -> list[dict]:
    """按观察顺序选择全局可入库、且未被前序来源桶占用的精确配额。"""
    selected = []
    occupied = {str(handle).lower() for handle in occupied_handles}
    for rec in ms.merge_seed_records(records):
        handle = (rec.get("handle") or "").lstrip("@")
        key = handle.lower()
        if not key or key in occupied or not cache.should_ingest_seed(handle):
            continue
        selected.append(rec)
        occupied.add(key)
        if len(selected) >= target:
            break
    return selected


def merge_selected_source_attribution(
    selected: list[dict], observed: list[dict]
) -> list[dict]:
    """只返回已选账号，但合并这些账号在所有实际观察来源中的归因。"""
    selected_keys = {
        (rec.get("handle") or "").lstrip("@").lower() for rec in selected
    }
    observed_selected = [
        rec
        for rec in observed
        if (rec.get("handle") or "").lstrip("@").lower() in selected_keys
    ]
    return ms.merge_seed_records(selected, observed_selected)


def ingest_discovered_seeds(
    records: list[dict],
    batch_id: str,
    cache=cc,
    *,
    atomic: bool = False,
    quota_assignment: dict[str, str] | None = None,
) -> dict:
    """入库后把多源审计字段写回新 seed 的 stage_json。

    ``creator_cache.seed_handles`` 保持旧接口并负责去重/数值归一化；本层只对本次真正
    新建且仍属于当前 batch 的 seed 调用公开 ``advance(..., 'seed')`` 写审计字段，
    不改已存在候选的原批次和来源。
    """
    if atomic:
        return cache.seed_handles_atomic(
            records,
            batch_id,
            quota_assignment=quota_assignment,
        )

    records = ms.merge_seed_records(records)
    fresh = {}
    for rec in records:
        h = (rec.get("handle") or "").lstrip("@")
        if h and cache.should_ingest_seed(h):
            fresh[h.lower()] = rec
    result = cache.seed_handles(records, batch_id)
    if not fresh:
        return {**result, "audited_new": 0}

    current = {
        (cand.get("handle") or "").lower(): cand
        for cand in cache.export_candidates("seed", batch_id)
    }
    audited = 0
    for key, rec in fresh.items():
        cand = current.get(key)
        if not cand:
            continue
        cand["discovered_via"] = rec.get("discovered_via") or "unknown"
        cand["discovery_sources"] = list(rec.get("discovery_sources") or [])
        cand["golden_seed_handles"] = list(rec.get("golden_seed_handles") or [])
        cand["discovery_batch"] = batch_id
        cache.advance(cand["handle"], "seed", cand)
        audited += 1
    return {**result, "audited_new": audited}


def build_stage1_barrier_artifact(
    records: list[dict],
    *,
    batch_id: str,
    round_contract_sha256: str,
) -> dict:
    """Build the deterministic B1 artifact from the exact atomic input cohort."""
    source_counts = {key: 0 for key in barriers.SOURCE_KEYS}
    handles = []
    for rec in records:
        handle = (rec.get("handle") or "").strip().lstrip("@")
        bucket = rec.get("discovery_quota_bucket")
        if not handle or bucket not in source_counts:
            raise ValueError(
                f"formal Stage1 artifact input is missing handle/quota bucket: {rec!r}"
            )
        handles.append(handle)
        source_counts[bucket] += 1
    count = len(handles)
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "target_total": count,
        "round_contract_sha256": round_contract_sha256,
        "handle_set_sha256": barriers.handle_set_sha256(handles),
        "source_counts": source_counts,
        "ingest": {
            "new_seeds": count,
            "audited_new": count,
            "deduped": 0,
            "rejected_skipped": 0,
        },
    }


def _prepare_atomic_json(path: Path, value: dict) -> Path:
    """Durably stage JSON beside its target; publishing is a later atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    with open(temporary, "xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def _publish_atomic_json(temporary: Path, target: Path) -> None:
    """Publish a fully flushed same-directory temporary file atomically."""
    os.replace(temporary, target)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--track", choices=["paid", "gifting"], default="paid",
                    help="决定粉丝档筛选范围（paid 10K-150K / gifting 5K-50K）")
    ap.add_argument("--target", type=int, default=0,
                    help="本轮多来源合计候选目标（默认读 config）")
    ap.add_argument("--ai-search", action="store_true", help="用旧 AI Search 兜底（默认结构化搜索）")
    ap.add_argument("--queries", nargs="*")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--wait", type=float, default=7.0)
    ap.add_argument("--golden-seed-limit", type=int, default=50,
                    help="本轮最多导出的 approved/collaborated 金种子数")
    ap.add_argument("--golden-seeds-out", default=None,
                    help="金种子 Lookalike manifest；默认 data/runs/<batch>/golden_lookalike_seeds.json")
    ap.add_argument("--golden-lookalikes-json", default=None,
                    help="Modash Lookalike 结果（golden-lookalikes-v1），严格匹配 batch+种子指纹")
    ap.add_argument("--export-golden-only", action="store_true",
                    help="只导出金种子 manifest 后退出；不连 Modash、不写候选 DB")
    ap.add_argument("--require-golden-lookalikes", action="store_true",
                    help="金种子为空、结果缺失或无效时失败；避免误把通用搜索当作完整反馈飞轮")
    ap.add_argument("--round-contract", default=None,
                    help="已冻结的 round_contract.json；浏览器动作前校验 batch/track/config/code SHA")
    ap.add_argument("--require-round-contract", action="store_true",
                    help="缺少 --round-contract 时直接失败（正式批次推荐）")
    ap.add_argument(
        "--stage1-barrier-out",
        "--stage1-artifact-out",
        dest="stage1_barrier_out",
        default=None,
        help=(
            "正式 Stage1 的 B1 artifact；默认 "
            "data/runs/<batch>/stage1_barrier_artifact.json"
        ),
    )
    ap.add_argument(
        "--carryover-manifest",
        default=None,
        help="与轮次合同绑定的结转清单；unresolved/retry_only 正式批次必填",
    )
    args = ap.parse_args()
    cfg = load_config()
    disc = cfg.get("discovery", {})
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
            if args.carryover_manifest:
                round_contract_mod.validate_carryover_for_contract(
                    contract,
                    round_contract_mod.load_carryover_manifest(
                        args.carryover_manifest
                    ),
                    contract_sha256=contract_sha,
                )
            elif contract["carryover_mode"] != "new_only":
                raise round_contract_mod.RoundContractError(
                    f"{contract['carryover_mode']} 轮次必须提供 --carryover-manifest"
                )
        except (OSError, round_contract_mod.RoundContractError) as exc:
            print(f"✗ 轮次合同校验失败：{exc}")
            return 2
        print(
            f"⓪ 轮次合同通过：{args.round_contract} · "
            f"carryover={contract['carryover_mode']}",
            flush=True,
        )
    elif args.require_round_contract:
        print("✗ 正式发现要求 --round-contract")
        return 2
    elif args.carryover_manifest:
        print("✗ --carryover-manifest 必须与 --round-contract 同时使用")
        return 2
    else:
        print("⚠ 未提供轮次合同；仅兼容旧批次，不建议用于正式新批")

    # 客户门控的正向资产：golden_seeds 自身只返回 approved/collaborated。
    golden = cc.golden_seeds(limit=max(0, args.golden_seed_limit))
    manifest = ms.build_golden_seed_manifest(args.batch_id, golden)
    manifest_path = Path(args.golden_seeds_out) if args.golden_seeds_out else (
        ROOT / "data" / "runs" / args.batch_id / "golden_lookalike_seeds.json"
    )
    manifest_path = ms.write_golden_seed_manifest(manifest_path, manifest)
    print(f"①A 金种子 manifest：{len(golden)} 个 approved/collaborated → {manifest_path}")
    if args.export_golden_only:
        print("已停在 Modash Lookalike 入口（未连接 Modash、未写候选 DB）。")
        return 0

    lookalike_records = []
    lookalike_error = None
    if args.golden_lookalikes_json:
        try:
            lookalike_records = ms.load_golden_lookalikes(
                args.golden_lookalikes_json,
                batch_id=args.batch_id,
                golden_handles=golden,
            )
            if not lookalike_records:
                lookalike_error = (
                    "回导文件通过格式校验，但 results 中没有任何候选；"
                    "请勿直接回导尚未填写的导出模板"
                )
            else:
                print(f"①B 金种子 Lookalike 回导：{len(lookalike_records)} 个候选"
                      f" ← {args.golden_lookalikes_json}")
        except ms.GoldenLookalikeInputError as exc:
            lookalike_error = str(exc)
    elif golden:
        lookalike_error = (
            "尚未提供 --golden-lookalikes-json；现有 modash_raw 只有不透明 "
            "lookalikesToken，不能离线抽取候选"
        )
    else:
        lookalike_error = "当前没有 approved/collaborated 金种子"

    if lookalike_error:
        marker = "✗" if args.require_golden_lookalikes else "⚠"
        print(f"{marker} 金种子 Lookalike 通道未完成：{lookalike_error}")
        if args.require_golden_lookalikes:
            print(f"  下一步：用 modash_golden_lookalikes 读取 {manifest_path}，再回导 golden-lookalikes-v1 JSON。")
            return 2
        print("  本轮继续通用 Modash 搜索兜底；日志不会把它标成金种子回流。")

    total_target = args.target or int(disc.get("search_target", 120))
    formal_selected_records: list[dict] = []
    formal_observed_records: list[dict] = []
    quota_assignment: dict[str, str] = {}
    golden_ingestible = lookalike_records
    golden_globally_existing: list[dict] = []
    if contract is not None:
        golden_ingestible, golden_globally_existing = (
            partition_globally_ingestible_seeds(lookalike_records)
        )
    try:
        source_plan = strategy.build_source_plan(
            target=total_target,
            golden_available=len(golden_ingestible),
            source_mix_pct=(
                contract["sources"]["quota_pct"]
                if contract is not None
                else disc.get("source_mix_pct", {})
            ),
        )
    except strategy.DiscoveryStrategyError as exc:
        print(f"✗ 来源配额配置无效：{exc}")
        return 2
    golden_target = source_plan["targets"]["golden_lookalike"]
    if contract is not None:
        formal_observed_records.extend(lookalike_records)
        golden_selected = golden_ingestible[:golden_target]
        formal_selected_records.extend(golden_selected)
        for rec in golden_selected:
            quota_assignment[
                (rec.get("handle") or "").lstrip("@").lower()
            ] = "golden_lookalike"
        print(
            f"  金种子 Lookalike 全局去重：观察 {len(lookalike_records)} · "
            f"已在库 {len(golden_globally_existing)} · "
            f"按配额选择 {len(golden_selected)}/{golden_target}",
            flush=True,
        )
        lookalike_records = golden_selected
    elif len(lookalike_records) > golden_target:
        print(
            f"  金种子 Lookalike 返回 {len(lookalike_records)}，"
            f"按来源配额使用前 {golden_target} 个",
            flush=True,
        )
        lookalike_records = lookalike_records[:golden_target]
    if source_plan["golden_shortfall"]:
        fallback = source_plan["fallback_reallocated"]
        print(
            "  金种子来源缺口 "
            f"{source_plan['golden_shortfall']}：重分配到通用电商 "
            f"{fallback['generic_commerce']} / 探索 {fallback['exploration']}",
            flush=True,
        )

    generic_records = []
    generic_errors = []
    if (
        contract is not None
        and args.ai_search
        and source_plan["targets"]["exploration"] > 0
    ):
        print(
            "✗ 正式轮次合同要求 exploration 来源；--ai-search 无法证明来源桶，"
            "请使用默认结构化搜索"
        )
        return 2
    if args.ai_search:                       # 旧路兜底
        queries = args.queries or disc.get("modash_queries", [])
        print(f"①C Modash AI Search（通用兜底）：{len(queries)} 条查询", flush=True)
        generic_target = (
            source_plan["targets"]["generic_commerce"]
            if contract is not None
            else total_target - len(lookalike_records)
        )
        generic_records = _tag_source(
            discover_seeds(queries, args.cdp, args.wait),
            "modash_ai_search:generic_commerce",
        )[:generic_target]
        if contract is not None and len(generic_records) < generic_target:
            print(
                "✗ 正式轮次来源配额不足：generic_commerce "
                f"要求 {generic_target}，实际 {len(generic_records)}"
            )
            return 2
    else:                                    # 结构化搜索（默认，主力）
        t = cfg["track"][args.track]
        lo, hi = ((t["min_followers"], t["max_followers"]) if args.track == "paid"
                  else (t["standard_min"], t["priority_max"]))
        filters = {"followers": {"min": lo, "max": hi},
                   "engagementRate": {"min": disc.get("search_er_min", 0.015)}}
        # 受众/地区硬筛（逆向确认字段：filters.geo 创作者地、filters.audience.credibility 受众可信度）
        geo_ids = disc.get("search_creator_geo_ids")
        if geo_ids:
            filters["geo"] = list(geo_ids)
        cred_min = disc.get("search_audience_credibility_min")
        if cred_min:
            filters["audience"] = {"credibility": float(cred_min)}
        geo_txt = f" · 创作地{geo_ids}" if geo_ids else ""
        cred_txt = f" · 受众可信≥{cred_min}" if cred_min else ""
        try:
            source_queries = strategy.structured_queries(disc)
        except strategy.DiscoveryStrategyError as exc:
            print(f"✗ 结构化来源配置无效：{exc}")
            return 2
        print(
            f"①C Modash 多来源结构化搜索：粉丝 {lo}-{hi} · "
            f"ER≥{filters['engagementRate']['min']}{geo_txt}{cred_txt} · "
            f"总目标 {total_target}",
            flush=True,
        )
        for bucket in strategy.STRUCTURED_SOURCE_KEYS:
            bucket_target = source_plan["targets"][bucket]
            if bucket_target <= 0:
                continue
            print(f"  来源 {bucket} · 目标 {bucket_target}", flush=True)
            discover_kwargs = {
                "target": bucket_target,
                "max_pages": disc.get("search_max_pages", 80),
                "require_amazon_bio": False,
                "page_delay": disc.get("search_page_delay", 2.2),
                "cdp_url": args.cdp,
            }
            if contract is not None:
                occupied_before_bucket = frozenset(quota_assignment)

                def quota_accept(rec, occupied=occupied_before_bucket):
                    handle = (rec.get("handle") or "").lstrip("@")
                    return bool(
                        handle
                        and handle.lower() not in occupied
                        and cc.should_ingest_seed(handle)
                    )

                discover_kwargs["quota_accept"] = quota_accept
            r = ms.discover(
                source_queries[bucket], filters, **discover_kwargs
            )
            if r.get("error"):
                generic_errors.append(f"{bucket}:{r['error']}")
                continue
            bucket_seeds = r.get("seeds")
            if not isinstance(bucket_seeds, list):
                generic_errors.append(f"{bucket}:返回缺少 seeds[]")
                continue
            quota_text = (
                f" · 配额可用 {r.get('accepted', '未报告')}"
                if contract is not None
                else ""
            )
            print(
                f"    扫描 {r['raw_scanned']} · 保留 {r['kept']}"
                f"{quota_text} · 过滤 {r['filtered_out']}（品牌/私密）",
                flush=True,
            )
            bucket_records = _tag_source(
                bucket_seeds, f"modash_structured_search:{bucket}"
            )
            if contract is not None:
                formal_observed_records.extend(bucket_records)
                bucket_selected = select_formal_quota(
                    bucket_records,
                    bucket_target,
                    set(quota_assignment),
                )
                generic_records.extend(bucket_selected)
                formal_selected_records.extend(bucket_selected)
                for rec in bucket_selected:
                    quota_assignment[
                        (rec.get("handle") or "").lstrip("@").lower()
                    ] = bucket
                if len(bucket_selected) < bucket_target:
                    generic_errors.append(
                        f"{bucket}:全局去重与跨桶去重后配额不足，"
                        f"要求 {bucket_target}，实际 {len(bucket_selected)}"
                    )
            else:
                generic_records.extend(bucket_records[:bucket_target])

    if generic_errors:
        marker = "✗" if contract is not None else "⚠"
        print(
            f"{marker} 部分通用 Modash 来源不可用或不足："
            + "；".join(generic_errors)
            + "（Chrome 需已登录 Modash 并开标签）"
        )
        if contract is not None:
            return 2
    discovered_seeds = (
        merge_selected_source_attribution(
            formal_selected_records, formal_observed_records
        )
        if contract is not None
        else ms.merge_seed_records(lookalike_records, generic_records)
    )
    if not discovered_seeds:
        print("✗ 金种子回流和通用搜索均无可入库候选")
        return 1

    # 正式轮次的 unique / 来源配额必须以“本轮实际可新入库”账号为口径。
    # 先合并来源再做全局去重，避免同一账号多来源命中时丢失归因。
    seeds = discovered_seeds
    globally_existing = []
    if contract is not None:
        seeds, globally_existing = partition_globally_ingestible_seeds(
            discovered_seeds
        )
        if globally_existing:
            print(
                "  全局去重排除已在库账号 "
                f"{len(globally_existing)} 个：可新入库 {len(seeds)} / "
                f"本轮唯一发现 {len(discovered_seeds)}",
                flush=True,
            )

    source_counts = {
        "golden_lookalike": sum(
            1 for x in seeds
            if any("lookalike:golden:" in s for s in x.get("discovery_sources", []))
        ),
        "generic_commerce": sum(
            1 for x in seeds
            if any("generic_commerce" in s
                   for s in x.get("discovery_sources", []))
        ),
        "exploration": sum(
            1 for x in seeds
            if any("exploration" in s for s in x.get("discovery_sources", []))
        ),
    }
    if contract is not None:
        quota_counts = {bucket: 0 for bucket in strategy.SOURCE_KEYS}
        for rec in seeds:
            key = (rec.get("handle") or "").lstrip("@").lower()
            bucket = quota_assignment.get(key)
            if bucket in quota_counts:
                quota_counts[bucket] += 1
        shortages = {
            bucket: source_plan["targets"][bucket] - quota_counts[bucket]
            for bucket in strategy.SOURCE_KEYS
            if quota_counts[bucket] < source_plan["targets"][bucket]
        }
        if shortages or len(seeds) < total_target:
            print(
                "✗ 正式轮次来源验收失败："
                f"unique={len(seeds)}/{total_target} · shortages={shortages}"
            )
            return 2
        print(
            f"✓ 正式轮次来源验收通过：unique={len(seeds)}/{total_target} · "
            f"quota={quota_counts} · observed_attribution={source_counts}",
            flush=True,
        )

        # Freeze each candidate's one-and-only quota bucket before the atomic write.
        # discovery_sources remains multi-valued attribution and is not used for the
        # disjoint quota count.
        seeds = [dict(rec) for rec in seeds]
        for rec in seeds:
            key = (rec.get("handle") or "").lstrip("@").lower()
            rec["discovery_quota_bucket"] = quota_assignment[key]

    barrier_path = None
    barrier_temporary = None
    if contract is not None:
        barrier_path = (
            Path(args.stage1_barrier_out)
            if args.stage1_barrier_out
            else ROOT
            / "data"
            / "runs"
            / args.batch_id
            / "stage1_barrier_artifact.json"
        )
        try:
            artifact = build_stage1_barrier_artifact(
                seeds,
                batch_id=args.batch_id,
                round_contract_sha256=contract_sha,
            )
            # Stage the complete bytes before touching the DB. Most filesystem
            # failures therefore leave no cohort to recover.
            barrier_temporary = _prepare_atomic_json(barrier_path, artifact)
        except (OSError, ValueError) as exc:
            print(f"✗ Stage1 barrier artifact 预写失败（DB 未写入）：{exc}")
            return 2

    try:
        ing = ingest_discovered_seeds(
            seeds,
            args.batch_id,
            atomic=contract is not None,
            quota_assignment=quota_assignment if contract is not None else None,
        )
    except cc.AtomicSeedIngestError as exc:
        if barrier_temporary is not None:
            barrier_temporary.unlink(missing_ok=True)
        print(f"✗ 正式 Stage1 原子入库失败（整批已回滚）：{exc}")
        return 2

    if contract is not None and any(
        ing.get(key) != expected
        for key, expected in {
            "new_seeds": len(seeds),
            "audited_new": len(seeds),
            "deduped": 0,
            "rejected_skipped": 0,
        }.items()
    ):
        if barrier_temporary is not None:
            barrier_temporary.unlink(missing_ok=True)
        print(
            "✗ 正式轮次入库一致性失败："
            f"预验收可入库 {len(seeds)}，实际结果 {ing}；不得视为合同通过"
        )
        return 2

    if contract is not None:
        assert barrier_path is not None and barrier_temporary is not None
        try:
            _publish_atomic_json(barrier_temporary, barrier_path)
        except OSError as exc:
            recovery = (
                str(barrier_temporary)
                if barrier_temporary.exists()
                else str(barrier_path)
            )
            print(
                "✗ Stage1 已整批提交，但 barrier artifact 发布/落盘失败："
                f"{exc}\n  恢复路径：保留 DB cohort；确认文件 {recovery} 后，"
                f"原子移动到 {barrier_path}，再单独执行 B1 校验；禁止重跑 seed 入库。"
            )
            return 2
        try:
            b1 = barriers.evaluate_b1(
                cc.DB,
                round_contract=contract_path,
                stage1_artifact=barrier_path,
                batch_id=args.batch_id,
                round_contract_sha256=contract_sha,
            )
        except (OSError, barriers.BarrierInputError) as exc:
            print(f"✗ B1 校验无法执行：{exc}")
            return 2
        if not b1.passed:
            print(
                "✗ B1 校验失败，不得启动 Stage2："
                + "、".join(b1.failures)
            )
            return 2
        print(f"✓ B1 原子发现 barrier 通过 → {barrier_path}", flush=True)
    print(f"发现 {len(seeds)} 个候选 {source_counts} → {ing}")
    print("status:", cc.status_dist(args.batch_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
