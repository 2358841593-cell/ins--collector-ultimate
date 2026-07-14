#!/usr/bin/env python3
"""账号池 + 自动轮换机制。

解决的问题：单个 Instagram 采集账号私有 API 容量很低，约 10-20 次他人查询后
触发 login_required 软封，恢复需 30+ 分钟。用一个账号池轮换可绕过该限制。

机制：
- 主动轮换：单账号用满 rotate_every 次成功请求就主动换下一个（不等触发软封）
- 被动轮换：调用遇 login_required / challenge / checkpoint 立即 cooldown 当前号 + 换号重试
- Cooldown 持久化：data/session/pool_state.json 记录每号冷却到期时间，重启不复用被烧号
- 透明代理：AccountPool 通过 __getattr__ 转发 Client 方法，调用方无需改动

用法：
    pool = AccountPool(accounts, rotate_every=10, cooldown_minutes=30)
    user = pool.user_info_by_username_v1("currentbody")   # 自动选号/轮换
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .instagram_session import apply_full_cookie_session, sync_client_cookie_jars
except ImportError:  # 兼容直接执行 scripts/*.py
    from instagram_session import apply_full_cookie_session, sync_client_cookie_jars

ROOT = Path(__file__).resolve().parents[1]
SESSION_DIR = ROOT / "data" / "session"
STATE_FILE = SESSION_DIR / "pool_state.json"
# 一次性冷登录纪律：记录哪些号已经用掉了"唯一一次密码冷登录"额度（不论成败）。
# 持久化，跨 run 有效——避免对同一个号反复冷登录（上次群体烧号的根因就是反复冷登录）。
COLD_FILE = SESSION_DIR / "cold_attempted.json"

# 触发轮换的错误关键词（出现即认为当前账号受限）
BLOCK_MARKERS = (
    "login_required", "challenge", "checkpoint",
    "feedback_required", "consent_required", "csrf",
)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _totp_now(secret: str, interval: int = 30, digits: int = 6) -> str:
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


def _parse_cookie_string(cookie_string: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_string.split(";"):
        if not part.strip() or "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


class PoolExhausted(RuntimeError):
    """所有账号都在冷却或登录失败。"""


class AccountPool:
    def __init__(
        self,
        accounts: list[dict[str, Any]],
        rotate_every: int = 10,
        cooldown_minutes: int = 30,
        no_proxy: bool = True,
        rotate_sleep: int = 3,
        wait_on_exhaust: bool = False,
        max_total_wait_minutes: int = 240,
    ) -> None:
        if not accounts:
            raise ValueError("账号池为空")
        # 复制，避免外部修改
        self._accounts = list(accounts)
        self._rotate_every = max(1, rotate_every)
        self._cooldown_seconds = cooldown_minutes * 60
        self._no_proxy = no_proxy
        self._rotate_sleep = rotate_sleep
        # 全量扫描模式：池子全冷却时等待最早账号恢复再继续，而非直接耗尽退出
        self._wait_on_exhaust = wait_on_exhaust
        self._max_total_wait = max_total_wait_minutes * 60
        self._waited_total = 0.0

        self._state: dict[str, float] = self._load_state()  # username -> cooldown_until(epoch)
        self._cold_attempted: set[str] = self._load_cold()  # 已用掉唯一一次冷登录额度的号
        self._idx = -1
        self._current_username: str | None = None
        self._current_client: Any = None
        self._current_count = 0
        self._total_requests = 0
        self._rotations = 0

    # ── 状态持久化 ──────────────────────────────────────────────────────────

    def _load_state(self) -> dict[str, float]:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_state(self) -> None:
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            os.chmod(STATE_FILE, 0o600)
        except Exception:
            pass

    def _load_cold(self) -> set[str]:
        if COLD_FILE.exists():
            try:
                return set(json.loads(COLD_FILE.read_text(encoding="utf-8")))
            except Exception:
                return set()
        return set()

    def _mark_cold_attempted(self, username: str) -> None:
        """标记某号已用掉唯一一次冷登录额度并持久化。登录前调用——这样即便冷登录撞验证墙，
        额度也已消耗，之后绝不再对它冷登录。"""
        self._cold_attempted.add(username)
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            COLD_FILE.write_text(json.dumps(sorted(self._cold_attempted), indent=2), encoding="utf-8")
            os.chmod(COLD_FILE, 0o600)
        except Exception:
            pass

    # ── 账号可用性 ──────────────────────────────────────────────────────────

    def _is_available(self, username: str) -> bool:
        return self._state.get(username, 0) <= time.time()

    def available_count(self) -> int:
        return sum(1 for a in self._accounts if self._is_available(a["username"]))

    # ── 登录 ────────────────────────────────────────────────────────────────

    def _validate(self, cl: Any) -> bool:
        try:
            info = cl.account_info()
            return bool(getattr(info, "username", None))
        except Exception:
            return False

    def _build_client(self, account: dict[str, Any]) -> Any:
        from instagrapi import Client

        username = account["username"]
        settings_file = SESSION_DIR / f"instagrapi-{username}.json"
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

        cl = Client()
        if not self._no_proxy:
            proxy = os.environ.get("IG_PROXY", "")
            if proxy:
                cl.set_proxy(proxy)

        # 1) 复用缓存 session
        if settings_file.exists():
            try:
                cl.load_settings(str(settings_file))
                sync_client_cookie_jars(cl)
                if self._validate(cl):
                    return cl
            except Exception:
                pass
            cl = Client()
            if not self._no_proxy:
                proxy = os.environ.get("IG_PROXY", "")
                if proxy:
                    cl.set_proxy(proxy)

        # 2) 完整浏览器 Cookie（sessionid + 设备/CSRF Cookie）
        cookie = account.get("cookie_string", "")
        if cookie:
            cookie_map = _parse_cookie_string(cookie)
            sid = cookie_map.get("sessionid", "")
            if sid:
                try:
                    apply_full_cookie_session(cl, cookie_map)
                    if self._validate(cl):
                        cl.dump_settings(str(settings_file))
                        os.chmod(settings_file, 0o600)
                        return cl
                except Exception:
                    cl = Client()

        # 3) password + TOTP —— 一次性冷登录纪律（用户 2026-06-04 定）
        #    每个号一生只冷登录一次：成功就缓存 session、之后全程暖恢复(tier1)；失败(撞验证墙)
        #    也消耗掉额度、永不重试。杜绝"反复冷登录"——上一批 19/20 被 UFAC 烧死，正是因为
        #    删了缓存导致每次轮换都冷登录。配套铁律：永不删 data/session/ 缓存。
        if username in self._cold_attempted:
            raise RuntimeError("已用掉唯一一次冷登录额度，跳过（需人工补 cookie/session 才能复活）")
        if not account.get("password"):
            raise RuntimeError("无密码，且 cookie/缓存均失效，跳过")
        self._mark_cold_attempted(username)  # 登录前先消耗额度——即便下一行撞墙也不再重试
        code = _totp_now(account["totp_secret"]) if account.get("totp_secret") else ""
        if not cl.login(username, account["password"], verification_code=code):
            raise RuntimeError("login returned False")
        cl.dump_settings(str(settings_file))
        os.chmod(settings_file, 0o600)
        return cl

    # ── 轮换 ────────────────────────────────────────────────────────────────

    def _rotate(self) -> Any:
        """切换到下一个可用账号。全量扫描模式下，池子全冷却时等待最早账号恢复再继续。"""
        self._save_state()
        n = len(self._accounts)
        while True:
            now = time.time()
            for _ in range(n):
                self._idx = (self._idx + 1) % n
                account = self._accounts[self._idx]
                uname = account["username"]
                if uname == self._current_username:
                    continue
                if not self._is_available(uname):
                    continue
                try:
                    cl = self._build_client(account)
                except Exception as exc:
                    _log(f"  ✗ 账号 {uname} 登录失败: {type(exc).__name__}: {str(exc)[:50]}，冷却")
                    self._state[uname] = now + self._cooldown_seconds
                    self._save_state()
                    continue
                self._current_client = cl
                self._current_username = uname
                self._current_count = 0
                self._rotations += 1
                _log(f"  ↻ 切换账号 → {uname} (可用 {self.available_count()}/{n})")
                if self._rotate_sleep:
                    time.sleep(self._rotate_sleep)
                return cl

            # 全部不可用：等待模式 → 睡到最早账号恢复再重试
            if self._wait_on_exhaust and self._waited_total < self._max_total_wait:
                future = [v for v in self._state.values() if v > time.time()]
                wait_s = max(20, min(future) - time.time()) if future else 60
                wait_s = min(wait_s + 5, self._cooldown_seconds + 10)
                self._waited_total += wait_s
                _log(f"  ⏳ 账号池全部冷却中，等待 ~{int(wait_s)}s 后重试"
                     f"（全量模式，累计已等 {int(self._waited_total/60)} 分）")
                time.sleep(wait_s)
                continue

            raise PoolExhausted(
                f"账号池耗尽：{n} 个账号全部冷却中或登录失败。"
                f"最早可在 ~{self._min_cooldown_minutes()} 分钟后重试。"
            )

    def _min_cooldown_minutes(self) -> int:
        now = time.time()
        future = [v for v in self._state.values() if v > now]
        if not future:
            return 0
        return max(1, int((min(future) - now) / 60))

    def _mark_blocked(self) -> None:
        if self._current_username:
            self._state[self._current_username] = time.time() + self._cooldown_seconds
            self._save_state()
        self._current_client = None
        self._current_username = None
        self._current_count = 0

    def _ensure_client(self) -> Any:
        if self._current_client is None or self._current_count >= self._rotate_every:
            if self._current_client is not None:
                _log(f"  账号 {self._current_username} 达到 {self._rotate_every} 次请求上限，主动轮换")
            self._rotate()
        return self._current_client

    # ── 带轮换的方法调用 ──────────────────────────────────────────────────────

    def _call_with_rotation(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        attempts = 0
        max_attempts = len(self._accounts) + 1
        while attempts <= max_attempts:
            cl = self._ensure_client()
            try:
                result = getattr(cl, method_name)(*args, **kwargs)
                self._current_count += 1
                self._total_requests += 1
                return result
            except PoolExhausted:
                raise
            except Exception as exc:
                msg = str(exc).lower()
                if any(marker in msg for marker in BLOCK_MARKERS):
                    _log(f"  ⚠ {self._current_username} 受限 ({method_name}: "
                         f"{type(exc).__name__})，cooldown + 轮换")
                    self._mark_blocked()
                    attempts += 1
                    continue
                # 非限流错误：直接抛出给调用方处理
                raise
        raise PoolExhausted(f"账号池耗尽，无法完成 {method_name}")

    # ── 透明代理 ──────────────────────────────────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # 私有/dunder 名字不代理（避免 __init__ 期间递归）
        if name.startswith("_"):
            raise AttributeError(name)
        cl = self._ensure_client()
        attr = getattr(cl, name)
        if callable(attr):
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return self._call_with_rotation(name, *args, **kwargs)
            return wrapper
        # 非方法属性（如 rank_token）：返回当前 client 的值
        return attr

    # ── 公开信息 ──────────────────────────────────────────────────────────────

    @property
    def current_username(self) -> str | None:
        return self._current_username

    def stats(self) -> dict[str, Any]:
        return {
            "total_requests": self._total_requests,
            "rotations": self._rotations,
            "available": self.available_count(),
            "total_accounts": len(self._accounts),
            "current": self._current_username,
        }

    def warm_up(self) -> str:
        """预先登录一个账号，返回其用户名。"""
        self._ensure_client()
        return self._current_username or "?"


def load_pool_accounts(
    pool_file: Path | None = None,
    include_cookie_accounts: bool = False,
    cookie_file: Path | None = None,
) -> list[dict[str, Any]]:
    """加载账号池。默认只用 account_pool.json（20 个 TOTP 账号）。"""
    accounts: list[dict[str, Any]] = []
    pool_file = pool_file or (ROOT / ".secrets" / "account_pool.json")
    if pool_file.exists():
        data = json.loads(pool_file.read_text(encoding="utf-8"))
        accounts.extend(data.get("accounts", []))
    if include_cookie_accounts:
        cookie_file = cookie_file or (ROOT / ".secrets" / "instagram_accounts.json")
        if cookie_file.exists():
            data = json.loads(cookie_file.read_text(encoding="utf-8"))
            accounts.extend(data.get("accounts", []))
    return accounts
