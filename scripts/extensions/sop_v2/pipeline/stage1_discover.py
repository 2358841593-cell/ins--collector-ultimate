"""① 找种子：客户批准金种子回流 + Modash 带货查询 → status=seed。无需 IG 号。

用法（Chrome 需已登录 Modash 并打开标签）：
    cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.stage1_discover \
        --batch-id SKINCARE-20260716 [--queries "q1" "q2" | 缺省读 config discovery.modash_queries]

金种子 Lookalike 不假装全自动：现有 show-profile 缓存只有不透明 lookalikesToken，
没有可离线抽取的候选，仓库也没有验证过其 app 内部端点。因此先导出 approved/collaborated
种子 manifest，人工在 Modash 执行 Lookalike 后，用 ``--golden-lookalikes-json`` 回导。
没有回导文件时会明确提示该通道仍阻塞，并保留原结构化搜索作为兜底。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
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


def ingest_discovered_seeds(records: list[dict], batch_id: str, cache=cc) -> dict:
    """入库后把多源审计字段写回新 seed 的 stage_json。

    ``creator_cache.seed_handles`` 保持旧接口并负责去重/数值归一化；本层只对本次真正
    新建且仍属于当前 batch 的 seed 调用公开 ``advance(..., 'seed')`` 写审计字段，
    不改已存在候选的原批次和来源。
    """
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--track", choices=["paid", "gifting"], default="paid",
                    help="决定粉丝档筛选范围（paid 10K-150K / gifting 5K-50K）")
    ap.add_argument("--target", type=int, default=0, help="累积多少 seed（默认读 config）")
    ap.add_argument("--ai-search", action="store_true", help="用旧 AI Search 兜底（默认结构化搜索）")
    ap.add_argument("--queries", nargs="*")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--wait", type=float, default=7.0)
    ap.add_argument("--golden-seed-limit", type=int, default=50,
                    help="本轮最多导出的 approved/collaborated 金种子数")
    ap.add_argument("--golden-seeds-out", default=None,
                    help="金种子人工 Lookalike manifest；默认 data/runs/<batch>/golden_lookalike_seeds.json")
    ap.add_argument("--golden-lookalikes-json", default=None,
                    help="人工 Modash Lookalike 结果（golden-lookalikes-v1），严格匹配 batch+种子指纹")
    ap.add_argument("--export-golden-only", action="store_true",
                    help="只导出金种子 manifest 后退出；不连 Modash、不写候选 DB")
    ap.add_argument("--require-golden-lookalikes", action="store_true",
                    help="金种子为空、结果缺失或无效时失败；避免误把通用搜索当作完整反馈飞轮")
    args = ap.parse_args()
    cfg = load_config()
    disc = cfg.get("discovery", {})

    # 客户门控的正向资产：golden_seeds 自身只返回 approved/collaborated。
    golden = cc.golden_seeds(limit=max(0, args.golden_seed_limit))
    manifest = ms.build_golden_seed_manifest(args.batch_id, golden)
    manifest_path = Path(args.golden_seeds_out) if args.golden_seeds_out else (
        ROOT / "data" / "runs" / args.batch_id / "golden_lookalike_seeds.json"
    )
    manifest_path = ms.write_golden_seed_manifest(manifest_path, manifest)
    print(f"①A 金种子 manifest：{len(golden)} 个 approved/collaborated → {manifest_path}")
    if args.export_golden_only:
        print("已停在人工 Modash Lookalike 入口（未连接 Modash、未写候选 DB）。")
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
            print(f"  人工入口：先按 {manifest_path} 在 Modash 扩池，再回导 golden-lookalikes-v1 JSON。")
            return 2
        print("  本轮继续通用 Modash 搜索兜底；日志不会把它标成金种子回流。")

    generic_records = []
    generic_error = None
    if args.ai_search:                       # 旧路兜底
        queries = args.queries or disc.get("modash_queries", [])
        print(f"①C Modash AI Search（通用兜底）：{len(queries)} 条查询", flush=True)
        generic_records = _tag_source(
            discover_seeds(queries, args.cdp, args.wait),
            "modash_ai_search",
        )
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
        target = args.target or disc.get("search_target", 120)
        geo_txt = f" · 创作地{geo_ids}" if geo_ids else ""
        cred_txt = f" · 受众可信≥{cred_min}" if cred_min else ""
        print(f"①C Modash 结构化搜索（通用兜底）：粉丝 {lo}-{hi} · ER≥{filters['engagementRate']['min']}"
              f"{geo_txt}{cred_txt} · 目标 {target}", flush=True)
        r = ms.discover(disc.get("search_query", ""), filters, target=target,
                        max_pages=disc.get("search_max_pages", 80),
                        require_amazon_bio=disc.get("search_require_amazon_bio", False),
                        page_delay=disc.get("search_page_delay", 2.2), cdp_url=args.cdp)
        if r.get("error"):
            generic_error = r["error"]
        else:
            print(f"  扫描 {r['raw_scanned']} · 保留 {r['kept']} · "
                  f"过滤 {r['filtered_out']}（品牌/私密）", flush=True)
            generic_records = _tag_source(r["seeds"], "modash_structured_search")

    if generic_error:
        print(f"⚠ 通用 Modash 搜索不可用：{generic_error}（Chrome 需已登录 Modash 并开标签）")
    seeds = ms.merge_seed_records(lookalike_records, generic_records)
    if not seeds:
        print("✗ 金种子回流和通用搜索均无可入库候选")
        return 1

    ing = ingest_discovered_seeds(seeds, args.batch_id)
    source_counts = {
        "golden_lookalike": sum(
            1 for x in seeds
            if any("lookalike:golden:" in s for s in x.get("discovery_sources", []))
        ),
        "generic": sum(
            1 for x in seeds
            if any(s in ("modash_structured_search", "modash_ai_search")
                   for s in x.get("discovery_sources", []))
        ),
    }
    print(f"发现 {len(seeds)} 个候选 {source_counts} → {ing}")
    print("status:", cc.status_dist(args.batch_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
