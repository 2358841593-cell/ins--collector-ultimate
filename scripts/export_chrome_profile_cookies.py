#!/usr/bin/env python3
"""在 macOS 本机从普通 Chrome profile 导出指定域名 Cookie。

仅适用于用户本人有权使用的本机 Chrome 会话。脚本通过 macOS Keychain 解密
Chrome v10/v11 Cookie，输出为 Playwright 兼容 JSON；不会打印 Cookie 值。

macOS 系统 Python 通常自带 cryptography：
    /usr/bin/python3 scripts/export_chrome_profile_cookies.py \
      --profile Default --domain instagram.com \
      --output .secrets/instagram-example.cookies.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
CHROME_EPOCH_OFFSET = 11_644_473_600


def normalize_domain(value: str) -> str:
    raw = value.strip().lower()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").strip(".")
    if not host or " " in host:
        raise argparse.ArgumentTypeError(f"无效域名：{value}")
    return host


def atomic_private_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if temp.exists():
            temp.unlink()


def chrome_time_to_unix(value: int) -> float:
    return -1 if not value else value / 1_000_000 - CHROME_EPOCH_OFFSET


def decrypt_cookie(
    *,
    host_key: str,
    plaintext: str,
    encrypted: bytes,
    key: bytes,
    db_version: int,
) -> str:
    if plaintext:
        return plaintext
    if not encrypted:
        return ""
    if encrypted[:3] not in (b"v10", b"v11"):
        raise RuntimeError("发现不支持的 Chrome Cookie 加密版本")

    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError(
            "缺少 cryptography；macOS 请使用 /usr/bin/python3 运行本脚本"
        ) from exc

    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    decoded = unpadder.update(padded) + unpadder.finalize()
    if db_version >= 24:
        expected = hashlib.sha256(host_key.encode("utf-8")).digest()
        if not decoded.startswith(expected):
            raise RuntimeError(f"Cookie 主机摘要校验失败：{host_key}")
        decoded = decoded[len(expected):]
    return decoded.decode("utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从普通 Chrome profile 导出域名 Cookie")
    parser.add_argument("--profile", default="Default", help="Chrome profile 目录名")
    parser.add_argument("--domain", required=True, type=normalize_domain)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--chrome-root", type=Path, default=CHROME_ROOT)
    return parser.parse_args()


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("本脚本只支持 macOS Chrome + Keychain")
    args = parse_args()
    db_path = args.chrome_root.expanduser() / args.profile / "Cookies"
    if not db_path.exists():
        raise SystemExit(f"找不到 Chrome Cookie 数据库：{db_path}")

    safe_storage = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
        stderr=subprocess.DEVNULL,
    ).rstrip(b"\r\n")
    key = hashlib.pbkdf2_hmac("sha1", safe_storage, b"saltysalt", 1003, dklen=16)

    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        version_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'version'"
        ).fetchone()
        db_version = int(version_row[0]) if version_row else 0
        rows = connection.execute(
            """
            SELECT host_key, name, value, encrypted_value, path, expires_utc,
                   is_secure, is_httponly, samesite
            FROM cookies
            WHERE host_key = ? OR host_key = ? OR host_key LIKE ?
            ORDER BY host_key, path, name
            """,
            (args.domain, f".{args.domain}", f"%.{args.domain}"),
        ).fetchall()

    same_site = {1: "Lax", 2: "Strict"}
    cookies = []
    for host_key, name, value, encrypted, path, expires, secure, httponly, samesite in rows:
        decrypted = decrypt_cookie(
            host_key=host_key,
            plaintext=value,
            encrypted=encrypted,
            key=key,
            db_version=db_version,
        )
        if not decrypted:
            continue
        cookies.append(
            {
                "name": name,
                "value": decrypted,
                "domain": host_key,
                "path": path,
                "expires": chrome_time_to_unix(expires),
                "httpOnly": bool(httponly),
                "secure": bool(secure),
                "sameSite": same_site.get(samesite, "None"),
            }
        )

    output = args.output.expanduser().resolve()
    atomic_private_write(
        output,
        json.dumps(
            {
                "domain": args.domain,
                "source": f"Google Chrome/{args.profile}",
                "cookies": cookies,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    names = sorted(cookie["name"] for cookie in cookies)
    print(
        json.dumps(
            {
                "output": str(output),
                "cookie_count": len(cookies),
                "cookie_names": names,
                "has_sessionid": "sessionid" in names,
                "mode": oct(output.stat().st_mode & 0o777),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("Cookie 值未写入日志；输出文件请勿提交或发送给他人。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
