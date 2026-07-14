#!/usr/bin/env python3
"""BrowserCollector —— 登录态浏览器会话采集 IG（B0-2，浏览器唯一通道）。

用已建好的登录态 Chrome profile（cookie 注入，见 pool_health.py）打开目标创作者
主页，通过浏览器自身会话调用 web_profile_info（web app 同款端点，非 instagrapi
私有移动 API），产出与旧 discover.py `_compact` profile 同构的字段；失败回退
og:description 解析（中英 locale 兼容）。

这是 discover.py 采集层要接的 `Collector` 协议的 profile 实现雏形。
不做密码登录、不发写操作、不回显凭证。

用法：
    .venv/bin/python scripts/browser_collect.py --account gimdohyeonay699 --handle annascountryhome
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".secrets"
PROFILE_BASE = SECRETS / "chrome-instagram-profiles"
IG_WEB_APP_ID = "936619743392459"  # 公开 web app id，web 端自身请求头

# og:description 数字解析：兼容英文 "1,234 Followers" 与中文 "1,234 位粉丝"
_NUM = r"([\d.,]+\s*[KMkm万]?)"
_FOLLOWERS_RE = re.compile(_NUM + r"\s*(?:Followers|位粉丝|粉丝)", re.I)
_FOLLOWING_RE = re.compile(r"(?:Following|已关注)\s*" + _NUM + r"|" + _NUM + r"\s*(?:Following|已关注)", re.I)
_POSTS_RE = re.compile(_NUM + r"\s*(?:Posts|篇帖子|帖子)", re.I)


def _to_int(s: str) -> int | None:
    if not s:
        return None
    s = s.strip().replace(",", "")
    mult = 1
    if s.endswith(("K", "k")):
        mult, s = 1_000, s[:-1]
    elif s.endswith(("M", "m")):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("万"):
        mult, s = 10_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def parse_og_description(desc: str) -> dict:
    out = {}
    if m := _FOLLOWERS_RE.search(desc):
        out["follower_count"] = _to_int(m.group(1))
    if m := _POSTS_RE.search(desc):
        out["media_count"] = _to_int(m.group(1))
    return out


def _compact_posts(user: dict) -> list[dict]:
    """从 web_profile_info 的 timeline media（首屏约 12 帖）抽同构 posts。
    完整 30 帖窗口需 graphql 分页（B0-3 后续）；首屏已够内容/赞助/互动初判。"""
    out = []
    edges = ((user.get("edge_owner_to_timeline_media") or {}).get("edges") or [])
    for e in edges:
        n = e.get("node") or {}
        caps = ((n.get("edge_media_to_caption") or {}).get("edges") or [])
        caption = caps[0]["node"]["text"] if caps else ""
        likes = (n.get("edge_liked_by") or n.get("edge_media_preview_like") or {}).get("count")
        out.append({
            "pk": n.get("id"),
            "code": n.get("shortcode"),
            "caption_text": caption,
            "like_count": likes,
            "comment_count": (n.get("edge_media_to_comment") or {}).get("count"),
            "media_type": 2 if n.get("is_video") else 1,
            "product_type": n.get("product_type") or "",
            "play_count": n.get("video_view_count") or 0,
            "taken_at": n.get("taken_at_timestamp"),
            "pinned": bool(n.get("pinned_for_users")),
        })
    return out


def _profile_from_web_info(user: dict) -> dict:
    """把 web_profile_info 的 user 映射到旧 _compact profile 同构字段。"""
    bio_links = []
    for link in (user.get("bio_links") or []):
        if link.get("url"):
            bio_links.append(link["url"])
    ext = user.get("external_url") or ""
    return {
        "posts": _compact_posts(user),
        "handle": user.get("username"),
        "pk": str(user.get("id") or ""),
        "full_name": user.get("full_name") or "",
        "profile_url": f"https://www.instagram.com/{user.get('username')}/",
        "follower_count": (user.get("edge_followed_by") or {}).get("count"),
        "following_count": (user.get("edge_follow") or {}).get("count"),
        "media_count": (user.get("edge_owner_to_timeline_media") or {}).get("count"),
        "is_verified": bool(user.get("is_verified")),
        "is_private": bool(user.get("is_private")),
        "category": user.get("category_name") or user.get("category") or "",
        "biography": user.get("biography") or "",
        "external_url": ext,
        "bio_links": bio_links or ([ext] if ext else []),
        "is_business": bool(user.get("is_business_account")),
        "_source": "browser:web_profile_info",
    }


def fetch_profile(account: str, handle: str, headless: bool = True) -> dict:
    from playwright.sync_api import sync_playwright

    profile_dir = PROFILE_BASE / account
    if not profile_dir.exists():
        sys.exit(f"登录态 profile 不存在：{profile_dir}（先跑 pool_health.py 建池）")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), channel="chrome", headless=headless,
            args=["--no-first-run", "--no-default-browser-check"],
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # 先落地主页，建立 referer 与会话上下文（拟人）
            page.goto(f"https://www.instagram.com/{handle}/",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)

            # 首选：浏览器会话内 fetch web_profile_info（结构化 JSON）
            try:
                data = page.evaluate(
                    """async ({handle, appid}) => {
                        const r = await fetch(
                          `/api/v1/users/web_profile_info/?username=${handle}`,
                          {headers: {'x-ig-app-id': appid}, credentials: 'include'});
                        if (!r.ok) return {__error: 'http_' + r.status};
                        return await r.json();
                    }""",
                    {"handle": handle, "appid": IG_WEB_APP_ID},
                )
            except Exception as e:  # noqa: BLE001
                data = {"__error": f"{type(e).__name__}"}

            user = (data or {}).get("data", {}).get("user")
            if user:
                prof = _profile_from_web_info(user)
                prof["_account_used"] = account
                return prof

            # 回退：og:description 解析（locale 兼容）
            meta = page.query_selector('meta[property="og:description"]')
            desc = meta.get_attribute("content") if meta else ""
            fallback = parse_og_description(desc or "")
            fallback.update({
                "handle": handle,
                "profile_url": f"https://www.instagram.com/{handle}/",
                "_source": "browser:og_description",
                "_account_used": account,
                "_web_info_error": (data or {}).get("__error", "no_user"),
                "_og_raw": desc,
            })
            return fallback
        finally:
            ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="用哪个登录态 profile 采集")
    ap.add_argument("--handle", required=True, help="目标创作者 handle")
    ap.add_argument("--show-head", action="store_true", help="有头模式")
    args = ap.parse_args()
    prof = fetch_profile(args.account, args.handle, headless=not args.show_head)
    # 打印结构化结果（不含任何凭证）
    print(json.dumps(prof, ensure_ascii=False, indent=2))
    return 0 if prof.get("follower_count") is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
