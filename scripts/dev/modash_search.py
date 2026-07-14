#!/usr/bin/env python3
"""在登录态 yibo Chrome(CDP)里跑一次 Modash AI Search 并只读抓结果（护肤 canary）。

只填 AI Search 输入 + 回车 + 读结果卡片，绝不点 View/Save/Bulk/解锁邮箱（消耗余额）。
结果存 .secrets/modash_pool_<slug>.json（gitignore），作 search_pool_import 开发样本。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

SECRETS = Path(__file__).resolve().parents[2] / ".secrets"

EXTRACT_JS = r"""() => {
  const out = []; const seen = new Set();
  const els = Array.from(document.querySelectorAll('a, span, div'));
  const handleEls = els.filter(el => /^@[A-Za-z0-9._]{2,40}$/.test((el.textContent||'').trim()));
  const num = (s, re) => { const m=(s||'').match(re); return m?m[1]:null; };
  for (const el of handleEls) {
    const handle = el.textContent.trim();
    if (seen.has(handle)) continue;
    let box = el, row = null;
    for (let i=0;i<8&&box.parentElement;i++){box=box.parentElement;
      const t=(box.textContent||'').replace(/\s+/g,' ');
      if (/Followers|粉丝/i.test(t)&&t.length<260){row=t;break;}}
    if(!row) continue; seen.add(handle);
    const followers = num(row,/([\d.,]+\s*[KMkm万]?)\s*Followers/i)||num(row,/([\d.,]+\s*[KMkm万]?)\s*位?粉丝/);
    const er = num(row,/([\d.]+%)/);
    out.push({handle, followers, er, name: row.split('@')[0].trim().slice(0,40)});
  }
  return out;
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--wait", type=float, default=9.0, help="搜索后等待渲染秒数")
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
                sys.exit("未找到 Modash 标签")

            ed = pg.query_selector("[contenteditable='true']")
            if not ed:
                sys.exit("未找到 AI Search 输入框")
            ed.click()
            pg.keyboard.press("Meta+A")
            pg.keyboard.press("Backspace")
            ed.type(args.query, delay=25)
            time.sleep(0.5)
            pg.keyboard.press("Enter")
            print(f"已提交 AI Search：{args.query!r}，等待 {args.wait}s 渲染…")
            time.sleep(args.wait)

            cards = pg.evaluate(EXTRACT_JS)
            slug = re.sub(r"[^a-z0-9]+", "-", args.query.lower())[:30].strip("-")
            out_file = SECRETS / f"modash_pool_{slug}.json"
            rec = {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "search_mode": "ai_search",
                "query": args.query,
                "url": pg.url,
                "count": len(cards),
                "candidates": cards,
            }
            out_file.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
            print(f"抓到 {len(cards)} 个候选 → {out_file}")
            for c in cards[:30]:
                print(f"  {c['handle']:24s} | {c['followers'] or '?':>7} | ER={c['er'] or '?':>6} | {c['name'][:26]}")
            return 0
        finally:
            b.close()


if __name__ == "__main__":
    raise SystemExit(main())
