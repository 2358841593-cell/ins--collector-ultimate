"""Storefront 浏览器穿透（P1-2，SCORE-D1/D2/D3）。

对候选 bio 里的聚合页（Linktree/Beacons/LTK/ShopMy/Stan 等）用真浏览器渲染后读真实
出链，判断是否穿透到 Amazon Storefront：
  - 命中 amazon.com/shop 或 /storefront → confirmed_yes（+ 链接 + 证据）
  - 打开成功但无 Amazon 出链 → confirmed_no
  - 打开失败/需人工点击（LTK 联盟跳转常无法自动确认）→ unknown（进 Review 待人工）
留证：source_url + captured_at + 命中的出链样本 + 截图路径。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

SECRETS = Path(__file__).resolve().parents[2].parent / ".secrets"
SHOT_DIR = SECRETS / "storefront-shots"

AMAZON_SHOP = re.compile(r"amazon\.[a-z.]+/(shop|storefront)", re.I)
AMAZON_ANY = re.compile(r"(amazon\.[a-z.]+|amzn\.to)", re.I)
AGG_HOSTS = ("linktr.ee", "beacons.ai", "beacons.page", "liketoknow.it", "shopltk.com",
             "shopmy.us", "stan.store", "komi.io", "linkin.bio", "milkshake", "flowpage",
             "snipfeed", "withkoji")


def _is_aggregator(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in AGG_HOSTS)


def penetrate(url: str, headless: bool = True, save_shot: bool = True) -> dict:
    """打开单个 bio 链接，读出链判断 Storefront。返回决策 + 证据。"""
    from playwright.sync_api import sync_playwright

    out = {"source_url": url, "status": "unknown", "amazon_link": None,
           "amazon_links": [], "checked": 0, "note": "", "screenshot": None}
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=headless,
                                     args=["--no-first-run", "--no-default-browser-check"])
        ctx = browser.new_context(viewport={"width": 1200, "height": 1400})
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(2500)

            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            out["checked"] = len(hrefs)
            amazon_shop = [h for h in hrefs if AMAZON_SHOP.search(h)]
            amazon_any = [h for h in hrefs if AMAZON_ANY.search(h)]

            if amazon_shop:
                out["status"] = "confirmed_yes"
                out["amazon_link"] = amazon_shop[0]
                out["amazon_links"] = amazon_shop[:5]
            elif amazon_any:
                # 有 amazon 链接但非 /shop（可能是单品联盟链）→ 记为 yes（有 Amazon 导购路径）
                out["status"] = "confirmed_yes"
                out["amazon_link"] = amazon_any[0]
                out["amazon_links"] = amazon_any[:5]
                out["note"] = "amazon_link_non_storefront_path"
            elif "liketoknow" in url.lower() or "shopltk" in url.lower():
                out["status"] = "unknown"
                out["note"] = "ltk_affiliate_needs_manual_click"
            else:
                out["status"] = "confirmed_no"
                out["note"] = "opened_no_amazon_outlink"

            if save_shot:
                slug = re.sub(r"[^a-z0-9]+", "-", url.lower())[:40].strip("-")
                shot = SHOT_DIR / f"{slug}.png"
                page.screenshot(path=str(shot))
                out["screenshot"] = str(shot)
            out["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        except Exception as e:  # noqa: BLE001
            out["status"] = "unknown"
            out["note"] = f"open_failed:{type(e).__name__}"
        finally:
            ctx.close()
            browser.close()
    return out


def resolve_candidate(cand: dict, headless: bool = True) -> dict:
    """对一个候选：若 storefront 未确认且有聚合页链接，穿透解析并回写。"""
    if cand.get("storefront_status") == "confirmed_yes":
        return {"status": "confirmed_yes", "note": "amazon_direct_in_bio"}
    links = list(cand.get("bio_links") or [])
    ext = cand.get("external_url")
    if ext and ext not in links:
        links.append(ext)
    agg = [l for l in links if _is_aggregator(l)]
    targets = agg or links
    for url in targets[:3]:
        res = penetrate(url, headless=headless)
        if res["status"] == "confirmed_yes":
            cand["storefront_status"] = "confirmed_yes"
            cand["amazon_storefront_link"] = res["amazon_link"]
            cand["storefront_evidence"] = res
            return res
        last = res
    # 全部未穿透到 Amazon
    if targets:
        # 若至少一个聚合页成功打开且无 Amazon → confirmed_no；否则 unknown
        cand["storefront_status"] = "confirmed_no" if last.get("status") == "confirmed_no" else "unknown"
        cand["storefront_evidence"] = last
        return last
    cand["storefront_status"] = "confirmed_no"
    return {"status": "confirmed_no", "note": "no_bio_links"}
