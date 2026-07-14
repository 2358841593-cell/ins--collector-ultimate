#!/usr/bin/env python3
"""登录态 Chrome profile 池健康分诊（R0-1，浏览器唯一通道）。

对 .secrets/accounts_raw.txt 中每个账号：把 sessionid cookie 注入其独立 Chrome
profile，只读打开 instagram.com 判断登录态（LOGGED_IN / LOGGED_OUT / CHALLENGE /
UNKNOWN）。不做密码/TOTP 登录、不发写操作、不回显凭证；账号间带随机停顿拟人。

输出：终端健康表 + JSON 报告写 .secrets/pool_health.json（gitignore）。

用法：
    .venv/bin/python scripts/pool_health.py                 # 全部账号，有头
    .venv/bin/python scripts/pool_health.py --headless      # 无头
    .venv/bin/python scripts/pool_health.py --username X    # 单个
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".secrets"
ACCOUNTS = SECRETS / "accounts_raw.txt"
PROFILE_BASE = SECRETS / "chrome-instagram-profiles"
REPORT = SECRETS / "pool_health.json"


def parse_accounts() -> list[dict]:
    if not ACCOUNTS.exists():
        sys.exit(f"未找到账号文件：{ACCOUNTS}")
    out = []
    for raw in ACCOUNTS.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        p = line.split("|")
        if len(p) < 4:
            continue
        ds_user_id = sessionid = ""
        for kv in p[3].split(";"):
            kv = kv.strip()
            if kv.startswith("ds_user_id="):
                ds_user_id = kv[len("ds_user_id="):]
            elif kv.startswith("sessionid="):
                sessionid = kv[len("sessionid="):]
        out.append({
            "username": p[0].strip(),
            "ds_user_id": ds_user_id,
            "sessionid": unquote(sessionid),
        })
    return out


def check_one(pw, acct: dict, headless: bool) -> dict:
    username = acct["username"]
    result = {"username": username, "state": "UNKNOWN", "url": "", "note": ""}
    if not acct["sessionid"] or not acct["ds_user_id"]:
        result["state"] = "NO_COOKIE"
        return result

    profile_dir = PROFILE_BASE / username
    profile_dir.mkdir(parents=True, exist_ok=True)
    cookies = [
        {"name": "sessionid", "value": acct["sessionid"], "domain": ".instagram.com",
         "path": "/", "httpOnly": True, "secure": True},
        {"name": "ds_user_id", "value": acct["ds_user_id"], "domain": ".instagram.com",
         "path": "/", "httpOnly": False, "secure": True},
    ]
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir), channel="chrome", headless=headless,
        args=["--no-first-run", "--no-default-browser-check"],
        viewport={"width": 1280, "height": 900},
    )
    try:
        ctx.add_cookies(cookies)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        url = page.url
        result["url"] = url
        body = ""
        b = page.query_selector("body")
        if b:
            body = (b.inner_text()[:400] or "").replace("\n", " ")

        if "/challenge" in url or "/checkpoint" in url or "suspended" in body.lower():
            result["state"] = "CHALLENGE"
        elif "/accounts/login" in url or "Log into Instagram" in body:
            result["state"] = "LOGGED_OUT"
        else:
            markers = any(page.query_selector(sel) is not None for sel in
                          ['svg[aria-label="Home"]', 'svg[aria-label="首页"]',
                           'a[href*="/direct/"]', 'svg[aria-label="New post"]',
                           'svg[aria-label="新帖子"]'])
            result["state"] = "LOGGED_IN" if markers else "UNKNOWN"
            if not markers:
                result["note"] = "首页加载但未见登录态标志（可能渲染慢/需复查）"
        return result
    except Exception as e:  # noqa: BLE001
        result["state"] = "ERROR"
        result["note"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--username", help="只查单个账号")
    args = ap.parse_args()

    accts = parse_accounts()
    if args.username:
        accts = [a for a in accts if a["username"] == args.username]
        if not accts:
            sys.exit(f"未找到账号 {args.username}")

    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as pw:
        for i, acct in enumerate(accts):
            r = check_one(pw, acct, args.headless)
            results.append(r)
            print(f"  {r['state']:11s} @{r['username']}"
                  + (f"  ({r['note']})" if r["note"] else ""))
            if i < len(accts) - 1:
                time.sleep(random.uniform(4.0, 9.0))  # 拟人停顿

    healthy = sum(1 for r in results if r["state"] == "LOGGED_IN")
    summary = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total": len(results), "healthy": healthy,
        "by_state": {s: sum(1 for r in results if r["state"] == s)
                     for s in sorted({r["state"] for r in results})},
        "accounts": results,
    }
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n健康 profile: {healthy}/{len(results)}  |  报告: {REPORT}")
    return 0 if healthy > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
