"""① 找种子：Modash 带货查询（CDP 驱动已登录 Chrome）→ status=seed。无需 IG 号。

用法（Chrome 需已登录 Modash 并打开标签）：
    cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.stage1_discover \
        --batch-id SKINCARE-20260716 [--queries "q1" "q2" | 缺省读 config discovery.modash_queries]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402

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
    args = ap.parse_args()
    cfg = load_config()
    disc = cfg.get("discovery", {})

    if args.ai_search:                       # 旧路兜底
        queries = args.queries or disc.get("modash_queries", [])
        print(f"① Modash AI Search（兜底）：{len(queries)} 条查询", flush=True)
        seeds = discover_seeds(queries, args.cdp, args.wait)
    else:                                    # 结构化搜索（默认，主力）
        from extensions.sop_v2.pipeline.modash_search import discover
        t = cfg["track"][args.track]
        lo, hi = ((t["min_followers"], t["max_followers"]) if args.track == "paid"
                  else (t["standard_min"], t["priority_max"]))
        filters = {"followers": {"min": lo, "max": hi},
                   "engagementRate": {"min": disc.get("search_er_min", 0.015)}}
        target = args.target or disc.get("search_target", 120)
        print(f"① Modash 结构化搜索：粉丝 {lo}-{hi} · ER≥{filters['engagementRate']['min']} · 目标 {target}", flush=True)
        r = discover(disc.get("search_query", ""), filters, target=target,
                     max_pages=disc.get("search_max_pages", 80),
                     require_amazon_bio=disc.get("search_require_amazon_bio", False),
                     page_delay=disc.get("search_page_delay", 2.2), cdp_url=args.cdp)
        if r.get("error"):
            print(f"✗ {r['error']}（Chrome 需已登录 Modash 并开标签）"); return 1
        print(f"  扫描 {r['raw_scanned']} · 保留 {r['kept']} · 过滤 {r['filtered_out']}（品牌/私密）", flush=True)
        seeds = r["seeds"]

    ing = cc.seed_handles(seeds, args.batch_id)
    print(f"发现 {len(seeds)} 个候选 → {ing}")
    print("status:", cc.status_dist(args.batch_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
