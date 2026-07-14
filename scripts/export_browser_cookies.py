#!/usr/bin/env python3
"""从用户主动开启的 Chromium 调试会话导出指定域名的 Cookie。

本脚本不会读取或解密浏览器的 Cookie 数据库，只通过 Chrome DevTools
Protocol 连接到已开启远程调试的 Chrome/Edge。请仅导出你本人有权使用的会话。

示例：
    python scripts/export_browser_cookies.py \
        --domain example.com \
        --output data/session/example.cookies.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


def normalize_domain(value: str) -> str:
    """把 URL 或域名规范化为不带前导点的主机名。"""
    raw = value.strip().lower()
    if not raw:
        raise argparse.ArgumentTypeError("域名不能为空")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").strip(".")
    if not host or " " in host:
        raise argparse.ArgumentTypeError(f"无效域名：{value}")
    return host


def cookie_matches_domain(cookie_domain: str, allowed_domain: str) -> bool:
    """仅匹配目标域名及其子域名，避免后缀字符串误匹配。"""
    cookie_host = cookie_domain.lower().lstrip(".")
    return cookie_host == allowed_domain or cookie_host.endswith(f".{allowed_domain}")


def atomic_private_write(path: Path, payload: str) -> None:
    """以仅当前用户可读写的权限原子落盘。"""
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从已开启 CDP 的 Chrome/Edge 导出指定域名 Cookie"
    )
    parser.add_argument(
        "--domain",
        required=True,
        type=normalize_domain,
        help="只导出该域名及其子域名，例如 example.com",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="输出 JSON 文件路径",
    )
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="浏览器 CDP 地址（默认：http://127.0.0.1:9222）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "缺少 Playwright。请先运行：python -m pip install playwright",
            file=sys.stderr,
        )
        return 2

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(args.cdp_url)
            if not browser.contexts:
                print("浏览器中没有可用的上下文。", file=sys.stderr)
                return 1

            cookies: list[dict] = []
            seen: set[tuple[str, str, str]] = set()
            for context in browser.contexts:
                for cookie in context.cookies():
                    if not cookie_matches_domain(cookie.get("domain", ""), args.domain):
                        continue
                    key = (cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", ""))
                    if key not in seen:
                        seen.add(key)
                        cookies.append(cookie)

            cookies.sort(key=lambda item: (item.get("domain", ""), item.get("path", ""), item.get("name", "")))
            payload = {
                "domain": args.domain,
                "cookies": cookies,
            }
            atomic_private_write(
                args.output.expanduser().resolve(),
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
    except PlaywrightError as exc:
        print(
            f"无法连接浏览器 CDP：{args.cdp_url}\n"
            "请确认浏览器已使用 --remote-debugging-port=9222 启动。\n"
            f"错误：{exc}",
            file=sys.stderr,
        )
        return 1

    output = args.output.expanduser().resolve()
    print(f"已导出 {len(cookies)} 条 {args.domain} Cookie：{output}")
    print("文件包含登录凭据，请勿提交到 Git、发送给他人或写入日志。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
