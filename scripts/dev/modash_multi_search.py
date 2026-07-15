#!/usr/bin/env python3
"""Modash 多查询 AI Search 收集器（绕开虚拟列表滚动问题）。

背景（实测结论）：Modash AI Search 只渲染约 6 个预览结果、不懒加载；Search 标签的
虚拟列表 + React 筛选在实时会话里程序化滚动/填值极不稳。可靠做法是**跑多个宽查询**，
每个取 6 个相关预览，累积去重——比硬滚一个列表稳。

用法：
    .venv/bin/python scripts/dev/modash_multi_search.py --queries q1 q2 q3 --out pool.json
只读：只填 AI Search 输入 + 回车 + 读结果，不点 View/Save/Bulk（不消耗余额）。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

SECRETS = Path(__file__).resolve().parents[2] / ".secrets"

EXTRACT = r"""() => { const out=[],seen=new Set();
  const hs=[...document.querySelectorAll('a,span,div')].filter(e=>/^@[\w.]{2,40}$/.test((e.textContent||'').trim()));
  for(const el of hs){ const h=el.textContent.trim(); if(seen.has(h))continue; let box=el;
    for(let i=0;i<8&&box.parentElement;i++){box=box.parentElement; const t=(box.textContent||'').replace(/\s+/g,' ');
      if(/Followers/i.test(t)&&t.length<260){ seen.add(h);
        out.push({handle:h, followers:(t.match(/([\d.,]+\s*[KMkm]?)\s*Followers/i)||[])[1], er:(t.match(/([\d.]+)%/)||[])[1]}); break; } } }
  return out; }"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--wait", type=float, default=7.0)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(args.cdp)
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
                raise SystemExit("未找到 Modash 标签")
            try:
                pg.get_by_text("AI Search", exact=True).first.click(timeout=4000)
                pg.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            allc = {}
            for q in args.queries:
                ed = pg.query_selector("[contenteditable='true']")
                if not ed:
                    break
                ed.click()
                pg.keyboard.press("Meta+A")
                pg.keyboard.press("Backspace")
                ed.type(q, delay=15)
                pg.wait_for_timeout(400)
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(int(args.wait * 1000))
                new = 0
                for c in pg.evaluate(EXTRACT):
                    if c["handle"] not in allc:
                        allc[c["handle"]] = c
                        new += 1
                print(f"  '{q[:44]}' → +{new} (累计 {len(allc)})")
            cands = [{"handle": k.lstrip("@"), "followers": v.get("followers"), "er": v.get("er")}
                     for k, v in allc.items()]
            Path(args.out).write_text(json.dumps(
                {"search_mode": "ai_search_multi",
                 "queries": args.queries, "candidates": cands}, ensure_ascii=False, indent=2))
            print(f"总计 {len(cands)} 个 unique 候选 → {args.out}")
            return 0
        finally:
            b.close()


if __name__ == "__main__":
    raise SystemExit(main())
