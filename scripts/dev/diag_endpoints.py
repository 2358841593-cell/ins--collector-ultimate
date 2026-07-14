#!/usr/bin/env python3
"""诊断私有 API 端点可用性。慢速、只用私有端点。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS_FILE = ROOT / ".secrets" / "instagram_accounts.json"
SESSION_DIR = ROOT / "data" / "session"

sys.path.insert(0, str(ROOT / "scripts"))
from discover import build_client  # noqa: E402

DELAY = 10


def run(label, fn):
    print(f"\n>>> {label}")
    try:
        result = fn()
        if isinstance(result, list):
            print(f"    ✓ 返回 {len(result)} 项")
        else:
            pk = getattr(result, "pk", None)
            uname = getattr(result, "username", None)
            print(f"    ✓ pk={pk} username={uname}")
        return True
    except Exception as exc:
        print(f"    ✗ {type(exc).__name__}: {str(exc)[:150]}")
        return False


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IG_USERNAME", "")
    if not username:
        raise SystemExit("用法：python scripts/dev/diag_endpoints.py <username>，或设置 IG_USERNAME")
    print(f"诊断账号: {username} (私有端点, 间隔 {DELAY}s)")
    cl = build_client(username, no_proxy=True)

    # 1. 自身信息（已知可用）
    run("account_info() [自身]", lambda: cl.account_info())
    time.sleep(DELAY)

    # 2. 私有 user 查询（其他用户）
    run("user_info_by_username_v1('currentbody')",
        lambda: cl.user_info_by_username_v1("currentbody"))
    time.sleep(DELAY)

    run("user_info_by_username_v1('therabody')",
        lambda: cl.user_info_by_username_v1("therabody"))
    time.sleep(DELAY)

    # 3. hashtag 私有端点
    run("hashtag_medias_v1('skincare', amount=3)",
        lambda: cl.hashtag_medias_v1("skincare", amount=3))
    time.sleep(DELAY)

    run("hashtag_medias_top('amazonfinds', amount=3)",
        lambda: cl.hashtag_medias_top("amazonfinds", amount=3))
    time.sleep(DELAY)

    # 4. 私有用户帖子
    try:
        uid = cl.user_id_from_username("currentbody")
        run(f"user_medias_v1(currentbody_id={uid}, amount=3)",
            lambda: cl.user_medias_v1(uid, amount=3))
    except Exception as exc:
        print(f"\n>>> user_id_from_username 失败: {exc}")

    print("\n诊断完成。")


if __name__ == "__main__":
    main()
