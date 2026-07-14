#!/usr/bin/env python3
"""导入账号池到 .secrets/account_pool.json。

输入格式（每行一个，stdin 粘贴）：
    username----password----totp_secret

输出文件仅本地，权限 600，永不入版本控制。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = ROOT / ".secrets"
POOL_FILE = SECRETS_DIR / "account_pool.json"


def parse_line(line: str) -> dict:
    parts = line.strip().split("----")
    if len(parts) < 3:
        raise ValueError("expected username----password----totp_secret")
    return {
        "username": parts[0].strip(),
        "password": parts[1].strip(),
        "totp_secret": parts[2].strip(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="安全导入 password+TOTP Instagram 账号")
    ap.add_argument(
        "--replace",
        action="store_true",
        help="显式覆盖整个账号池；默认按 username 合并并保留原账号",
    )
    args = ap.parse_args()

    raw = [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
    if not raw:
        raise SystemExit("从 stdin 粘贴账号行：username----password----totp_secret")

    incoming = []
    for i, line in enumerate(raw, 1):
        try:
            account = parse_line(line)
            account["login"] = "password"
            incoming.append(account)
        except ValueError as exc:
            raise SystemExit(f"第 {i} 行: {exc}") from exc

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if POOL_FILE.exists() and not args.replace:
        data = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        existing = list(data.get("accounts", []))

    by_username = {a["username"]: dict(a) for a in existing}
    order = [a["username"] for a in existing]
    added = 0
    updated = 0
    for account in incoming:
        username = account["username"]
        if username in by_username:
            by_username[username].update(account)
            updated += 1
        else:
            by_username[username] = account
            order.append(username)
            added += 1
    accounts = [by_username[username] for username in order]

    if POOL_FILE.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = POOL_FILE.with_name(f"{POOL_FILE.name}.bak-{stamp}")
        shutil.copy2(POOL_FILE, backup)
        backup.chmod(stat.S_IRUSR | stat.S_IWUSR)

    temp = POOL_FILE.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, POOL_FILE)
    POOL_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    mode = "覆盖" if args.replace else "合并"
    print(
        f"已{mode}保存 {len(accounts)} 个账号到 {POOL_FILE}（新增 {added}，更新 {updated}，权限 600）"
    )
    for a in incoming:
        print(f"  - {a['username']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
