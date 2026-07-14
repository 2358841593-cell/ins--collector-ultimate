#!/usr/bin/env python3
"""一次性预热登录：对指定号做【密码+TOTP 冷登录 ONE 次】→ 成功立刻缓存 session。
策略（用户 2026-06-04 定）：第一次也是唯一一次冷登录，之后全程暖恢复，绝不删 data/session/。
单趟、间隔、不重试。默认只打首轮 DEAD_COOKIE（cookie 已失效但账号可能还活）的号；
CHALLENGE 号不打（密码登录也会撞同一道验证墙）。

用法:
  warmup_login.py --limit 2                 # 先试前 2 个 DEAD_COOKIE
  warmup_login.py                           # 全部 DEAD_COOKIE
  warmup_login.py --users horse.5625203,... # 指定号
"""
from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS = ROOT / ".secrets"
SESSION_DIR = ROOT / "data" / "session"
COOKIE_REPORT = SESSION_DIR / "cookie_report.json"
COLD_FILE = SESSION_DIR / "cold_attempted.json"
GAP = 30  # 冷登录之间间隔（秒）—— 比 cookie 体检更稀疏，避免快速连发指纹


def totp_now(secret: str, interval: int = 30, digits: int = 6) -> str:
    clean = "".join(secret.split()).upper()
    pad = len(clean) % 8
    if pad:
        clean += "=" * (8 - pad)
    key = base64.b32decode(clean)
    dig = hmac.new(key, struct.pack(">Q", int(time.time() // interval)), hashlib.sha1).digest()
    o = dig[-1] & 0x0F
    code = struct.unpack(">I", dig[o:o + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def all_accounts() -> dict:
    src = SECRETS / "account_pool.json"   # 直接读活池
    return {a["username"]: a for a in json.loads(src.read_text(encoding="utf-8"))["accounts"]}


def cold_attempted() -> set[str]:
    if not COLD_FILE.exists():
        return set()
    try:
        return set(json.loads(COLD_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def mark_cold_attempted(username: str) -> None:
    attempted = cold_attempted()
    attempted.add(username)
    COLD_FILE.parent.mkdir(parents=True, exist_ok=True)
    COLD_FILE.write_text(json.dumps(sorted(attempted), indent=2), encoding="utf-8")
    os.chmod(COLD_FILE, 0o600)


def warmup(acc: dict) -> dict:
    from instagrapi import Client

    user = acc["username"]
    if not acc.get("password") or not acc.get("totp_secret"):
        return {"username": user, "ok": False, "detail": "无密码/TOTP，跳过"}
    sf = SESSION_DIR / f"instagrapi-{user}.json"
    if sf.exists():
        return {"username": user, "ok": False, "detail": "session 已存在，拒绝重复冷登录"}
    if user in cold_attempted():
        return {"username": user, "ok": False, "detail": "已记录冷登录尝试，拒绝重复冷登录"}
    cl = Client()  # no_proxy
    try:
        # 登录发出前先记录；成功或失败都不再重复冷登录。
        mark_cold_attempted(user)
        code = totp_now(acc["totp_secret"])
        ok = cl.login(user, acc["password"], verification_code=code)
        if not ok:
            return {"username": user, "ok": False, "detail": "login 返回 False"}
        uname = cl.account_info().username
        cl.dump_settings(str(sf)); os.chmod(sf, 0o600)  # 立刻存好 → 之后暖恢复
        return {"username": user, "ok": True, "detail": f"@{uname} 冷登录成功 → 已缓存 session"}
    except Exception as exc:
        n = type(exc).__name__
        low = (n + str(exc)).lower()
        kind = "CHALLENGE" if "challenge" in low else ("BAD_PW" if ("password" in low or "incorrect" in low) else n)
        return {"username": user, "ok": False, "detail": f"{kind}: {str(exc)[:70]}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--users", default="")
    ap.add_argument("--gap", type=int, default=GAP)
    args = ap.parse_args()

    acc_map = all_accounts()
    if args.users:
        targets = [u.strip() for u in args.users.split(",") if u.strip()]
    else:
        rep = json.loads(COOKIE_REPORT.read_text(encoding="utf-8"))
        targets = [r["username"] for r in rep if r.get("bucket") == "DEAD_COOKIE"]
    if args.limit:
        targets = targets[:args.limit]

    print(f"预热冷登录 {len(targets)} 个号（密码+TOTP，ONE 次，间隔 {args.gap}s，成功即缓存）\n", flush=True)
    results = []
    for i, u in enumerate(targets):
        if u not in acc_map:
            print(f"[{i+1}/{len(targets)}] {u:20s} ... ❌ 不在备份里"); continue
        print(f"[{i+1}/{len(targets)}] {u:20s} ... ", end="", flush=True)
        r = warmup(acc_map[u])
        results.append(r)
        print(("✅ " if r["ok"] else "❌ ") + r["detail"], flush=True)
        if i < len(targets) - 1:
            time.sleep(args.gap)
    (SESSION_DIR / "warmup_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = [r["username"] for r in results if r["ok"]]
    print(f"\n冷登录成功(已缓存): {len(ok)}/{len(results)}  {', '.join(ok) or '—'}")


if __name__ == "__main__":
    main()
