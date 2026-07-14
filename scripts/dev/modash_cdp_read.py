#!/usr/bin/env python3
"""只读连接 yibo Chrome（CDP 9222）读 Modash Discovery 当前结果（E0-4 通道验证）。

通过 Playwright connectOverCDP 连到用户正在跑的 Chrome，只操作 Modash 标签页，
只读抽取当前可见的候选卡片（name/handle/followers/ER）。绝不点击 View/Save/
Bulk save/新搜索（消耗余额），绝不动其他标签，绝不关浏览器。

用法：.venv/bin/python scripts/dev/modash_cdp_read.py [--cdp http://127.0.0.1:9222]
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--dump-text", action="store_true", help="额外打印可见文本头部辅助定位")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        try:
            # 遍历所有 context/page，只找 modash 标签，绝不动其他
            modash_page = None
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if "modash.io" in (pg.url or ""):
                        modash_page = pg
                        break
                if modash_page:
                    break
            if not modash_page:
                sys.exit("未找到 Modash 标签页（请确保 marketer.modash.io 已打开）")

            page = modash_page
            print(f"已连接 Modash 标签：{page.url}")
            print(f"标题：{page.title()}")

            # 只读抽取结果卡片：找 @handle，向上定位到含 Followers/粉丝 的最紧凑行容器
            cards = page.evaluate(
                r"""() => {
                  const out = [];
                  const seen = new Set();
                  const els = Array.from(document.querySelectorAll('a, span, div'));
                  const handleEls = els.filter(el => /^@[A-Za-z0-9._]{2,40}$/.test((el.textContent||'').trim()));
                  const num = (s, re) => { const m = (s||'').match(re); return m ? m[1] : null; };
                  for (const el of handleEls) {
                    const handle = el.textContent.trim();
                    if (seen.has(handle)) continue;
                    // 向上找：文本含 Followers/粉丝 且长度受限（行容器，非整页）
                    let box = el, row = null;
                    for (let i = 0; i < 8 && box.parentElement; i++) {
                      box = box.parentElement;
                      const t = (box.textContent||'').replace(/\s+/g,' ');
                      if (/Followers|粉丝/i.test(t) && t.length < 260) { row = t; break; }
                    }
                    if (!row) continue;
                    seen.add(handle);
                    // name 通常在 @handle 前；followers 在 "Followers" 前；ER 在 "%"
                    const followers = num(row, /([\d.,]+\s*[KMkm万]?)\s*Followers/i)
                                   || num(row, /([\d.,]+\s*[KMkm万]?)\s*位?粉丝/);
                    const er = num(row, /([\d.]+%)\s*(?:Engagement|Eng|互动)/i)
                             || num(row, /([\d.]+%)/);
                    out.push({ handle, followers, er, name: row.split('@')[0].trim().slice(0,40) });
                  }
                  return out;
                }"""
            )
            print(f"\n可见候选卡片：{len(cards)} 个（结构化）")
            for c in cards[:40]:
                print(f"  {c['handle']:22s} | followers={c['followers'] or '?':>7} | ER={c['er'] or '?':>7} | {c['name'][:28]}")

            if args.dump_text:
                b = page.query_selector("body")
                if b:
                    print("\n--- 可见文本头部 ---")
                    print((b.inner_text()[:1200]).replace("\n", " "))
            return 0
        finally:
            # 只断开连接，绝不关闭用户的浏览器
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
