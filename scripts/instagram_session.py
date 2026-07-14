#!/usr/bin/env python3
"""把已登录 Chrome 导出的完整 Cookie 固化为 instagrapi 暖 session。

本模块既提供命令行入口，也提供 AccountPool 共用的 Cookie 注入函数。它不会
使用账号密码或 TOTP，不会打印 Cookie 值；只有在暖 session 复载并确认登录
用户名一致后，才会原子替换正式 session 文件。
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POOL = ROOT / ".secrets" / "account_pool.json"
REQUIRED_COOKIES = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did")


def atomic_private_json_write(path: Path, payload: Any) -> None:
    """以 0600 权限原子写 JSON，避免中途失败破坏已有会话。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if temp.exists():
            temp.unlink()


def require_private_file(path: Path) -> None:
    """拒绝读取组/其他用户可访问的凭据文件。"""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"凭据文件权限必须为 0600：{path}")


def load_browser_cookie_export(path: Path) -> dict[str, str]:
    """读取 export_browser_cookies.py 生成的 JSON，并保留全部 Cookie。"""
    require_private_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("cookies")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Cookie 导出文件没有 cookies 列表")

    cookies: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Cookie 条目必须是对象")
        name = str(row.get("name") or "").strip()
        value = str(row.get("value") or "")
        if not name or not value:
            continue
        if name in cookies and cookies[name] != value:
            raise ValueError(f"存在同名但值不同的 Cookie：{name}")
        cookies[name] = value

    missing = [name for name in REQUIRED_COOKIES if not cookies.get(name)]
    if missing:
        raise ValueError(f"缺少关键 Cookie：{', '.join(missing)}")
    return cookies


def cookie_user_id(cookies: Mapping[str, str]) -> str:
    """从 ds_user_id/sessionid 取账号 ID，并校验两者一致。"""
    sessionid = str(cookies.get("sessionid") or "")
    ds_user_id = str(cookies.get("ds_user_id") or "")
    session_user_id = unquote(sessionid).split(":", 1)[0] if sessionid else ""
    if not session_user_id or not session_user_id.isdigit():
        raise ValueError("sessionid 中没有有效账号 ID")
    if ds_user_id and ds_user_id != session_user_id:
        raise ValueError("sessionid 与 ds_user_id 不属于同一账号")
    return ds_user_id or session_user_id


def sync_client_cookie_jars(client: Any, cookies: Mapping[str, str] | None = None) -> None:
    """让 private/public 请求共用同一套完整 Cookie。"""
    if cookies is None:
        cookies = getattr(client, "cookie_dict", {}) or {}
    for transport_name in ("private", "public"):
        transport = getattr(client, transport_name, None)
        jar = getattr(transport, "cookies", None)
        if jar is None:
            continue
        for name, value in cookies.items():
            jar.set(name, value)


def apply_full_cookie_session(client: Any, cookies: Mapping[str, str]) -> str:
    """把完整浏览器 Cookie 注入 Client，并建立私有 API 授权头。"""
    cookie_map = dict(cookies)
    user_id = cookie_user_id(cookie_map)
    authorization_data = {
        "ds_user_id": user_id,
        "sessionid": cookie_map["sessionid"],
        "should_use_header_over_cookies": True,
    }

    client.settings["cookies"] = cookie_map
    client.settings["authorization_data"] = authorization_data
    if cookie_map.get("mid"):
        client.settings["mid"] = cookie_map["mid"]
    client.init()
    client.authorization_data = authorization_data
    if cookie_map.get("mid"):
        client.mid = cookie_map["mid"]
    sync_client_cookie_jars(client, cookie_map)
    return user_id


def cookie_string(cookies: Mapping[str, str]) -> str:
    """生成项目账号池使用的 Cookie 字符串，不做日志输出。"""
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def merge_cookie_into_existing_account(
    pool_file: Path,
    username: str,
    cookies: Mapping[str, str],
) -> None:
    """只更新主账号池已有账号，禁止静默创建重复账号。"""
    require_private_file(pool_file)
    payload = json.loads(pool_file.read_text(encoding="utf-8"))
    accounts = payload.get("accounts", [])
    matches = [
        account
        for account in accounts
        if str(account.get("username") or "").lower() == username.lower()
    ]
    if len(matches) != 1:
        raise ValueError(f"主账号池中应有且仅有一个 {username}，实际为 {len(matches)}")
    matches[0]["cookie_string"] = cookie_string(cookies)
    atomic_private_json_write(pool_file, payload)


def create_client(proxy: str = "") -> Any:
    from instagrapi import Client

    client = Client()
    if proxy:
        client.set_proxy(proxy)
    return client


def bootstrap_warm_session(
    *,
    username: str,
    cookie_file: Path,
    output: Path,
    proxy: str = "",
    pool_file: Path | None = None,
) -> dict[str, Any]:
    """全量注入、临时落盘、暖复载验证，成功后才替换正式 session。"""
    cookies = load_browser_cookie_export(cookie_file)
    client = create_client(proxy)

    # 已有暖 session 时保留验证过的移动端设备参数，只替换 Cookie 与授权信息。
    if output.exists():
        require_private_file(output)
        client.load_settings(str(output))
    apply_full_cookie_session(client, cookies)

    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = output.with_name(f".{output.name}.candidate")
    atomic_private_json_write(candidate, client.get_settings())

    try:
        verifier = create_client(proxy)
        verifier.load_settings(str(candidate))
        sync_client_cookie_jars(verifier)
        info = verifier.account_info()
        detected = str(getattr(info, "username", "") or "")
        if detected.lower() != username.lower():
            raise RuntimeError(f"Cookie 实际属于 @{detected or '?'}，不是 @{username}")

        # account_info 可能刷新 rur 等短期 Cookie，以验证后的状态作为正式暖 session。
        atomic_private_json_write(candidate, verifier.get_settings())
        final_cookie_names = set((verifier.get_settings().get("cookies") or {}).keys())
        missing_after_reload = sorted(set(cookies) - final_cookie_names)
        if missing_after_reload:
            raise RuntimeError(
                "暖 session 复载后丢失 Cookie：" + ", ".join(missing_after_reload)
            )
        os.replace(candidate, output)
        os.chmod(output, 0o600)
    finally:
        if candidate.exists():
            candidate.unlink()

    if pool_file is not None:
        merge_cookie_into_existing_account(pool_file, username, cookies)

    return {
        "username": username,
        "cookie_count": len(cookies),
        "cookie_names": sorted(cookies),
        "warm_reload_verified": True,
        "output": str(output),
        "pool_updated": pool_file is not None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 Chrome 导出的完整 Instagram Cookie 固化为暖 session"
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--cookie-file", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--proxy", default=os.environ.get("IG_PROXY", ""))
    parser.add_argument(
        "--update-account-pool",
        action="store_true",
        help="同时把完整 Cookie 合并进主账号池的已有账号",
    )
    parser.add_argument("--account-pool", type=Path, default=DEFAULT_POOL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    username = args.username.strip()
    output = args.output or (ROOT / "data" / "session" / f"instagrapi-{username}.json")
    result = bootstrap_warm_session(
        username=username,
        cookie_file=args.cookie_file.expanduser().resolve(),
        output=output.expanduser().resolve(),
        proxy=args.proxy,
        pool_file=(
            args.account_pool.expanduser().resolve()
            if args.update_account_pool
            else None
        ),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("未使用密码或 TOTP；Cookie 值未写入日志。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
