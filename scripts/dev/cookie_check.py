#!/usr/bin/env python3
"""Cookie 账号体检：用 sessionid 登录(login_by_sessionid)，校验 account_info，成功即缓存
session(供正式跑暖恢复)。单趟、间隔、低风险（不走密码冷登录）。

输出 data/session/cookie_report.json + 打印表。
分类：ALIVE 活 / DEAD 死(原因)。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / ".secrets" / "account_pool.json"
SESSION_DIR = ROOT / "data" / "session"
REPORT = SESSION_DIR / "cookie_report.json"
GAP = 10  # 账号间隔秒数


def sessionid_of(cookie: str) -> str:
    for part in cookie.split(";"):
        if part.strip().startswith("sessionid="):
            return part.strip().split("=", 1)[1]
    return ""


def classify(exc: Exception) -> str:
    n = type(exc).__name__
    low = (n + " " + str(exc)).lower()
    if "challenge" in low or "checkpoint" in low or n.startswith("Challenge"):
        return "CHALLENGE"   # 账号被关进验证墙（cookie 能认人但 IG 要求过验证）
    if "login_required" in low or "logged out" in low or "csrf" in low:
        return "DEAD_COOKIE"  # cookie 失效/被登出
    return "OTHER:" + n


def check(acc: dict) -> dict:
    from instagrapi import Client

    user = acc["username"]
    sf = SESSION_DIR / f"instagrapi-{user}.json"
    cookie = acc.get("cookie_string", "")

    # 非 cookie 号（unicor）：缓存暖恢复
    if not cookie:
        cl = Client()
        try:
            if not sf.exists():
                return {"username": user, "alive": False, "bucket": "SKIP", "detail": "无 cookie 无缓存，跳过"}
            cl.load_settings(str(sf))
            uname = cl.account_info().username
            return {"username": user, "alive": True, "bucket": "ALIVE", "detail": f"@{uname}（缓存暖恢复）"}
        except Exception as exc:
            return {"username": user, "alive": False, "bucket": classify(exc), "detail": f"{type(exc).__name__}: {str(exc)[:60]}"}

    # cookie 号：直接注入 sessionid 打私有 API（pipeline 实际用的路径），暴露真实拦截原因
    sid = sessionid_of(cookie)
    if not sid:
        return {"username": user, "alive": False, "bucket": "DEAD_COOKIE", "detail": "无 sessionid"}
    uid = sid.split("%3A")[0]
    cl = Client()  # no_proxy；体检不绑代理
    cl.settings["cookies"] = {"sessionid": sid}
    cl.init()
    cl.authorization_data = {"ds_user_id": uid, "sessionid": sid, "should_use_header_over_cookies": True}
    try:
        u = cl.user_info_v1(int(uid))
        try:  # 活号：缓存 session 供正式跑暖恢复
            cl.dump_settings(str(sf)); os.chmod(sf, 0o600)
        except Exception:
            pass
        return {"username": user, "alive": True, "bucket": "ALIVE", "detail": f"@{u.username} pk={u.pk}"}
    except Exception as exc:
        return {"username": user, "alive": False, "bucket": classify(exc), "detail": f"{type(exc).__name__}: {str(exc)[:60]}"}


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    accounts = json.loads(POOL.read_text(encoding="utf-8"))["accounts"]
    print(f"Cookie 体检 {len(accounts)} 个号（间隔 {GAP}s，登录成功即缓存 session）\n", flush=True)
    results = []
    for i, acc in enumerate(accounts):
        print(f"[{i+1:2d}/{len(accounts)}] {acc['username']:20s} ... ", end="", flush=True)
        r = check(acc)
        results.append(r)
        print(("✅ " if r["alive"] else "❌ ") + r["detail"], flush=True)
        if i < len(accounts) - 1:
            time.sleep(GAP)
    REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    alive = [r["username"] for r in results if r["alive"]]
    by: dict[str, list] = {}
    for r in results:
        by.setdefault(r["bucket"], []).append(r["username"])
    print("\n──────── 汇总 ────────")
    print(f"  活 ALIVE: {len(alive)}/{len(results)}  {', '.join(alive) or '—'}")
    for b in sorted(k for k in by if k != "ALIVE"):
        print(f"  {b}: {len(by[b])}  {', '.join(by[b])}")
    print(f"\n报告: {REPORT}")


if __name__ == "__main__":
    main()
