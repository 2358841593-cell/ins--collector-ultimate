#!/usr/bin/env python3
"""账号健康检查：对每个账号强制 cookie 重登（不复用缓存），测真实他人查询。

回答两个问题：
1. 哪些账号现在能做 user_info_by_username_v1（他人查询）
2. cookie 重登 vs 复用缓存 session 是否有差异
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS_FILE = ROOT / ".secrets" / "instagram_accounts.json"

TEST_USER = "currentbody"  # 已知存在的公开账号


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    cookies = {}
    for part in cookie_string.split(";"):
        if not part.strip() or "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


def check(account: dict) -> str:
    from instagrapi import Client

    username = account["username"]
    cookies = parse_cookie_string(account.get("cookie_string", ""))
    sessionid = cookies.get("sessionid", "")
    if not sessionid:
        return "no-sessionid"

    cl = Client()
    # 强制 cookie 重登，不加载任何缓存 session
    try:
        if not cl.login_by_sessionid(sessionid):
            return "login-fail"
    except Exception as exc:
        return f"login-err:{type(exc).__name__}"

    # 测真实他人查询
    try:
        u = cl.user_info_by_username_v1(TEST_USER)
        return f"OK pk={u.pk}"
    except Exception as exc:
        return f"lookup-fail:{type(exc).__name__}:{str(exc)[:40]}"


def main():
    data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    accounts = data.get("accounts", [])
    print(f"健康检查 {len(accounts)} 个账号（cookie 重登 + 他人查询 @{TEST_USER}）\n")
    ok = []
    for i, acc in enumerate(accounts):
        u = acc["username"]
        print(f"[{i+1}/{len(accounts)}] {u:20s} ... ", end="", flush=True)
        r = check(acc)
        print(r)
        if r.startswith("OK"):
            ok.append(u)
        if i < len(accounts) - 1:
            time.sleep(5)
    print(f"\n可做他人查询: {len(ok)}/{len(accounts)}: {ok}")


if __name__ == "__main__":
    main()
