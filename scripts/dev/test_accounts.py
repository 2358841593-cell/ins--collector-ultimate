#!/usr/bin/env python3
"""测试所有账号哪个能通过 cookie sessionid 正常调用 API。

对每个账号：
1. 解析 cookie_string 提取 sessionid + 相关 cookie
2. 用 login_by_sessionid 建立会话
3. 尝试一个轻量 API 调用 (account_info) 验证是否真的可用
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS_FILE = ROOT / ".secrets" / "instagram_accounts.json"
SESSION_DIR = ROOT / "data" / "session"


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    cookies = {}
    for part in cookie_string.split(";"):
        if not part.strip() or "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def test_account(account: dict) -> tuple[bool, str]:
    from instagrapi import Client

    username = account["username"]
    cookies = parse_cookie_string(account.get("cookie_string", ""))
    sessionid = cookies.get("sessionid", "")
    if not sessionid:
        return False, "no sessionid in cookie"

    cl = Client()
    try:
        # login_by_sessionid 会从 sessionid 提取 user_id 并验证
        ok = cl.login_by_sessionid(sessionid)
        if not ok:
            return False, "login_by_sessionid returned False"
    except Exception as exc:
        return False, f"login_by_sessionid: {type(exc).__name__}: {exc}"

    # 轻量 API 调用验证 session 真的可用
    try:
        info = cl.account_info()
        detected = getattr(info, "username", None) or "?"
        # 保存 session 供后续复用
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        settings_file = SESSION_DIR / f"instagrapi-{username}.json"
        cl.dump_settings(str(settings_file))
        import os
        os.chmod(settings_file, 0o600)
        return True, f"OK (detected: {detected})"
    except Exception as exc:
        return False, f"account_info: {type(exc).__name__}: {str(exc)[:120]}"


def main() -> int:
    data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    accounts = data.get("accounts", [])
    print(f"测试 {len(accounts)} 个账号（cookie sessionid 方式）\n")

    working = []
    for i, acc in enumerate(accounts):
        username = acc["username"]
        print(f"[{i+1}/{len(accounts)}] {username} ... ", end="", flush=True)
        ok, msg = test_account(acc)
        status = "✓" if ok else "✗"
        print(f"{status} {msg}")
        if ok:
            working.append(username)
        if i < len(accounts) - 1:
            time.sleep(3)

    print(f"\n可用账号: {len(working)}/{len(accounts)}")
    for w in working:
        print(f"  ✓ {w}")
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(main())
