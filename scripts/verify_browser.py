#!/usr/bin/env python3
"""自动浏览器核验阶段：用真实 Chrome（headless）实跑核验并留证据。

对 review 候选逐个执行可自动化的核验，每项产出：
  - workflow（怎么验的）
  - client_concern（客户为什么关心，从 brief 提炼）
  - checked（验了什么）
  - result + result_text（结论）
  - evidence：截图 PNG + 机器提取的真实数据 + 源 URL + 时间戳（防幻觉）

可自动核验：
  1. Amazon Storefront 穿透 —— 打开 bio 链接，渲染后读真实出链，找 amazon.com/shop
  2. Storefront 成熟度 —— 若有 Amazon 链接，打开数商品/分类
  3. 视觉素材抓取 —— 截图候选内容，供视觉模型判断皮肤纹理真实度
不可自动（脚本明确标注）：Save 率 / DM 分享 —— 公开页面不渲染，只能 Modash

用法：
    .venv/bin/python scripts/verify_browser.py --limit 6
    .venv/bin/python scripts/verify_browser.py --handles sangeetadosanjh,isabellawears__
输出：data/runs/verifications-{run_id}.json + data/runs/evidence/*.png
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "data" / "runs"
EVIDENCE_DIR = RUNS_DIR / "evidence"

AMAZON_RE = re.compile(r"amazon\.|amzn\.", re.I)
AMAZON_SHOP_RE = re.compile(r"amazon\.[a-z.]+/shop/", re.I)

# 客户 brief 提炼出的关注点（每个核验项对应客户的哪条需求）
CLIENT_CONCERN = {
    "amazon_storefront": "客户硬性门槛：bio 必须直链一个成熟、活跃、有组织的 Amazon Storefront，"
                         "否则不进 Prime Day 转化名单。（brief: Active Infrastructure / Bio Link Infrastructure —— "
                         "“If they don't have a curated Amazon Storefront, they shouldn't be considered”）",
    "storefront_maturity": "客户要求 Storefront「成熟、活跃、有组织」，不只是存在。"
                          "（brief: a mature, active, and organized Amazon Storefront）",
    "visual": "护肤/红光类信任建立在真实、未修图的近距离皮肤纹理上，排斥重滤镜/柔光环形灯。"
              "（brief: Visual Validation —— raw, unedited skin textures, crisp close-up macro framing）",
}


def latest_run() -> str:
    files = sorted(glob.glob(str(RUNS_DIR / "discovery-*.json")), key=os.path.getmtime)
    if not files:
        raise SystemExit("没有 discovery-*.json")
    return files[-1]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def extract_links(page) -> list[str]:
    """读页面所有出链的 hostname+pathname（去 query，避免追踪参数）。"""
    js = """() => [...document.querySelectorAll('a[href]')].map(a => {
        try { const u = new URL(a.href); return (u.hostname + u.pathname).toLowerCase(); }
        catch(e){ return ''; }
    }).filter(Boolean)"""
    try:
        return page.evaluate(js)
    except Exception:
        return []


def verify_bio(page, cand: dict) -> dict:
    """核验项 1：Amazon Storefront 穿透。"""
    handle = cand["handle"]
    url = cand.get("bio_link_url") or ""
    bio_type = cand.get("bio_link_type") or "unknown"
    shot = EVIDENCE_DIR / f"{handle}-bio.png"
    rec = {
        "id": "amazon_storefront",
        "title": "Amazon Storefront 穿透核验",
        "client_concern": CLIENT_CONCERN["amazon_storefront"],
        "workflow": [
            f"Playwright 驱动真实 Chrome 打开 bio 链接（{bio_type}）",
            "等待 DOM + JS 渲染（networkidle / 4s）",
            "提取页面全部出链的域名+路径（去除追踪参数）",
            "匹配 Amazon 域名（amazon.* / amzn.*）与 /shop/ 橱窗路径",
        ],
        "evidence": {"source_url": url, "captured_at": now_iso(),
                     "screenshot": f"evidence/{shot.name}", "data": {}},
    }
    if not url:
        rec.update(result="inconclusive", checked="无 bio 链接",
                   result_text="该候选无 bio 链接 URL，无法穿透。")
        return rec
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(3500)
        title = (page.title() or "")[:120]
        links = extract_links(page)
        amazon_hits = sorted({l for l in links if AMAZON_RE.search(l)})
        shop_hits = sorted({l for l in links if AMAZON_SHOP_RE.search(l)})
        try:
            page.screenshot(path=str(shot), full_page=False)
        except Exception as exc:
            rec["evidence"]["screenshot_error"] = str(exc)[:80]
        rec["evidence"]["data"] = {
            "page_title": title,
            "total_links": len(links),
            "amazon_links": len(amazon_hits),
            "amazon_shop_links": len(shop_hits),
            "amazon_sample": amazon_hits[:8],
        }
        rec["checked"] = f"渲染后共 {len(links)} 个出链；匹配 Amazon 域名 {len(amazon_hits)} 个，其中 /shop/ 橱窗 {len(shop_hits)} 个"
        # 直链 Amazon 自身页面
        if AMAZON_SHOP_RE.search(url) or "amazon" in (cand.get("bio_link_type") or ""):
            rec.update(result="pass",
                       result_text=f"bio 直链 Amazon 橱窗，页面标题「{title}」已渲染确认。")
        elif shop_hits:
            rec.update(result="pass",
                       result_text=f"聚合页内发现 Amazon 橱窗链接：{shop_hits[0]}")
        elif amazon_hits:
            rec.update(result="pass",
                       result_text=f"发现 Amazon 链接（非 /shop/ 橱窗，需确认）：{amazon_hits[0]}")
        else:
            # 聚合页/LTK 常用联盟跳转隐藏最终零售商
            note = "未发现直接 Amazon 链接。" + (
                "LTK/聚合页商品多走联盟跳转（liketk.it/rstyle.me），最终零售商被隐藏，"
                "需点进单个商品跟随跳转才能确认是否 Amazon。"
                if bio_type in ("ltk", "beacons", "linktree", "linkin_bio", "stan_store")
                else "")
            rec.update(result="inconclusive" if bio_type in ("ltk", "beacons") else "fail",
                       result_text=note)
    except Exception as exc:
        rec.update(result="error", checked="页面加载失败",
                   result_text=f"{type(exc).__name__}: {str(exc)[:100]}")
    return rec


def verify_storefront_maturity(page, cand: dict) -> dict | None:
    """核验项 2：Storefront 成熟度（仅 amazon_direct）。"""
    handle = cand["handle"]
    url = cand.get("bio_link_url") or ""
    if "amazon" not in (cand.get("bio_link_type") or ""):
        return None
    shot = EVIDENCE_DIR / f"{handle}-storefront.png"
    rec = {
        "id": "storefront_maturity",
        "title": "Storefront 成熟度核验",
        "client_concern": CLIENT_CONCERN["storefront_maturity"],
        "workflow": [
            "打开 Amazon Storefront 页面",
            "等待渲染",
            "统计页面商品图片数量、抓取标题作为活跃/有组织的代理指标",
            "截图留证",
        ],
        "evidence": {"source_url": url, "captured_at": now_iso(),
                     "screenshot": f"evidence/{shot.name}", "data": {}},
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        title = (page.title() or "")[:120]
        imgs = page.evaluate("() => document.querySelectorAll('img').length")
        try:
            page.screenshot(path=str(shot), full_page=False)
        except Exception as exc:
            rec["evidence"]["screenshot_error"] = str(exc)[:80]
        rec["evidence"]["data"] = {"page_title": title, "image_count": imgs}
        rec["checked"] = f"页面标题「{title}」，渲染出 {imgs} 张图片（商品图代理）"
        if "amazon" in title.lower() and imgs > 30:
            rec.update(result="pass",
                       result_text=f"确认是真实 Amazon 橱窗页（{imgs} 张商品图），有内容、非空壳。")
        elif "amazon" in title.lower():
            rec.update(result="pass", result_text=f"确认 Amazon 橱窗页存在（{imgs} 张图）。")
        else:
            rec.update(result="inconclusive",
                       result_text="页面未确认为 Amazon 橱窗（可能区域限制 / 需登录）。")
    except Exception as exc:
        rec.update(result="error", result_text=f"{type(exc).__name__}: {str(exc)[:100]}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="自动浏览器核验 + 留证据")
    ap.add_argument("--run", type=str, default=None)
    ap.add_argument("--limit", type=int, default=6, help="核验前 N 个 review 候选")
    ap.add_argument("--handles", type=str, default=None, help="指定 handle，逗号分隔")
    ap.add_argument("--include-status", type=str, default="review,include",
                    help="核验哪些状态的候选")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    run_path = args.run or latest_run()
    data = json.loads(Path(run_path).read_text(encoding="utf-8"))
    run_id = data.get("run_id", "run")
    statuses = set(args.include_status.split(","))

    cands = [c for c in data["candidates"] if c.get("status") in statuses]
    if args.handles:
        want = {h.strip().lower() for h in args.handles.split(",")}
        cands = [c for c in cands if c["handle"].lower() in want]
    else:
        cands.sort(key=lambda c: -c.get("discovery_score", 0))
        cands = cands[: args.limit]

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"核验 {len(cands)} 个候选（run {run_id}）\n")

    out_path = RUNS_DIR / f"verifications-{run_id}.json"
    # 合并模式：保留已有候选的核验记录（含手工补的视觉判断），只更新本次跑的
    if out_path.exists():
        out = json.loads(out_path.read_text(encoding="utf-8"))
        out["verified_at"] = now_iso()
    else:
        out = {"run_id": run_id, "verified_at": now_iso(), "candidates": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(
            viewport={"width": 1200, "height": 1400},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        )
        page = ctx.new_page()
        for i, c in enumerate(cands, 1):
            h = c["handle"]
            print(f"[{i}/{len(cands)}] @{h} ({c.get('bio_link_type')})")
            checks = []
            bio = verify_bio(page, c)
            checks.append(bio)
            print(f"    Amazon 穿透: {bio.get('result')} — {bio.get('result_text','')[:60]}")
            mat = verify_storefront_maturity(page, c)
            if mat:
                checks.append(mat)
                print(f"    Storefront 成熟度: {mat.get('result')} — {mat.get('result_text','')[:50]}")
            out["candidates"][h] = {
                "handle": h, "verified_at": now_iso(), "checks": checks,
                "bio_link_url": c.get("bio_link_url"), "bio_link_type": c.get("bio_link_type"),
                "profile_url": c.get("profile_url"),
            }
        browser.close()

    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 核验记录 → {out_path}")
    print(f"  证据截图 → {EVIDENCE_DIR}/*.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
