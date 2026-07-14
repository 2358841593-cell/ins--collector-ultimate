#!/usr/bin/env python3
"""验证账号池里的账号能否用 password+TOTP 登录并做他人查询。"""
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
TEST_USER = "currentbody"


def totp_now(secret: str, interval: int = 30, digits: int = 6) -> str:
    clean = "".join(secret.split()).upper()
    pad = len(clean) % 8
    if pad:
        clean += "=" * (8 - pad)
    key = base64.b32decode(clean)
    counter = int(time.time() // interval)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def test_login(account: dict) -> str:
    from instagrapi import Client

    username = account["username"]
    cl = Client()
    settings_file = SESSION_DIR / f"instagrapi-{username}.json"
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        code = totp_now(account["totp_secret"])
        ok = cl.login(account["username"], account["password"], verification_code=code)
        if not ok:
            return "login=False"
    except Exception as exc:
        return f"login-err: {type(exc).__name__}: {str(exc)[:80]}"

    # 缓存 session
    try:
        cl.dump_settings(str(settings_file))
        os.chmod(settings_file, 0o600)
    except Exception:
        pass

    # 测他人查询
    try:
        u = cl.user_info_by_username_v1(TEST_USER)
        return f"OK pk={u.pk}"
    except Exception as exc:
        return f"lookup-fail: {type(exc).__name__}: {str(exc)[:60]}"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    data = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    accounts = data["accounts"][:n]
    print(f"测试前 {len(accounts)} 个账号 password+TOTP 登录 + 他人查询\n")
    ok = 0
    for i, acc in enumerate(accounts):
        print(f"[{i+1}/{len(accounts)}] {acc['username']:18s} ... ", end="", flush=True)
        r = test_login(acc)
        print(r)
        if r.startswith("OK"):
            ok += 1
        if i < len(accounts) - 1:
            time.sleep(8)
    print(f"\n登录+查询成功: {ok}/{len(accounts)}")


if __name__ == "__main__":
    main()
