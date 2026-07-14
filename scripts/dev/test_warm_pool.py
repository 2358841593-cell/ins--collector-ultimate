#!/usr/bin/env python3
"""安全检查账号池已有 session；绝不回退到 cookie 或密码登录。"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POOL_FILE = ROOT / ".secrets" / "account_pool.json"
SESSION_DIR = ROOT / "data" / "session"
REPORT_FILE = SESSION_DIR / "warm_pool_report.json"


def classify(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "login_required" in text:
        return "SESSION_EXPIRED"
    if "challenge" in text or "checkpoint" in text:
        return "CHALLENGE"
    if "feedback_required" in text or "please wait" in text:
        return "RATE_LIMITED"
    return "ERROR"


def check(username: str, lookup: str) -> dict[str, str | bool]:
    from instagrapi import Client

    session_file = SESSION_DIR / f"instagrapi-{username}.json"
    if not session_file.exists():
        return {"username": username, "ok": False, "bucket": "NO_SESSION", "detail": "无缓存 session"}

    client = Client()
    try:
        client.load_settings(str(session_file))
        own = client.account_info()
    except Exception as exc:
        return {
            "username": username,
            "ok": False,
            "bucket": classify(exc),
            "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
        }

    if lookup:
        try:
            target = client.user_info_by_username_v1(lookup)
        except Exception as exc:
            return {
                "username": username,
                "ok": False,
                "bucket": "LOOKUP_FAILED",
                "detail": f"warm ok; {type(exc).__name__}: {str(exc)[:100]}",
            }
        detail = f"@{own.username}; lookup @{target.username} ok"
    else:
        detail = f"@{own.username}; warm session ok"
    return {"username": username, "ok": True, "bucket": "ALIVE_WARM", "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description="仅用缓存 session 检查账号；绝不冷登录")
    parser.add_argument("--users", default="", help="逗号分隔；留空检查整个池")
    parser.add_argument("--lookup", default="currentbody", help="暖恢复后查询的公开账号；空字符串则跳过")
    parser.add_argument("--gap", type=float, default=3.0, help="账号之间间隔秒数")
    args = parser.parse_args()

    pool = json.loads(POOL_FILE.read_text(encoding="utf-8")).get("accounts", [])
    known = {item["username"] for item in pool}
    users = [value.strip() for value in args.users.split(",") if value.strip()] if args.users else sorted(known)
    unknown = [username for username in users if username not in known]
    if unknown:
        raise SystemExit(f"账号不在池中: {', '.join(unknown)}")

    print(f"纯暖恢复检查 {len(users)} 个账号（不会使用密码/cookie）\n", flush=True)
    results = []
    for index, username in enumerate(users, 1):
        print(f"[{index:2d}/{len(users)}] {username:20s} ... ", end="", flush=True)
        result = check(username, args.lookup)
        results.append(result)
        icon = "✅" if result["ok"] else "❌"
        print(f"{icon} {result['bucket']}: {result['detail']}", flush=True)
        if index < len(users) and args.gap > 0:
            time.sleep(args.gap)

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = Counter(str(item["bucket"]) for item in results)
    print("\n汇总: " + ", ".join(f"{key}={value}" for key, value in sorted(summary.items())))
    print(f"报告: {REPORT_FILE}")
    return 0 if all(bool(item["ok"]) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
