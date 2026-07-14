#!/usr/bin/env python3
"""账号恢复体检：暖恢复优先（用缓存 session，安全），无缓存才冷登录探测（单趟、间隔、不重试）。

输出 data/session/recover_report.json，并打印分类表。
分类：
  ALIVE_WARM  缓存 session 直接通过        → 保留
  ALIVE_COLD  冷登录通过（已恢复）          → 保留
  SOFT_COOL   login_required（软冷却，可恢复）→ 保留（只是在歇）
  CHALLENGE   人工验证墙（UFAC/未知步骤）    → 无法自动恢复
  DEAD_2FA    2FA 端点 404（被硬卡）         → 无法自动恢复
  BAD_CREDS   密码/账号错                    → 删
  OTHER       其它                          → 人工看
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POOL_FILE = ROOT / ".secrets" / "account_pool.json"
SESSION_DIR = ROOT / "data" / "session"
REPORT = SESSION_DIR / "recover_report.json"
COLD_GAP = 15  # 冷登录之间的间隔秒数（单趟、温和，避免快速连发指纹）


def totp_now(secret: str, interval: int = 30, digits: int = 6) -> str:
    clean = "".join(secret.split()).upper()
    pad = len(clean) % 8
    if pad:
        clean += "=" * (8 - pad)
    key = base64.b32decode(clean)
    counter = int(time.time() // interval)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def classify(exc: Exception) -> tuple[str, str]:
    name = type(exc).__name__
    msg = str(exc)
    low = (name + " " + msg).lower()
    if "two_factor_login" in low or ("404" in low and "challenge" not in low):
        return "DEAD_2FA", f"{name}: {msg[:70]}"
    if "challenge" in low or name in {"ChallengeRequired", "ChallengeUnknownStep"}:
        return "CHALLENGE", f"{name}: {msg[:70]}"
    if "login_required" in low or name == "LoginRequired":
        return "SOFT_COOL", f"{name}: {msg[:70]}"
    if "password" in low or "incorrect" in low or "bad" in low:
        return "BAD_CREDS", f"{name}: {msg[:70]}"
    return "OTHER", f"{name}: {msg[:70]}"


def check_one(account: dict, did_cold: list[bool]) -> dict:
    from instagrapi import Client

    username = account["username"]
    settings_file = SESSION_DIR / f"instagrapi-{username}.json"
    cl = Client()  # no_proxy 默认；体检不绑代理

    # 1) 暖恢复：有缓存就先试（零冷登录，安全）
    if settings_file.exists():
        try:
            cl.load_settings(str(settings_file))
            info = cl.account_info()
            if getattr(info, "username", None):
                return {"username": username, "bucket": "ALIVE_WARM", "detail": "cached session 有效"}
        except Exception:
            pass  # 暖恢复失败 → 落到冷登录
        cl = Client()

    # 2) 冷登录探测（无缓存 / 暖恢复失败）。单趟、间隔、不重试。
    if did_cold[0]:
        time.sleep(COLD_GAP)
    did_cold[0] = True
    try:
        code = totp_now(account["totp_secret"]) if account.get("totp_secret") else ""
        if not cl.login(username, account["password"], verification_code=code):
            return {"username": username, "bucket": "BAD_CREDS", "detail": "login returned False"}
    except Exception as exc:
        bucket, detail = classify(exc)
        return {"username": username, "bucket": bucket, "detail": detail}

    # 冷登录成功 → 存缓存 + 校验
    try:
        cl.dump_settings(str(settings_file))
        os.chmod(settings_file, 0o600)
    except Exception:
        pass
    try:
        info = cl.account_info()
        if getattr(info, "username", None):
            return {"username": username, "bucket": "ALIVE_COLD", "detail": "冷登录通过，已恢复"}
        return {"username": username, "bucket": "OTHER", "detail": "登录 ok 但 account_info 空"}
    except Exception as exc:
        bucket, detail = classify(exc)
        return {"username": username, "bucket": bucket, "detail": "登录后校验失败: " + detail}


KEEP = {"ALIVE_WARM", "ALIVE_COLD", "SOFT_COOL"}


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    accounts = json.loads(POOL_FILE.read_text(encoding="utf-8"))["accounts"]
    print(f"体检 {len(accounts)} 个账号（暖恢复优先，冷登录间隔 {COLD_GAP}s，单趟不重试）\n", flush=True)

    did_cold = [False]
    results = []
    for i, acc in enumerate(accounts):
        print(f"[{i+1:2d}/{len(accounts)}] {acc['username']:18s} ... ", end="", flush=True)
        r = check_one(acc, did_cold)
        results.append(r)
        keep = "保留" if r["bucket"] in KEEP else "删"
        print(f"{r['bucket']:11s} [{keep}]  {r['detail']}", flush=True)

    REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    by = {}
    for r in results:
        by.setdefault(r["bucket"], []).append(r["username"])
    keep = [r["username"] for r in results if r["bucket"] in KEEP]
    drop = [r["username"] for r in results if r["bucket"] not in KEEP]
    print("\n──────── 汇总 ────────")
    for b in ["ALIVE_WARM", "ALIVE_COLD", "SOFT_COOL", "CHALLENGE", "DEAD_2FA", "BAD_CREDS", "OTHER"]:
        if b in by:
            print(f"  {b:11s} {len(by[b]):2d}  {', '.join(by[b])}")
    print(f"\n  保留(可用/将恢复): {len(keep)}  {', '.join(keep) or '—'}")
    print(f"  删除(无法自动恢复): {len(drop)}  {', '.join(drop) or '—'}")
    print(f"\n报告已写入 {REPORT}")


if __name__ == "__main__":
    main()
