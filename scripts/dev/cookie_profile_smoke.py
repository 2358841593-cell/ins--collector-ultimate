#!/usr/bin/env python3
"""从 sessionid cookie 建立登录态 Chrome profile 并冒烟验证（浏览器唯一通道 R0-3/B0-2 起点）。

读取本机 .secrets/accounts_raw.txt（格式：user|pw|totp|ds_user_id=X;sessionid=Y|email|ts），
用 Playwright 持久化上下文（真实 Chrome 渠道）为指定账号建独立 profile，注入 cookie，
打开 instagram.com 判断是否登录态，截图存证到 .secrets（gitignore）。

不做密码/TOTP 登录、不发任何写操作、不回显凭证。仅只读加载首页判断登录态。

用法：
    .venv/bin/python scripts/dev/cookie_profile_smoke.py --index 0 [--headless]
    .venv/bin/python scripts/dev/cookie_profile_smoke.py --username gimdohyeonay699
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
SECRETS = ROOT / ".secrets"
ACCOUNTS = SECRETS / "accounts_raw.txt"
PROFILE_BASE = SECRETS / "chrome-instagram-profiles"
SHOT_DIR = SECRETS / "smoke-shots"


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
        cookie_blob = p[3]
        ds_user_id = sessionid = ""
        for kv in cookie_blob.split(";"):
            kv = kv.strip()
            if kv.startswith("ds_user_id="):
                ds_user_id = kv[len("ds_user_id="):]
            elif kv.startswith("sessionid="):
                sessionid = kv[len("sessionid="):]
        out.append({
            "username": p[0].strip(),
            "ds_user_id": ds_user_id,
            # cookie 值在源文件里是 URL 编码的（%3A 等），注入时用解码后的原值
            "sessionid": unquote(sessionid),
        })
    return out


def run(acct: dict, headless: bool) -> int:
    from playwright.sync_api import sync_playwright

    username = acct["username"]
    if not acct["sessionid"] or not acct["ds_user_id"]:
        sys.exit(f"[{username}] 缺 sessionid/ds_user_id，无法建登录态")

    profile_dir = PROFILE_BASE / username
    profile_dir.mkdir(parents=True, exist_ok=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    shot = SHOT_DIR / f"{username}.png"

    cookies = [
        {"name": "sessionid", "value": acct["sessionid"], "domain": ".instagram.com",
         "path": "/", "httpOnly": True, "secure": True},
        {"name": "ds_user_id", "value": acct["ds_user_id"], "domain": ".instagram.com",
         "path": "/", "httpOnly": False, "secure": True},
    ]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=headless,
            args=["--no-first-run", "--no-default-browser-check"],
            viewport={"width": 1280, "height": 900},
        )
        try:
            ctx.add_cookies(cookies)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)  # 让首页渲染
            url = page.url
            body = (page.inner_text("body")[:400] if page.query_selector("body") else "").replace("\n", " ")
            page.screenshot(path=str(shot), full_page=False)

            logged_out = "/accounts/login" in url or "Log in" in body[:120] or "Log into Instagram" in body
            # 登录态标志：能看到自己的 profile 入口 / 首页 feed（未被踢到登录墙）
            logged_in_markers = any(
                page.query_selector(sel) is not None
                for sel in ['a[href="/"] svg[aria-label="Home"]',
                            'svg[aria-label="Home"]',
                            'a[href*="/direct/"]',
                            'span:has-text("For you")']
            )
            state = "LOGGED_IN" if (logged_in_markers and not logged_out) else (
                "LOGGED_OUT" if logged_out else "UNKNOWN")
            print(f"[{username}] home_state={state} url={url}")

            # B0-2 验证：读自己的 profile，抓 og:description（含粉丝/帖数）证明可结构化采集
            prof_stat = ""
            try:
                page.goto(f"https://www.instagram.com/{username}/",
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                meta = page.query_selector('meta[property="og:description"]')
                prof_stat = meta.get_attribute("content") if meta else ""
                title = page.title()
                on_login = "/accounts/login" in page.url
                page.screenshot(path=str(shot), full_page=False)
                print(f"[{username}] profile_url={page.url} on_login_wall={on_login}")
                print(f"[{username}] profile_title={title}")
                print(f"[{username}] og_description={prof_stat[:200]}")
            except Exception as e:  # noqa: BLE001
                print(f"[{username}] profile_read_error={type(e).__name__}: {e}")

            print(f"[{username}] screenshot={shot}")
            ok = state == "LOGGED_IN" and bool(prof_stat)
            return 0 if ok else 1
        finally:
            ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--index", type=int, default=0, help="账号序号（默认 0，第一个）")
    g.add_argument("--username", help="指定账号 username")
    ap.add_argument("--headless", action="store_true", help="无头模式（默认有头，更拟人）")
    args = ap.parse_args()

    accts = parse_accounts()
    if not accts:
        sys.exit("账号文件为空")
    if args.username:
        acct = next((a for a in accts if a["username"] == args.username), None)
        if not acct:
            sys.exit(f"未找到账号 {args.username}")
    else:
        if args.index >= len(accts):
            sys.exit(f"index {args.index} 越界（共 {len(accts)} 个）")
        acct = accts[args.index]
    return run(acct, args.headless)


if __name__ == "__main__":
    raise SystemExit(main())
