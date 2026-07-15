"""登录态会话 V2：完整 cookie 注入 + UA + 首次暖 profile（应对 IG 反爬）。

新账号格式（accounts_v2.txt，| 分隔）：
  username | password | totp | cookies(csrftoken=..;datr=..;ig_did=..;mid=..;rur=..;ds_user_id=..;sessionid=..) |
  email? | phone? | User-Agent

要点：
- 注入**全部** cookie（csrftoken/datr/mid/ig_did 是设备指纹，缺了会被判风险 → "Continue as X" 登出屏）
- 设置匹配的 User-Agent
- 首次需暖 profile（点过"继续"，persistent 目录记住登录态）

⚠ IP 层限流：IG 会对来源 IP 限流（"Please wait a few minutes"）。高频请求会触发，
   影响该 IP 下所有账号。必须：拟人低频 + 尊重冷却 + 必要时住宅代理换 IP。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

SECRETS = Path(__file__).resolve().parents[2].parent / ".secrets"
ACCOUNTS_V2 = SECRETS / "accounts_v2.txt"
PROFILE_BASE = SECRETS / "chrome-instagram-profiles"


def parse_accounts(path: Path | None = None) -> list[dict]:
    """解析 accounts_v2.txt → [{username, cookies:{...}, ua, sessionid_present}]。"""
    path = path or ACCOUNTS_V2
    out = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        username = parts[0].strip()
        cookie_str = parts[3]
        cookies = {}
        for kv in cookie_str.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                cookies[k.strip()] = v.strip()
        ua = next((p.strip() for p in parts if p.strip().startswith("Mozilla")), None)
        out.append({
            "username": username,
            "cookies": cookies,
            "ua": ua,
            "has_session": "sessionid" in cookies,
        })
    return out


def cookie_list(acct: dict) -> list[dict]:
    """转成 Playwright add_cookies 格式（sessionid URL 解码）。"""
    out = []
    for k, v in acct["cookies"].items():
        val = unquote(v) if k == "sessionid" else v
        out.append({"name": k, "value": val, "domain": ".instagram.com",
                    "path": "/", "secure": True})
    return out


def open_context(pw, acct: dict, headless: bool = True, proxy: str | None = None):
    """用该账号的完整 cookie + UA 打开持久化 Chrome 上下文。
    proxy 形如 http://127.0.0.1:7897（住宅/Clash 出口，换 IP 绕 IP 限流）。"""
    profile_dir = PROFILE_BASE / acct["username"]
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(user_data_dir=str(profile_dir), channel="chrome", headless=headless,
                  args=["--no-first-run", "--no-default-browser-check"])
    if acct.get("ua"):
        kwargs["user_agent"] = acct["ua"]
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    ctx = pw.chromium.launch_persistent_context(**kwargs)
    ctx.add_cookies(cookie_list(acct))
    return ctx


def warm_profile(ctx, timeout_ms: int = 40000) -> dict:
    """首次暖 profile：进首页，跨过"继续 as X"登出屏，验证进入 feed。
    返回 {warmed, url, note}。IP 限流时会失败（"Please wait"），此时应停手换 IP。"""
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        pg.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=timeout_ms)
        pg.wait_for_timeout(5000)
        body = (pg.inner_text("body")[:200] if pg.query_selector("body") else "")
        if "Please wait" in body or "请稍" in body or "try again" in body.lower():
            return {"warmed": False, "url": pg.url, "note": "ip_rate_limited"}
        # 跨过"继续"屏（若有）
        pg.evaluate(r"""() => { const e=[...document.querySelectorAll('button,div[role=button],a,span')]
            .find(x=>['继续','Continue','繼續'].includes((x.textContent||'').trim())); if(e) e.click(); }""")
        pg.wait_for_timeout(4000)
        feed = pg.evaluate(r"""() => !!document.querySelector('svg[aria-label="Home"],svg[aria-label="主页"],a[href^="/direct/"]')""")
        logged_out = "创建新账户" in (pg.inner_text("body")[:200] if pg.query_selector("body") else "")
        if feed and not logged_out:
            return {"warmed": True, "url": pg.url, "note": "ok"}
        return {"warmed": False, "url": pg.url,
                "note": "still_logged_out_screen (sessionid 未认证或 IP 限流)"}
    except Exception as e:  # noqa: BLE001
        return {"warmed": False, "url": pg.url, "note": f"{type(e).__name__}"}
