#!/usr/bin/env python3
"""Amazon Finds 导购型红人发现 — 六阶段 Pipeline.

从 Instagram 种子源（品牌账号、Hashtag、Modash CSV）发现候选 creator，
经多层漏斗筛选 + 评论意图分析，输出评分排序结果。

用法：
    .venv/bin/python scripts/discover.py \\
        --username YOUR_USERNAME \\
        --no-proxy \\
        [--modash-search data/modash/search_export.csv] \\
        [--modash-profiles data/modash/profiles_export.csv]
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from account_pool import AccountPool, PoolExhausted, load_pool_accounts  # noqa: E402
from instagram_session import apply_full_cookie_session, sync_client_cookie_jars  # noqa: E402

# ─── 路径 ──────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
SECRETS_FILE = ROOT / ".secrets" / "instagram_accounts.json"
POOL_FILE = ROOT / ".secrets" / "account_pool.json"
SESSION_DIR = ROOT / "data" / "session"
RUNS_DIR = ROOT / "data" / "runs"
CONFIG_DIR = ROOT / "config"
POD_LIBRARY_FILE = ROOT / "data" / "pod_accounts.json"   # 互赞团/水军账号库（持久，跨 run 累积）

# ─── 关键词 & 模式 ─────────────────────────────────────────────────────────────

PRODUCT_REC_KEYWORDS = [
    # Amazon 专属
    "amazon finds", "amazon storefront", "amazon must haves",
    "amazon favorites", "amazon haul", "amazon beauty",
    "amazon", "storefront",
    # 链接/橱窗引导
    "link in bio", "shop my", "in my storefront",
    "linked everything", "linked it", "linking", "linked below",
    "links below", "everything is linked", "everything linked",
    "linked in", "shop the", "shop now", "shop with me",
    "swipe to shop", "tap to shop", "available on", "available at",
    # 联盟/折扣码
    "discount code", "use code", "promo code", "my code",
    "code:", "% off", "affiliate",
    # 带货话术
    "holy grail", "repurchase", "restock",
    "currently using", "my favorites", "must haves", "must have",
    "top picks", "roundup", "recommendation", "recommend",
    "haul", "unboxing", "first impressions", "honest review",
    "obsessed with", "favorite finds", "my picks",
]

AUTHORITY_KEYWORDS: dict[str, list[str]] = {
    "ingredients": [
        "niacinamide", "retinol", "hyaluronic", "vitamin c",
        "peptide", "ceramide", "salicylic", "aha", "bha",
        "glycolic", "lactic acid", "squalane",
    ],
    "device_specs": [
        "wavelength", "nm", "irradiance", "led",
        "red light", "near infrared", "joules", "mw/cm",
    ],
    "skin_science": [
        "skin barrier", "moisture barrier", "collagen",
        "elastin", "cell turnover", "photobiomodulation",
        "dermatologist", "clinical",
    ],
    "conditions": [
        "sensitive skin", "acne", "hyperpigmentation",
        "fine lines", "texture", "rosacea", "eczema",
        "dark spots", "wrinkles", "pores",
    ],
}

SPONSORED_PATTERNS = [
    re.compile(r"#ad\b", re.I),
    re.compile(r"#sponsored\b", re.I),
    re.compile(r"#partner\b", re.I),
    re.compile(r"paid\s+partnership", re.I),
    re.compile(r"\bgifted\b", re.I),
    re.compile(r"\bcollab\b", re.I),
    re.compile(r"brand\s+ambassador", re.I),
    re.compile(r"discount\s+code", re.I),
]

# Link-in-bio 穿透开关（默认关，由 --penetrate-linktree 打开）
PENETRATE_LINKTREE = False

AMAZON_PATTERNS = [
    re.compile(r"amazon\.com/shop/", re.I),
    re.compile(r"amazon\.[a-z.]+/shop/", re.I),
]
LINKINBIO_PATTERNS = [
    re.compile(r"linktr\.ee/", re.I),
    re.compile(r"beacons\.ai/", re.I),
    re.compile(r"(liketoknow\.it|shopltk\.com|ltk\.app)/", re.I),
    re.compile(r"linkin\.bio/", re.I),
    re.compile(r"bio\.site/", re.I),
    re.compile(r"stan\.store/", re.I),
]

BRAND_LIKE_CATEGORIES = {"product/service", "shopping", "brand", "retailer",
                          "grocery store", "local business", "company"}
BRAND_LIKE_BIO_WORDS = ["official", "shop now", "our products", "™", "®",
                         "worldwide shipping", "order now"]

STRONG_INTENT = [
    "ordered", "just bought", "bought this", "in my cart",
    "adding to cart", "buying this",
    "where is the link", "link please", "link in bio",
    "storefront", "which folder", "is it in your",
]
MEDIUM_INTENT = [
    "does this work for", "what wavelength", "sensitive skin",
    "how long", "what size", "which shade", "which one",
    "do you recommend", "worth it", "would you suggest",
    "what's the difference", "how do you use",
    "does it help with", "is it good for",
]
WEAK_INTENT = [
    "need this", "want this", "love this one",
    "code", "discount", "promo", "sale",
    "saving this", "bookmarked",
]

BOT_TEXT_PATTERNS = [
    re.compile(
        r"^(beautiful|love this|nice pic|amazing|gorgeous|so pretty|stunning|wow|fire|goals)[\!\.\s]*$",
        re.I,
    ),
    re.compile(r"^[\U0001F300-\U0001FAFF\U00002702-\U000027B0\s]+$"),   # 纯 emoji
    re.compile(r"^@\w+\s*$"),                                            # 纯 tag
    re.compile(r"^(follow me|check my|dm for|collab\?).*$", re.I),       # spam
]

# 互赞团 / 刷量评论：泛泛吹捧"内容/创作者"本身，而非对产品/话题有真实兴趣。
# 这是 Amazon Finds 型创作者真实社区 vs 刷量社区的关键区分信号。
ENGAGEMENT_POD_PATTERNS = [
    # "love/amazing/great ... content/video/reel/post/creativity/style/presentation"
    re.compile(
        r"\b(love|loving|loved|amazing|great|nice|wonderful|beautiful|awesome|adorable|incredible)\b"
        r".{0,25}\b(content|video|videos|reel|reels|post|posts|feed|creativity|presentation|style|aesthetic|page|profile)\b",
        re.I,
    ),
    # "your content/videos/creativity is so ..." / "content creator"
    re.compile(r"\byour\s+(content|videos|reels|creativity|presentation|style|feed|page)\b", re.I),
    re.compile(r"\bcontent\s+(creator|looks|feels|is\s+(so|really|amazing|great))\b", re.I),
    re.compile(r"\b(amazing|great|talented|fantastic|incredible)\s+(content\s+)?creator\b", re.I),
    # "love how real/engaging/natural your content feels"
    re.compile(r"\b(real|natural|engaging|authentic)\b.{0,20}\b(content|feel|feels)\b", re.I),
    re.compile(r"\bkeep\s+(it\s+up|posting|sharing|going)\b", re.I),
]

# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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


def load_account(username: str) -> dict[str, str]:
    checked: list[Path] = []
    for path in (POOL_FILE, SECRETS_FILE):
        if not path.exists():
            continue
        checked.append(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        for acc in data.get("accounts", []):
            if str(acc.get("username") or "").lower() == username.lower():
                return acc
    if not checked:
        raise SystemExit("本机账号池不存在，请先运行 scripts/import_pool.py")
    raise SystemExit(f"账号 {username} 未找到")


def load_seeds_toml() -> dict[str, Any]:
    """读取 config/seeds.toml（简易 TOML 解析，不引入三方库）。"""
    path = CONFIG_DIR / "seeds.toml"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    result: dict[str, Any] = {}
    current_section = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # 跳过空行和注释
        if not line or line.startswith("#"):
            i += 1
            continue
        # section header
        header = re.match(r"^\[([^\]]+)\]$", line)
        if header:
            current_section = header.group(1)
            parts = current_section.split(".")
            target = result
            for part in parts:
                target = target.setdefault(part, {})
            i += 1
            continue
        # key = value
        kv = re.match(r'^(\w+)\s*=\s*(.*)$', line)
        if not kv:
            i += 1
            continue
        key, raw_val = kv.group(1), kv.group(2).strip()
        # 多行数组：累积到 ] 结束
        if raw_val.startswith("[") and "]" not in raw_val:
            arr_text = raw_val
            i += 1
            while i < len(lines):
                arr_text += "\n" + lines[i]
                if "]" in lines[i]:
                    break
                i += 1
            val = _parse_toml_array(arr_text)
        elif raw_val.startswith("["):
            val = _parse_toml_array(raw_val)
        elif raw_val.replace("_", "").replace(".", "").lstrip("-").isdigit():
            clean = raw_val.replace("_", "")
            val = float(clean) if "." in clean else int(clean)
        elif raw_val.startswith('"'):
            val = raw_val.strip('"')
        else:
            val = raw_val
        # 写入嵌套 dict
        parts = current_section.split(".")
        target = result
        for part in parts:
            if not part:
                break
            target = target.setdefault(part, {})
        target[key] = val
        i += 1
    return result


def _parse_toml_array(text: str) -> list[str]:
    """从 TOML 数组字符串（可能多行）提取值列表。"""
    return re.findall(r'"([^"]*)"', text)


def as_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return value


# ─── Instagram 客户端 ─────────────────────────────────────────────────────────

def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_string.split(";"):
        if not part.strip() or "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def _validate_client(cl: Any) -> str | None:
    """轻量 API 调用验证 session 真的可用，返回检测到的用户名。"""
    try:
        info = cl.account_info()
        return getattr(info, "username", None) or "?"
    except Exception:
        return None


def build_client(username: str, no_proxy: bool = True) -> Any:
    """建立 Instagram 客户端。

    优先级：
    1. 缓存的 session 文件（验证可用则复用）
    2. 完整浏览器 Cookie（同步 private/public Cookie jar）
    3. username/password 登录（最后手段，可能触发挑战）
    """
    from instagrapi import Client

    account = load_account(username)
    settings_file = SESSION_DIR / f"instagrapi-{username}.json"
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    cl = Client()
    if not no_proxy:
        proxy = os.environ.get("IG_PROXY", "")
        if proxy:
            cl.set_proxy(proxy)
            log(f"Proxy: {proxy[:30]}…")

    # ── 1. 复用缓存 session ──
    if settings_file.exists():
        try:
            cl.load_settings(str(settings_file))
            sync_client_cookie_jars(cl)
            detected = _validate_client(cl)
            if detected:
                log(f"✓ 复用缓存 session: {username} (检测: {detected})")
                return cl
            log(f"  缓存 session 失效，尝试 cookie 登录…")
        except Exception:
            log(f"  缓存 session 加载失败，尝试 cookie 登录…")
        cl = Client()  # 重置
        if not no_proxy:
            proxy = os.environ.get("IG_PROXY", "")
            if proxy:
                cl.set_proxy(proxy)

    # ── 2. 完整浏览器 Cookie 恢复 ──
    cookies = parse_cookie_string(account.get("cookie_string", ""))
    sessionid = cookies.get("sessionid", "")
    if sessionid:
        try:
            apply_full_cookie_session(cl, cookies)
            detected = _validate_client(cl)
            if detected:
                cl.dump_settings(str(settings_file))
                os.chmod(settings_file, 0o600)
                log(f"✓ 已登录 (完整 cookie): {username} (检测: {detected})")
                return cl
        except Exception as exc:
            log(f"  cookie 登录失败: {type(exc).__name__}: {str(exc)[:80]}")

    # ── 3. username/password 登录（最后手段）──
    log(f"  尝试 username/password 登录…")
    cl = Client()
    if not no_proxy:
        proxy = os.environ.get("IG_PROXY", "")
        if proxy:
            cl.set_proxy(proxy)
    verification_code = ""
    if account.get("totp_secret"):
        verification_code = totp_now(account["totp_secret"])
    logged_in = cl.login(
        account["username"],
        account["password"],
        verification_code=verification_code,
    )
    if not logged_in:
        raise SystemExit(f"登录失败: {username}（账号可能被风控，请换账号）")
    cl.dump_settings(str(settings_file))
    os.chmod(settings_file, 0o600)
    log(f"✓ 已登录 (密码): {username}")
    return cl


def call_first(cl: Any, method_names: list[str], *args: Any, **kwargs: Any) -> list[Any]:
    """依次尝试多个 API 方法，返回第一个成功的结果。"""
    last_err: Exception | None = None
    for name in method_names:
        fn = getattr(cl, name, None)
        if not fn:
            continue
        try:
            return list(fn(*args, **kwargs) or [])
        except Exception as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err
    return []


def fetch_user_info(cl: Any, handle: str) -> dict[str, Any]:
    """获取用户 profile。

    只用私有 API user_info_by_username_v1（users/{username}/usernameinfo/）。
    不 fallback 到公开端点（web_profile_info），避免 IP 级 429 限流恶化。
    """
    user = cl.user_info_by_username_v1(handle)
    item = as_plain(user)
    bio_links = item.get("bio_links") or []
    if isinstance(bio_links, list):
        bio_links = [
            (link.get("url") or "") if isinstance(link, dict) else str(link)
            for link in bio_links
        ]
    return {
        "pk": str(item.get("pk") or ""),
        "username": item.get("username") or handle,
        "full_name": item.get("full_name") or "",
        "biography": item.get("biography") or "",
        "follower_count": item.get("follower_count") or 0,
        "following_count": item.get("following_count") or 0,
        "media_count": item.get("media_count") or 0,
        "is_private": bool(item.get("is_private")),
        "is_verified": bool(item.get("is_verified")),
        "is_business": bool(item.get("is_business")),
        "category": item.get("category") or item.get("category_name") or "",
        "external_url": str(item.get("external_url") or ""),
        "bio_links": bio_links,
    }


def fetch_user_medias(cl: Any, user_id: str, amount: int) -> list[dict[str, Any]]:
    """获取用户最近帖子，v1 优先，fallback 到 raw API。"""
    try:
        medias = cl.user_medias_v1(str(user_id), amount=amount)
        return [_compact_media(as_plain(m)) for m in medias]
    except Exception:
        try:
            raw = cl.private_request(
                f"feed/user/{user_id}/",
                params={"count": amount, "rank_token": cl.rank_token, "ranked_content": "true"},
            )
            return [_compact_raw_media(item) for item in (raw.get("items") or [])[:amount]]
        except Exception:
            return []


def _compact_media(item: dict[str, Any]) -> dict[str, Any]:
    sponsor_tags = item.get("sponsor_tags") or []
    if isinstance(sponsor_tags, list):
        sponsor_tags = [
            (t.get("username") or "") if isinstance(t, dict) else str(t)
            for t in sponsor_tags
        ]
    return {
        "pk": str(item.get("pk") or ""),
        "code": item.get("code") or "",
        "taken_at": str(item.get("taken_at") or ""),
        "media_type": item.get("media_type"),
        "product_type": item.get("product_type"),
        "like_count": item.get("like_count") or 0,
        "comment_count": item.get("comment_count") or 0,
        "play_count": item.get("play_count") or 0,
        "view_count": item.get("view_count") or 0,
        "caption_text": item.get("caption_text") or "",
        "sponsor_tags": sponsor_tags,
    }


def _compact_raw_media(item: dict[str, Any]) -> dict[str, Any]:
    caption = item.get("caption") or {}
    taken_at = item.get("taken_at") or ""
    if isinstance(taken_at, int | float):
        taken_at = datetime.fromtimestamp(taken_at, timezone.utc).isoformat()
    sponsor_tags = item.get("sponsor_tags") or []
    if isinstance(sponsor_tags, list):
        sponsor_tags = [
            (t.get("username") or "") if isinstance(t, dict) else str(t)
            for t in sponsor_tags
        ]
    return {
        "pk": str(item.get("pk") or item.get("id") or ""),
        "code": item.get("code") or item.get("shortcode") or "",
        "taken_at": str(taken_at),
        "media_type": item.get("media_type"),
        "product_type": item.get("product_type"),
        "like_count": item.get("like_count") or 0,
        "comment_count": item.get("comment_count") or 0,
        "play_count": item.get("play_count") or item.get("ig_play_count") or 0,
        "view_count": item.get("view_count") or item.get("video_view_count") or 0,
        "caption_text": caption.get("text") or item.get("caption_text") or "",
        "sponsor_tags": sponsor_tags,
    }


def fetch_comments(cl: Any, media_pk: str, amount: int) -> list[dict[str, Any]]:
    """获取帖子评论，v1 优先。"""
    try:
        comments = cl.media_comments_v1(str(media_pk), amount=amount)
        result = [_compact_comment(as_plain(c)) for c in comments]
        if len(result) >= amount:
            return result[:amount]
        # fallback 补充
        try:
            fb = cl.media_comments(str(media_pk), amount=amount)
            fb_result = [_compact_comment(as_plain(c)) for c in fb]
            if len(fb_result) > len(result):
                return fb_result[:amount]
        except Exception:
            pass
        return result
    except Exception:
        try:
            comments = cl.media_comments(str(media_pk), amount=amount)
            return [_compact_comment(as_plain(c)) for c in comments][:amount]
        except Exception:
            return []


def _compact_comment(item: dict[str, Any]) -> dict[str, Any]:
    user = item.get("user") or {}
    if isinstance(user, dict):
        username = user.get("username") or ""
    else:
        username = str(user)
    return {
        "pk": str(item.get("pk") or ""),
        "text": item.get("text") or "",
        "username": username,
        "like_count": item.get("like_count") or 0,
        "created_at": str(item.get("created_at_utc") or item.get("created_at") or ""),
    }


# ─── Modash CSV 解析 ──────────────────────────────────────────────────────────

def parse_modash_search_csv(path: str) -> list[dict[str, Any]]:
    """解析 Modash 搜索导出 CSV → 候选 username 列表 + 附带数据。

    Modash 导出 CSV 列名可能有多种变体（Username/username/Handle），
    这里做宽松匹配。
    """
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            log(f"⚠ Modash search CSV 为空: {path}")
            return []
        # 建立列名映射（小写 → 原始列名）
        col_map = {name.lower().strip(): name for name in reader.fieldnames}
        username_key = _find_col(col_map, ["username", "handle", "instagram handle",
                                             "ig handle", "profile", "name"])
        followers_key = _find_col(col_map, ["followers", "follower count",
                                              "followers count", "followercount"])
        er_key = _find_col(col_map, ["engagement rate", "engagementrate", "er",
                                       "avg engagement rate", "er %"])
        credibility_key = _find_col(col_map, ["credibility", "audience credibility",
                                                "credibility score", "fake followers"])

        if not username_key:
            log(f"⚠ Modash CSV 找不到 username 列。列名: {list(reader.fieldnames)}")
            return []

        for row in reader:
            handle = (row.get(username_key) or "").strip().lstrip("@").lower()
            if not handle:
                continue
            entry: dict[str, Any] = {"username": handle, "source": "modash_search"}
            if followers_key:
                entry["modash_followers"] = _safe_int(row.get(followers_key))
            if er_key:
                entry["modash_er"] = _safe_float(row.get(er_key))
            if credibility_key:
                entry["modash_credibility"] = _safe_float(row.get(credibility_key))
            rows.append(entry)
    log(f"  Modash search CSV → {len(rows)} 个候选")
    return rows


def parse_modash_profiles_csv(path: str) -> dict[str, dict[str, Any]]:
    """解析 Modash profile report 导出 CSV → {username: 增强数据}。"""
    result: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return {}
        col_map = {name.lower().strip(): name for name in reader.fieldnames}

        username_key = _find_col(col_map, ["username", "handle", "instagram handle"])
        credibility_key = _find_col(col_map, ["credibility", "audience credibility",
                                                "credibility score"])
        fake_key = _find_col(col_map, ["fake followers", "fake followers %",
                                         "suspicious followers"])
        er_key = _find_col(col_map, ["engagement rate", "engagementrate", "er"])
        avg_likes_key = _find_col(col_map, ["avg likes", "average likes", "avg. likes"])
        avg_comments_key = _find_col(col_map, ["avg comments", "average comments"])
        avg_reels_key = _find_col(col_map, ["avg reels plays", "avg. reels plays",
                                              "average reels plays"])
        us_key = _find_col(col_map, ["us audience", "us %", "united states",
                                       "audience us", "us audience %"])
        female_key = _find_col(col_map, ["female", "female %", "female audience",
                                           "audience female"])
        age_key = _find_col(col_map, ["18-24", "18-34", "age 18-34", "age 18-24"])

        if not username_key:
            log(f"⚠ Modash profiles CSV 找不到 username 列。列名: {list(reader.fieldnames)}")
            return {}

        for row in reader:
            handle = (row.get(username_key) or "").strip().lstrip("@").lower()
            if not handle:
                continue
            data: dict[str, Any] = {}
            if credibility_key:
                data["modash_credibility"] = _safe_float(row.get(credibility_key))
            if fake_key:
                val = _safe_float(row.get(fake_key))
                if val is not None:
                    # 可能是百分比或小数
                    data["modash_fake_pct"] = val / 100 if val > 1 else val
            if er_key:
                data["modash_er"] = _safe_float(row.get(er_key))
            if avg_likes_key:
                data["modash_avg_likes"] = _safe_int(row.get(avg_likes_key))
            if avg_comments_key:
                data["modash_avg_comments"] = _safe_int(row.get(avg_comments_key))
            if avg_reels_key:
                data["modash_avg_reels_plays"] = _safe_int(row.get(avg_reels_key))
            if us_key:
                data["modash_audience_us_pct"] = _safe_float(row.get(us_key))
            if female_key:
                data["modash_audience_female_pct"] = _safe_float(row.get(female_key))
            if age_key:
                data["modash_audience_age_18_34_pct"] = _safe_float(row.get(age_key))
            if data:
                result[handle] = data
    log(f"  Modash profiles CSV → {len(result)} 个 profile 增强数据")
    return result


def _find_col(col_map: dict[str, str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c.lower() in col_map:
            return col_map[c.lower()]
    return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return None


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        s = str(val).replace(",", "").replace("%", "").replace(" ", "").strip()
        if not s:
            return None
        v = float(s)
        return v
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 阶段 1: Seed 采集 → 候选池
# ═══════════════════════════════════════════════════════════════════════════════

def stage1_collect_seeds(
    cl: Any,
    brand_handles: list[str],
    hashtags: list[str],
    per_source: int,
    sleep_sec: int,
    modash_search_path: str | None,
    use_hashtags: bool = False,
    keyword_queries: list[str] | None = None,
    use_keyword_search: bool = False,
    lookalike_seeds: list[str] | None = None,
    use_lookalike: bool = False,
    use_brand_engagement: bool = False,
) -> dict[str, dict[str, Any]]:
    """从品牌账号、Hashtag、Modash CSV 三类种子源收集候选池。

    use_hashtags: 默认 False。实测多数采集账号调用 hashtag 端点会触发
    login_required，且该错误会软封整个 session（导致后续所有私有请求失效）。
    因此默认跳过。仅在确认账号有 hashtag 权限时才开启。
    """
    log("═══ 阶段 1: Seed 采集 ═══")
    candidates: dict[str, dict[str, Any]] = {}   # handle → info
    brand_set = {h.lower() for h in brand_handles}
    errors: list[str] = []

    # ── 1a. 品牌账号 tagged/mentioned creators（主力渠道）──
    # usertag_medias 返回"品牌被 tag 的帖子"，这些帖子作者正是合作过的 creator。
    # 这是这些采集账号唯一稳定可用的发现渠道，所以多采一些。
    brand_tagged_amount = max(per_source * 2, 30)
    for handle in brand_handles:
        log(f"  品牌账号: @{handle}")
        try:
            user = cl.user_info_by_username_v1(handle)
            user_data = as_plain(user)
            user_id = str(user_data.get("pk") or "")
            # 品牌被 tag 的帖子 → 提取作者作为候选
            try:
                tagged = call_first(cl, ["usertag_medias_v1", "usertag_medias"],
                                    user_id, amount=brand_tagged_amount)
                tagged_authors = 0
                for media in tagged:
                    m = as_plain(media)
                    author = (m.get("user") or {})
                    if isinstance(author, dict):
                        author_handle = (author.get("username") or "").lower()
                    else:
                        author_handle = ""
                    if author_handle and author_handle not in brand_set:
                        _add_candidate(candidates, author_handle,
                                       f"brand_tagged:{handle}")
                        tagged_authors += 1
                log(f"    被 tag 帖子 → {len(tagged)} 条, {tagged_authors} 个作者候选")
            except Exception as exc:
                errors.append(f"brand_tagged:{handle} → {type(exc).__name__}: {str(exc)[:60]}")
            time.sleep(sleep_sec)

            # 品牌自己帖子 caption 中 mention 的 creator
            try:
                medias = fetch_user_medias(cl, user_id, per_source)
                mention_count = 0
                for media in medias:
                    caption = media.get("caption_text") or ""
                    mentions = re.findall(r"@([A-Za-z0-9_.]+)", caption)
                    for m_handle in mentions:
                        m_handle = m_handle.lower().rstrip(".")
                        if m_handle and m_handle not in brand_set:
                            _add_candidate(candidates, m_handle,
                                           f"brand_mention:{handle}")
                            mention_count += 1
                log(f"    品牌帖子 → {len(medias)} 条, {mention_count} 个 mention 候选")
            except Exception as exc:
                errors.append(f"brand_posts:{handle} → {type(exc).__name__}: {str(exc)[:60]}")
            time.sleep(sleep_sec)
        except Exception as exc:
            errors.append(f"brand:{handle} → {type(exc).__name__}: {str(exc)[:60]}")

    # ── 1b. Hashtag top/recent feed（默认跳过）──
    # 注意：实测采集账号调用 hashtag 端点返回 login_required，且会软封 session，
    # 导致后续所有私有请求（含 Stage 2 的候选查询）全部失效。
    # 因此默认完全跳过。仅 --use-hashtags 时尝试，且放在品牌采集之后。
    hashtag_disabled = False
    if not use_hashtags and hashtags:
        log(f"  ⊘ 跳过 {len(hashtags)} 个 hashtag（账号无 hashtag 权限，调用会软封 session）")
        log(f"    如需启用：加 --use-hashtags（仅在确认账号有权限时）")
    for tag in (hashtags if use_hashtags else []):
        if hashtag_disabled:
            break
        log(f"  Hashtag: #{tag}")
        got_any = False
        for method, label in [("hashtag_medias_top", "top"), ("hashtag_medias_recent", "recent")]:
            try:
                posts = call_first(cl, [method], tag, amount=per_source)
                for media in posts:
                    m = as_plain(media)
                    author = m.get("user") or {}
                    if isinstance(author, dict):
                        author_handle = (author.get("username") or "").lower()
                    else:
                        author_handle = ""
                    if author_handle and author_handle not in brand_set:
                        _add_candidate(candidates, author_handle, f"hashtag_{label}:{tag}")
                log(f"    {label} → {len(posts)} 条帖子")
                got_any = True
            except Exception as exc:
                msg = str(exc)
                errors.append(f"hashtag_{label}:{tag} → {type(exc).__name__}: {msg[:50]}")
                if "login_required" in msg.lower():
                    hashtag_disabled = True
            time.sleep(sleep_sec)
        if hashtag_disabled and not got_any:
            log(f"  ⚠ Hashtag 端点不可用 (login_required)，跳过剩余 hashtag")

    # ── 1c. 关键词用户搜索（私有 search_users，相当于 bio/关键词搜索，种子无关）──
    # 突破点：这是 hashtag 之外、账号可用的"按垂类找人"端点，直接定位护肤创作者
    if use_keyword_search and keyword_queries:
        log(f"  关键词搜索（search_users）: {len(keyword_queries)} 个词")
        for q in keyword_queries:
            try:
                res = cl.search_users(q)
                n = _add_usershorts(candidates, res, brand_set, f"search:{q}")
                log(f"    '{q}' → {n} 个候选")
            except Exception as exc:
                errors.append(f"search:{q} → {type(exc).__name__}: {str(exc)[:50]}")
            time.sleep(sleep_sec)

    # ── 1d. Lookalike 扩展（fbsearch_suggested_profiles + chaining，从对口种子找相似）──
    # 突破点：Instagram 自家"相似账号"图谱，对口种子→同垂类 lookalike（每个 ~78 个）
    if use_lookalike and lookalike_seeds:
        log(f"  Lookalike 扩展: {len(lookalike_seeds)} 个种子")
        for seed in lookalike_seeds:
            seed = seed.lower().lstrip("@").strip()
            if not seed:
                continue
            try:
                su = cl.user_info_by_username_v1(seed)
                spk = str(as_plain(su).get("pk") or "")
                time.sleep(sleep_sec)
                try:
                    sug = cl.fbsearch_suggested_profiles(spk)
                    n = _add_usershorts(candidates, sug, brand_set, f"lookalike:{seed}")
                    log(f"    @{seed} 相似账号 → {n} 个候选")
                except Exception as exc:
                    errors.append(f"lookalike:{seed} → {type(exc).__name__}: {str(exc)[:40]}")
                time.sleep(sleep_sec)
                try:
                    ch = cl.chaining(spk)
                    ch_users = (ch or {}).get("users") if isinstance(ch, dict) else None
                    n2 = _add_usershorts(candidates, ch_users, brand_set, f"chaining:{seed}")
                    if n2:
                        log(f"    @{seed} chaining → {n2} 个候选")
                except Exception:
                    pass
                time.sleep(sleep_sec)
            except Exception as exc:
                errors.append(f"lookalike_seed:{seed} → {type(exc).__name__}: {str(exc)[:40]}")

    # ── 1e. 品牌互动挖掘（点赞者/评论者，护肤品牌的互动者偏护肤垂类）──
    if use_brand_engagement:
        log(f"  品牌互动挖掘（media_likers）")
        for handle in brand_handles:
            try:
                bu = cl.user_info_by_username_v1(handle)
                bpk = str(as_plain(bu).get("pk") or "")
                medias = fetch_user_medias(cl, bpk, 2)
                for m in medias[:2]:
                    mid = m.get("pk") or ""
                    if not mid:
                        continue
                    try:
                        likers = cl.media_likers(mid)
                        n = _add_usershorts(candidates, likers, brand_set, f"brand_liker:{handle}")
                        log(f"    @{handle} 帖 {m.get('code','?')} 点赞者 → {n} 个候选")
                    except Exception as exc:
                        errors.append(f"likers:{handle} → {type(exc).__name__}: {str(exc)[:40]}")
                    time.sleep(sleep_sec)
            except Exception as exc:
                errors.append(f"brand_engagement:{handle} → {type(exc).__name__}: {str(exc)[:40]}")

    # ── 1f. Modash Search CSV ──
    if modash_search_path:
        log(f"  Modash Search CSV: {modash_search_path}")
        modash_rows = parse_modash_search_csv(modash_search_path)
        for row in modash_rows:
            handle = row["username"]
            if handle not in brand_set:
                _add_candidate(candidates, handle, "modash_search")
                # 附加 Modash 搜索时的预筛数据
                if "modash_er" in row:
                    candidates[handle].setdefault("modash_search_er", row["modash_er"])
                if "modash_credibility" in row:
                    candidates[handle].setdefault("modash_search_credibility",
                                                   row["modash_credibility"])

    # ── 汇总 ──
    log(f"  候选池: {len(candidates)} 个（去重后）")
    if errors:
        log(f"  采集错误: {len(errors)} 条")
        for e in errors[:5]:
            log(f"    - {e}")
    return candidates


def _add_candidate(pool: dict[str, dict[str, Any]], handle: str, source: str) -> None:
    handle = handle.lower().strip()
    if not handle:
        return
    if handle not in pool:
        pool[handle] = {"handle": handle, "discovery_sources": []}
    if source not in pool[handle]["discovery_sources"]:
        pool[handle]["discovery_sources"].append(source)


def _add_usershorts(pool: dict[str, dict[str, Any]], items: Any,
                    brand_set: set[str], source: str) -> int:
    """把 UserShort 列表（search_users/fbsearch/likers 返回）加入候选池。"""
    n = 0
    for it in (items or []):
        d = as_plain(it)
        h = (d.get("username") or "").lower() if isinstance(d, dict) else ""
        if h and h not in brand_set:
            before = h in pool
            _add_candidate(pool, h, source)
            if not before:
                n += 1
    return n


# ═══════════════════════════════════════════════════════════════════════════════
# 阶段 2: Profile + Posts 回扫
# ═══════════════════════════════════════════════════════════════════════════════

def stage2_scan_profiles(
    cl: Any,
    candidates: dict[str, dict[str, Any]],
    candidate_posts: int,
    sleep_sec: int,
) -> dict[str, dict[str, Any]]:
    """对每个候选者采集 profile 和最近 N 条 posts。"""
    log(f"═══ 阶段 2: Profile + Posts 回扫 ({len(candidates)} 个) ═══")
    enriched: dict[str, dict[str, Any]] = {}
    total = len(candidates)

    for idx, (handle, cand) in enumerate(candidates.items(), 1):
        log(f"  [{idx}/{total}] @{handle}")
        try:
            profile = fetch_user_info(cl, handle)
            if profile.get("is_private"):
                log(f"    ⊘ 私密账号，跳过")
                cand.update(profile)
                cand["status"] = "exclude"
                cand["filter_reasons"] = ["private_account"]
                cand["posts"] = []
                enriched[handle] = cand
                continue

            cand.update(profile)
            # ── 早筛（全量优化 + 橱窗硬门槛）──
            # profile 便宜(1 请求)，先查橱窗；无 Amazon/橱窗链接的号直接出局，
            # 省掉对它的 medias 抓取 + 评论分析（全量时这是大头）。仅有橱窗的号才深扫。
            bio = _check_bio_links(cand)
            cand.update(bio)
            if bio.get("has_amazon_storefront") is False:
                log(f"    ⊘ 无 Amazon/橱窗链接，跳过深扫")
                cand["status"] = "exclude"
                cand.setdefault("filter_reasons", []).append("no_amazon_storefront")
                cand["posts"] = []
                enriched[handle] = cand
                time.sleep(sleep_sec)
                continue

            posts = fetch_user_medias(cl, profile["pk"], candidate_posts)
            log(f"    ✓ {profile['follower_count']} followers, {len(posts)} posts · 橱窗={bio.get('has_amazon_storefront')}")
            cand["posts"] = posts
            enriched[handle] = cand
        except PoolExhausted as exc:
            log(f"    ✗ 账号池耗尽，停止扫描: {exc}")
            # 剩余未扫描的候选保持原状（不标记 error）
            for rem_handle, rem_cand in candidates.items():
                if rem_handle not in enriched:
                    enriched[rem_handle] = rem_cand
            break
        except Exception as exc:
            log(f"    ✗ 采集失败: {type(exc).__name__}: {exc}")
            cand["status"] = "error"
            cand["error"] = f"{type(exc).__name__}: {exc}"
            cand["posts"] = []
            enriched[handle] = cand
        time.sleep(sleep_sec)

    log(f"  回扫完成: 成功 {sum(1 for c in enriched.values() if c.get('status') != 'error')},"
        f" 失败 {sum(1 for c in enriched.values() if c.get('status') == 'error')}")
    return enriched


# ═══════════════════════════════════════════════════════════════════════════════
# 阶段 3: 多层漏斗筛选
# ═══════════════════════════════════════════════════════════════════════════════

def stage3_funnel(
    candidates: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """六层漏斗筛选。"""
    log(f"═══ 阶段 3: 漏斗筛选 ═══")

    min_followers = cfg.get("min_followers", 10_000)
    max_followers = cfg.get("max_followers", 150_000)
    max_sponsored_ratio = cfg.get("max_sponsored_ratio", 0.40)
    min_product_rec_ratio = cfg.get("min_product_rec_ratio", 0.20)
    benchmarks = cfg.get("engagement_benchmarks", {})

    counts = defaultdict(int)

    for handle, cand in candidates.items():
        if cand.get("status") in ("exclude", "error"):
            counts["already_excluded"] += 1
            continue

        cand.setdefault("filter_reasons", [])
        cand.setdefault("review_reasons", [])

        # ── 3.1 基础过滤 ──
        followers = cand.get("follower_count") or 0
        if followers < min_followers or followers > max_followers:
            cand["status"] = "exclude"
            cand["filter_reasons"].append(f"followers_out_of_range:{followers}")
            counts["followers_out_of_range"] += 1
            continue

        if _is_brand_like(cand, cfg.get("brand_handles", [])):
            cand["status"] = "exclude"
            cand["filter_reasons"].append("brand_like")
            counts["brand_like"] += 1
            continue

        if not _is_active(cand):
            cand["status"] = "exclude"
            cand["filter_reasons"].append("inactive")
            counts["inactive"] += 1
            continue

        # 设置 tier
        cand["tier"] = "micro" if followers < 100_000 else "mid"

        # ── 3.2 Bio 基础设施检测（Amazon Storefront = 客户硬性门槛）──
        bio_result = _check_bio_links(cand)
        cand.update(bio_result)
        has_amazon = bio_result.get("has_amazon_storefront")
        if has_amazon is False:
            # 没有任何 Amazon storefront 链接（other/none）→ 不满足硬性要求
            cand["status"] = "exclude"
            cand["filter_reasons"].append("no_amazon_storefront")
            counts["no_amazon_storefront"] += 1
            continue
        # has_amazon == True 直接通过；== "unverified"（link-in-bio 未穿透）在 3.6 标 review

        # ── 3.3 内容画像（软信号：不硬性 exclude）──
        # 客户要求"产品推荐占比低 → 降级或 exclude"。已有 Amazon storefront 的候选
        # 即使近期 caption 产品密度低，也保留为 review 交人工判断，不直接排除。
        content_result = _analyze_content(cand)
        cand.update(content_result)
        cand.update(_detect_niche(cand))   # 赛道/垂类 + 对口度
        if content_result["creator_archetype"] == "lifestyle":
            cand["review_reasons"].append("low_product_content")
        if cand.get("campaign_fit") == "off":
            cand["review_reasons"].append("off_vertical")

        # ── 3.4 赞助饱和度 ──
        sponsor_result = _check_sponsorship(cand)
        cand.update(sponsor_result)
        if sponsor_result["sponsored_ratio"] > max_sponsored_ratio:
            cand["status"] = "exclude"
            cand["filter_reasons"].append("ad_saturated")
            counts["ad_saturated"] += 1
            continue
        if sponsor_result["sponsored_ratio"] > 0.30:
            cand["review_reasons"].append("high_sponsorship")

        # ── 3.5 互动率 ──
        er_result = _calc_engagement(cand, benchmarks)
        cand.update(er_result)
        if not er_result["meets_er_benchmark"]:
            if er_result.get("reels_engagement_rate", 0) == 0 and er_result.get("static_engagement_rate", 0) == 0:
                cand["status"] = "exclude"
                cand["filter_reasons"].append("no_engagement_data")
                counts["no_engagement_data"] += 1
                continue
            else:
                cand["review_reasons"].append("low_engagement")

        # ── 3.6 综合判定 ──
        if cand.get("has_amazon_storefront") == "unverified":
            cand["review_reasons"].append("linkinbio_needs_manual_check")

        if cand.get("review_reasons"):
            cand["status"] = "review"
        else:
            cand["status"] = "include"
        counts[cand["status"]] += 1

    log(f"  漏斗结果:")
    for reason, count in sorted(counts.items()):
        log(f"    {reason}: {count}")
    return candidates


def _is_brand_like(cand: dict[str, Any], brand_handles: list[str]) -> bool:
    """判断是否为品牌/官方号。"""
    handle = (cand.get("handle") or "").lower()
    name = (cand.get("full_name") or "").lower()
    bio = (cand.get("biography") or "").lower()
    category = (cand.get("category") or "").lower()

    # 类目检查
    if category and any(bc in category for bc in BRAND_LIKE_CATEGORIES):
        return True
    # Bio 关键词
    if any(word in bio for word in BRAND_LIKE_BIO_WORDS):
        return True
    # handle 或名字含品牌词
    brand_set = {b.lower() for b in brand_handles}
    for b in brand_set:
        if b in handle or b in name:
            return True
    return False


def _is_active(cand: dict[str, Any]) -> bool:
    """最近 30 天是否发过帖。"""
    posts = cand.get("posts") or []
    if not posts:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for post in posts:
        taken_str = post.get("taken_at") or ""
        try:
            taken = datetime.fromisoformat(taken_str.replace("Z", "+00:00"))
            if taken.tzinfo is None:
                taken = taken.replace(tzinfo=timezone.utc)
            if taken > cutoff:
                return True
        except (ValueError, TypeError):
            continue
    return False


def _check_bio_links(cand: dict[str, Any]) -> dict[str, Any]:
    """检测 bio 链接类型和 Amazon Storefront。

    关键：instagrapi 的 bio_links 常同时包含聚合工具链接（linktr.ee）和展开后的
    Amazon 直链。必须先扫描所有 url 找直链，再退回 link-in-bio 穿透，否则会因为
    linktr.ee 排在前面而漏掉同一列表里的 amazon.com/shop。
    """
    result = {
        "has_amazon_storefront": False,
        "bio_link_type": "none",
        "bio_link_url": "",
    }
    # 收集所有候选 url（external_url + bio_links），去重保序
    raw_urls: list[str] = []
    ext_url = cand.get("external_url") or ""
    if ext_url:
        raw_urls.append(ext_url)
    raw_urls.extend(cand.get("bio_links") or [])
    seen: set[str] = set()
    urls: list[str] = []
    for u in raw_urls:
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    # ── Pass 1: 任意 url 直链 amazon.com/shop ──
    for url in urls:
        for pat in AMAZON_PATTERNS:
            if pat.search(url.lower()):
                result["has_amazon_storefront"] = True
                result["bio_link_type"] = "amazon_direct"
                result["bio_link_url"] = url
                return result

    # ── Pass 2: bio 文本含 amazon ──
    bio = (cand.get("biography") or "").lower()
    if "amazon.com/shop" in bio or "amazon storefront" in bio:
        result["has_amazon_storefront"] = True
        result["bio_link_type"] = "amazon_in_bio_text"
        return result

    # ── Pass 3: link-in-bio 工具 → 穿透检测（可选）──
    types = ["linktree", "beacons", "ltk", "linkin_bio", "bio_site", "stan_store"]
    for i, pat in enumerate(LINKINBIO_PATTERNS):
        for url in urls:
            if pat.search(url.lower()):
                result["bio_link_type"] = types[i] if i < len(types) else "other_linkinbio"
                result["bio_link_url"] = url
                # 穿透默认关闭：linktr.ee/beacons 等对 bot 返回 429/403，不可靠。
                # 标 unverified 交 Modash/人工确认；开 --penetrate-linktree 时才尝试。
                if PENETRATE_LINKTREE and _linktree_has_amazon(url):
                    result["has_amazon_storefront"] = True
                else:
                    result["has_amazon_storefront"] = "unverified"
                return result

    # ── Pass 4: 其他链接 ──
    if urls:
        result["bio_link_type"] = "other"
        result["bio_link_url"] = urls[0]
    return result


def _linktree_has_amazon(url: str) -> bool:
    """访问 link-in-bio 页面，检查是否含 Amazon 链接。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore").lower()
            return "amazon.com/shop" in html
    except Exception:
        return False


AUTHORITY_LABELS = {"ingredients": "护肤成分", "device_specs": "设备规格",
                    "skin_science": "皮肤科学", "conditions": "适用场景"}
AUTHORITY_WEIGHTS = {"ingredients": 3, "device_specs": 4, "skin_science": 3, "conditions": 2}

# ─── 赛道/垂类检测 ──────────────────────────────────────────────────────────────
# fit: core=对口本次护肤设备活动 / related=相关可考虑 / off=跨垂类（与活动不符）
NICHE_DEFS = {
    "skincare":      {"label": "护肤", "fit": "core", "kw": ["skincare", "skin care", "skin barrier",
                       "serum", "retinol", "niacinamide", "spf", "acne", "glow skin", "dermat", "esthetician",
                       "hyperpigmentation", "anti-aging", "antiaging", "glass skin", "skincare routine"]},
    "beauty_device": {"label": "美容仪器", "fit": "core", "kw": ["led mask", "led face", "red light",
                       "redlight", "light therapy", "currentbody", "solawave", "therabody", "microcurrent",
                       "wavelength", "irradiance", "near infrared", "beauty device", "skincare device"]},
    "beauty_makeup": {"label": "美妆", "fit": "core", "kw": ["makeup", "make up", "beauty", "grwm", "mua",
                       "lipstick", "foundation", "mascara", "cosmetic", "glam", "eyeshadow", "blush", "beauty tips"]},
    "haircare":      {"label": "美发", "fit": "related", "kw": ["hair", "haircare", "hairstyle", "curls",
                       "blonde", "balayage", "hairtok", "hair gloss", "hair routine"]},
    "fashion":       {"label": "时尚", "fit": "related", "kw": ["fashion", "outfit", "ootd", "style", "wardrobe",
                       "midsize", "try on", "tryon", "lookbook", "what i wore", "stylist", "fashionista"]},
    "wellness":      {"label": "健康/健身", "fit": "related", "kw": ["fitness", "gym", "workout", "lift",
                       "pilates", "yoga", "wellness", "nutrition", "health coach", "self care", "selfcare"]},
    "parenting":     {"label": "母婴/育儿", "fit": "related", "kw": ["toddler mom", "mum of", "mama of",
                       "toddler", "parenting", "motherhood", "newborn", "mom of", "boy mom", "girl mom"]},
    "lifestyle":     {"label": "生活方式", "fit": "related", "kw": ["lifestyle", "daily vlog", "day in my life",
                       "home", "cozy", "aesthetic", "slow living", "routine"]},
    "finance":       {"label": "理财/金融", "fit": "off", "kw": ["money", "finance", "invest", "investor",
                       "credit card", "credit cards", "wealth", "budget", "stocks", "miles", "points", "fintech"]},
    "food":          {"label": "美食", "fit": "off", "kw": ["recipe", "foodie", "cooking", "baking", "eats",
                       "meal", "food blog", "what i eat", "restaurant"]},
    "travel":        {"label": "旅行", "fit": "off", "kw": ["travel", "wanderlust", "trip", "destination",
                       "vacation", "explore the world", "digital nomad"]},
}
FIT_LABEL = {"core": "对口", "related": "相关", "off": "跨垂类"}


def _detect_niche(cand: dict[str, Any]) -> dict[str, Any]:
    """从 bio + caption 检测创作者赛道（主/次）+ 对本次活动的对口度。

    bio 命中权重高于 caption。返回 niche_primary/secondary、对口度 campaign_fit、
    以及可审计的 niche_breakdown（每个赛道命中词）。
    """
    bio = (cand.get("biography") or "").lower()
    posts = cand.get("posts") or []
    caps = " ".join((p.get("caption_text") or "") for p in posts).lower()

    scored = []
    for niche, d in NICHE_DEFS.items():
        bio_hits = [kw for kw in d["kw"] if kw in bio]
        cap_hits = [kw for kw in d["kw"] if kw in caps]
        score = len(bio_hits) * 3 + len(cap_hits)   # bio 权重高
        if score > 0:
            scored.append({
                "niche": niche, "label": d["label"], "fit": d["fit"], "score": score,
                "hits": sorted(set(bio_hits + cap_hits))[:8],
            })
    scored.sort(key=lambda x: -x["score"])

    if not scored:
        return {"niche_primary": "unknown", "niche_primary_label": "未识别",
                "niche_secondary": [], "campaign_fit": "unknown",
                "campaign_fit_label": "未识别", "niche_breakdown": []}

    primary = scored[0]
    secondary = [s["label"] for s in scored[1:4] if s["score"] >= max(1, primary["score"] * 0.3)]

    # 对口度：有明显核心赛道信号 → core；否则看主赛道 fit
    has_core = any(s["fit"] == "core" and s["score"] >= 2 for s in scored)
    if primary["fit"] == "core" or has_core:
        fit = "core"
    elif primary["fit"] == "related":
        fit = "related"
    else:
        # 主赛道是 off，但若有 related 信号则降为 related，否则 off
        fit = "related" if any(s["fit"] == "related" and s["score"] >= 2 for s in scored) else "off"

    return {
        "niche_primary": primary["niche"],
        "niche_primary_label": primary["label"],
        "niche_secondary": secondary,
        "campaign_fit": fit,
        "campaign_fit_label": FIT_LABEL.get(fit, fit),
        "niche_breakdown": scored[:6],
    }


def _analyze_content(cand: dict[str, Any]) -> dict[str, Any]:
    """分析内容画像：产品推荐比例 + 权威度评分（含可审计的计算依据）。"""
    posts = cand.get("posts") or []
    if not posts:
        return {
            "creator_archetype": "unknown",
            "product_rec_ratio": 0,
            "skincare_authority_score": 0,
            "authority_evidence": [],
            "product_rec_breakdown": {},
            "authority_breakdown": {},
        }

    # 产品推荐比例：逐帖匹配 PRODUCT_REC_KEYWORDS，记录命中的词作为证据
    rec_count = 0
    rec_matched_kw: set[str] = set()
    for post in posts:
        caption = (post.get("caption_text") or "").lower()
        hit = [kw for kw in PRODUCT_REC_KEYWORDS if kw in caption]
        if hit:
            rec_count += 1
            rec_matched_kw.update(hit)
    ratio = rec_count / len(posts) if posts else 0

    if ratio >= 0.40:
        archetype = "amazon_finds"
    elif ratio >= 0.20:
        archetype = "mixed"
    else:
        archetype = "lifestyle"

    product_rec_breakdown = {
        "formula": "含产品推荐词的帖数 / 总帖数",
        "rec_posts": rec_count,
        "total_posts": len(posts),
        "math": f"{rec_count} / {len(posts)} = {round(ratio, 3)}",
        "matched_keywords": sorted(rec_matched_kw)[:20],
        "thresholds": "≥0.40 amazon_finds · ≥0.20 mixed · <0.20 lifestyle",
    }

    # 权威度评分：4 类目关键词命中 × 权重，求和 ×5 封顶 100
    all_captions = " ".join((p.get("caption_text") or "") for p in posts).lower()
    evidence: list[str] = []
    cat_breakdown = []
    raw_score = 0
    for category, keywords in AUTHORITY_KEYWORDS.items():
        hits = [kw for kw in keywords if kw in all_captions]
        evidence.extend(hits)
        w = AUTHORITY_WEIGHTS.get(category, 1)
        contrib = len(hits) * w
        raw_score += contrib
        cat_breakdown.append({
            "category": category, "label": AUTHORITY_LABELS.get(category, category),
            "weight": w, "hits": hits, "count": len(hits), "contribution": contrib,
        })
    authority_score = min(100, int(raw_score * 5))
    math_terms = " + ".join(f"{c['count']}×{c['weight']}" for c in cat_breakdown if c["count"])
    authority_breakdown = {
        "formula": "Σ(各类目命中词数 × 类目权重) × 5，封顶 100",
        "categories": cat_breakdown,
        "raw_score": raw_score,
        "math": f"({math_terms or 0}) × 5 = {raw_score * 5} → 封顶 {authority_score}",
        "weights_note": "成分×3 · 设备规格×4 · 皮肤科学×3 · 适用场景×2（设备规格权重最高，呼应红光/LED 设备专业度）",
    }

    return {
        "creator_archetype": archetype,
        "product_rec_ratio": round(ratio, 3),
        "skincare_authority_score": authority_score,
        "authority_evidence": sorted(set(evidence)),
        "product_rec_breakdown": product_rec_breakdown,
        "authority_breakdown": authority_breakdown,
    }


def _check_sponsorship(cand: dict[str, Any]) -> dict[str, Any]:
    """检测赞助帖比例。双信号源：caption 文本 + sponsor_tags。"""
    posts = cand.get("posts") or []
    scan_posts = posts[:15]  # 只看最近 15 条
    if not scan_posts:
        return {
            "sponsored_count": 0,
            "sponsored_ratio": 0,
            "organic_amazon_posts_count": 0,
        }

    sponsored_count = 0
    organic_rec_count = 0
    for post in scan_posts:
        caption = post.get("caption_text") or ""
        is_sponsored = False

        # 信号源 1: caption 匹配
        for pat in SPONSORED_PATTERNS:
            if pat.search(caption):
                is_sponsored = True
                break

        # 信号源 2: sponsor_tags
        if not is_sponsored and post.get("sponsor_tags"):
            is_sponsored = True

        if is_sponsored:
            sponsored_count += 1
        else:
            # 非赞助的产品推荐帖
            caption_lower = caption.lower()
            if any(kw in caption_lower for kw in PRODUCT_REC_KEYWORDS):
                organic_rec_count += 1

    ratio = sponsored_count / len(scan_posts)
    return {
        "sponsored_count": sponsored_count,
        "sponsored_ratio": round(ratio, 3),
        "organic_amazon_posts_count": organic_rec_count,
    }


def _calc_engagement(cand: dict[str, Any], benchmarks: dict[str, Any]) -> dict[str, Any]:
    """按 Reels/Static 分别计算互动率，对比基准。"""
    posts = cand.get("posts") or []
    followers = cand.get("follower_count") or 1
    tier = cand.get("tier", "micro")

    reels: list[dict] = []
    statics: list[dict] = []
    for p in posts:
        mt = p.get("media_type")
        pt = p.get("product_type") or ""
        if mt == 2 or "clip" in str(pt).lower() or "reel" in str(pt).lower():
            reels.append(p)
        else:
            statics.append(p)

    def _er(post_list: list[dict]) -> tuple[float, float, float]:
        if not post_list:
            return 0.0, 0.0, 0.0
        total_likes = sum(p.get("like_count") or 0 for p in post_list)
        total_comments = sum(p.get("comment_count") or 0 for p in post_list)
        avg_likes = total_likes / len(post_list)
        avg_comments = total_comments / len(post_list)
        er = (avg_likes + avg_comments) / followers * 100
        return round(er, 3), round(avg_likes, 1), round(avg_comments, 1)

    reels_er, reels_avg_likes, reels_avg_comments = _er(reels)
    static_er, static_avg_likes, static_avg_comments = _er(statics)

    # 基准判断
    if tier == "micro":
        reels_min = benchmarks.get("micro_reels_min", 3.0)
        static_min = benchmarks.get("micro_static_min", 1.8)
    else:
        reels_min = benchmarks.get("mid_reels_min", 1.5)
        static_min = benchmarks.get("mid_static_min", 1.5)

    meets = False
    if reels and reels_er >= reels_min:
        meets = True
    elif statics and static_er >= static_min:
        meets = True
    elif not reels and not statics:
        meets = False

    return {
        "reels_count": len(reels),
        "reels_avg_likes": reels_avg_likes,
        "reels_avg_comments": reels_avg_comments,
        "reels_engagement_rate": reels_er,
        "static_count": len(statics),
        "static_avg_likes": static_avg_likes,
        "static_avg_comments": static_avg_comments,
        "static_engagement_rate": static_er,
        "meets_er_benchmark": meets,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 阶段 4: 评论意图分析
# ═══════════════════════════════════════════════════════════════════════════════

def stage4_comment_analysis(
    cl: Any,
    candidates: dict[str, dict[str, Any]],
    top_posts: int,
    comments_per_post: int,
    sleep_sec: int,
    only_handles: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """对通过漏斗的候选者分析评论购买意图。

    only_handles: 若提供，仅分析这些 handle（用于聚焦深抓真正的候选，省账号/时间）。
    """
    log("═══ 阶段 4: 评论意图分析 ═══")

    eligible = {h: c for h, c in candidates.items()
                if c.get("status") in ("include", "review")}
    if only_handles:
        eligible = {h: c for h, c in eligible.items() if h.lower() in only_handles}
    # 载入持久化的互赞团/水军账号库（跨 run 累积，命中直接判定）
    pod_lib = load_pod_library()
    pod_set = set(pod_lib.keys())
    log(f"  需分析: {len(eligible)} 个候选" + (f"（限定 {len(only_handles)} 个 handle）" if only_handles else "")
        + f" · 已知水军库 {len(pod_set)} 个账号")
    newly_flagged = 0

    for idx, (handle, cand) in enumerate(eligible.items(), 1):
        log(f"  [{idx}/{len(eligible)}] @{handle}")
        posts = cand.get("posts") or []
        if not posts:
            cand.update(_empty_comment_result())
            continue

        # 选帖策略：互动最高 N 条 + 最近 N 条。
        # 互赞团/水军集中在爆款帖的"热门评论"层，只看 top 帖会把样本污染。
        # 加入最近帖（新鲜、真实买家问答多）+ 每帖抓更深（穿过水军层）以提升采样真实度。
        by_engagement = sorted(
            posts, key=lambda p: (p.get("like_count") or 0) + (p.get("comment_count") or 0),
            reverse=True,
        )[:top_posts]
        by_recent = sorted(posts, key=lambda p: p.get("taken_at") or "", reverse=True)[:top_posts]
        sorted_posts, seen_pk = [], set()
        for post in by_engagement + by_recent:
            pk = post.get("pk") or ""
            if pk and pk not in seen_pk:
                seen_pk.add(pk)
                sorted_posts.append(post)

        all_comments: list[dict[str, Any]] = []
        seen_comment_pk: set[str] = set()
        commenter_posts: dict[str, set[str]] = defaultdict(set)  # 跨帖追踪：username → 评论过的帖子集合
        for post in sorted_posts:
            pk = post.get("pk") or ""
            if not pk:
                continue
            try:
                comments = fetch_comments(cl, pk, comments_per_post)
                added = 0
                for cm in comments:
                    cm["post_code"] = post.get("code", "")   # 记录所在帖，供报告"原帖"一键跳转
                    u = (cm.get("username") or "").lower()
                    if u:
                        commenter_posts[u].add(pk)  # 先记跨帖（去重前），保住"刷遍全部帖"信号
                    cpk = cm.get("pk") or ""
                    # 只按评论 pk 去重；同账号在不同帖的相同话不去重（那正是 pod 信号）
                    key = cpk or (u + "|" + (cm.get("text", "")[:30]) + "|" + pk)
                    if key in seen_comment_pk:
                        continue
                    seen_comment_pk.add(key)
                    all_comments.append(cm)
                    added += 1
                log(f"    帖子 {post.get('code','?')}: {len(comments)} 抓取 / {added} 新增")
            except Exception as exc:
                log(f"    帖子 {post.get('code','?')}: 评论获取失败 - {exc}")
            time.sleep(sleep_sec)

        if not all_comments:
            cand.update(_empty_comment_result())
            cand.setdefault("review_reasons", [])
            cand["review_reasons"].append("comments_unavailable")
            continue

        # 跨帖刷评者：在该创作者 ≥2 个采样帖都评论的账号（pod 行为）
        cross_post_podders = {u for u, pks in commenter_posts.items() if len(pks) >= 2}

        analysis = _analyze_comments(all_comments, pod_library=pod_set,
                                     cross_post_podders=cross_post_podders)
        # 回写水军库：本次新判定的 pod 账号 → 累积（含命中创作者、样例、原因、次数）
        pod_commenters = analysis.pop("pod_commenters", {})
        ts = datetime.now(timezone.utc).isoformat()
        for u, info in pod_commenters.items():
            entry = pod_lib.get(u)
            if entry:
                entry["hits"] = entry.get("hits", 1) + 1
                entry["last_seen"] = ts
                if handle not in entry.get("creators", []):
                    entry.setdefault("creators", []).append(handle)
                if info["reason"] not in entry.get("reasons", []):
                    entry.setdefault("reasons", []).append(info["reason"])
            else:
                pod_lib[u] = {"first_seen": ts, "last_seen": ts, "hits": 1,
                              "creators": [handle], "sample": info["sample"],
                              "reasons": [info["reason"]]}
                pod_set.add(u)
                newly_flagged += 1
        cand["pod_commenters"] = sorted(pod_commenters.keys())
        cand["pod_commenters_count"] = len(pod_commenters)
        cand.update(analysis)

        # 刷量主导（bot+pod ≥ 50%）→ include 降级为 review
        if analysis["low_quality_ratio"] >= 0.50:
            cand.setdefault("review_reasons", [])
            if "inauthentic_engagement" not in cand["review_reasons"]:
                cand["review_reasons"].append("inauthentic_engagement")
            if cand.get("status") == "include":
                cand["status"] = "review"
        log(f"    intent={analysis['purchase_intent_ratio']:.1%},"
            f" bot={analysis['bot_comment_ratio']:.1%},"
            f" pod={analysis['engagement_pod_ratio']:.1%},"
            f" trust={analysis['trust_level']},"
            f" 水军命中={len(pod_commenters)}（新增{sum(1 for u in pod_commenters if pod_lib[u]['hits']==1)}）")

    # 持久化水军库（跨 run 累积，下次扫到直接略过）
    if pod_lib:
        save_pod_library(pod_lib)
        log(f"  水军账号库更新 → {len(pod_lib)} 个（本次新增 {newly_flagged}）：{POD_LIBRARY_FILE.name}")

    return candidates


def _empty_comment_result() -> dict[str, Any]:
    return {
        "comments_analyzed": 0,
        "purchase_intent_ratio": 0,
        "bot_comment_ratio": 0,
        "engagement_pod_ratio": 0,
        "low_quality_ratio": 0,
        "trust_level": "unknown",
        "top_intent_comments": [],
        "intent_keywords_found": [],
        "pod_comment_samples": [],
        "pod_commenters": [],
        "pod_commenters_count": 0,
    }


def load_pod_library() -> dict[str, dict[str, Any]]:
    """载入互赞团/水军账号库 {username: {hits, creators, sample, reasons,...}}。"""
    if not POD_LIBRARY_FILE.exists():
        return {}
    try:
        data = json.loads(POD_LIBRARY_FILE.read_text(encoding="utf-8"))
        return data.get("accounts", {})
    except Exception:
        return {}


def save_pod_library(accounts: dict[str, dict[str, Any]]) -> None:
    POD_LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(),
               "count": len(accounts), "accounts": accounts}
    POD_LIBRARY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    POD_LIBRARY_FILE.chmod(0o600)


def _analyze_comments(
    comments: list[dict[str, Any]],
    pod_library: set[str] | None = None,
    cross_post_podders: set[str] | None = None,
) -> dict[str, Any]:
    """分析评论的购买意图、bot 比例、互赞团比例。

    低质评论分两类：
    - bot：纯 emoji / 纯 tag / 单词吹捧 / spam
    - pod（互赞团）：泛泛吹捧"内容/创作者"本身，或刷量行为特征

    互赞团判定三信号（任一命中且无购买意图）：
      1. 账号已在水军库 pod_library（历史扫到过，直接判定）
      2. 跨帖刷评 cross_post_podders（同一账号在该创作者多个帖子都评论）
      3. 文本命中 ENGAGEMENT_POD_PATTERNS（泛泛吹捧）
    返回 pod_commenters（被判为水军的账号 + 样例 + 原因），供回写账号库。
    """
    pod_library = pod_library or set()
    cross_post_podders = cross_post_podders or set()
    total = len(comments)
    bot_count = 0
    pod_count = 0
    intent_scores: list[tuple[float, str, str, str]] = []   # (score, text, keyword, post_code)
    intent_keywords_found: set[str] = set()
    pod_samples: list[str] = []
    pod_commenters: dict[str, dict[str, Any]] = {}   # username -> {sample, reason}

    for comment in comments:
        text = (comment.get("text") or "").strip()
        user = (comment.get("username") or "").lower()
        if not text:
            bot_count += 1
            continue

        # 购买意图检测（优先，意图评论不算低质）
        text_lower = text.lower()
        best_score = 0
        best_kw = ""
        for kw in STRONG_INTENT:
            if kw in text_lower:
                best_score = max(best_score, 3); best_kw = kw; intent_keywords_found.add(kw)
        for kw in MEDIUM_INTENT:
            if kw in text_lower:
                best_score = max(best_score, 2)
                if not best_kw: best_kw = kw
                intent_keywords_found.add(kw)
        for kw in WEAK_INTENT:
            if kw in text_lower:
                best_score = max(best_score, 1)
                if not best_kw: best_kw = kw
                intent_keywords_found.add(kw)
        if best_score > 0:
            intent_scores.append((best_score, text, best_kw, comment.get("post_code", "")))
            continue  # 有意图就不再判低质

        # Bot 检测（纯 emoji / 纯 tag / 单词吹捧 / spam）
        if any(pat.match(text) for pat in BOT_TEXT_PATTERNS):
            bot_count += 1
            continue

        # 互赞团检测（三信号）。跨帖信号必须配合"低质文本"才算——否则会把忠实粉丝
        # （在多帖留走心长评/真实讨论）误判为互赞团。低质 = 泛泛吹捧 / 很短 / 纯表情。
        # 真互赞团 = 反复刷"泛泛短评"；忠粉 = 反复留"有内容的话" → 后者是优质互动，不该扣分。
        is_lowvalue = len(text.strip()) < 30 or any(pat.search(text) for pat in ENGAGEMENT_POD_PATTERNS)
        pod_reason = ""
        if user and user in pod_library:
            pod_reason = "known_pod"          # 历史水军库命中
        elif user and user in cross_post_podders and is_lowvalue:
            pod_reason = "cross_post"         # 跨帖刷评 + 低质文本 才算（走心长评的跨帖=忠粉，放行）
        elif any(pat.search(text) for pat in ENGAGEMENT_POD_PATTERNS):
            pod_reason = "generic_praise"     # 泛泛吹捧
        if pod_reason:
            pod_count += 1
            if len(pod_samples) < 5:
                pod_samples.append(text)
            if user and user not in pod_commenters:
                pod_commenters[user] = {"sample": text[:80], "reason": pod_reason}
            continue

    low_quality_count = bot_count + pod_count
    valid_count = total - low_quality_count
    intent_count = len(intent_scores)
    intent_ratio = intent_count / valid_count if valid_count > 0 else 0
    bot_ratio = bot_count / total if total > 0 else 0
    pod_ratio = pod_count / total if total > 0 else 0
    low_quality_ratio = low_quality_count / total if total > 0 else 0

    if intent_ratio >= 0.15 and low_quality_ratio < 0.40:
        trust = "high"
    elif intent_ratio >= 0.05 and low_quality_ratio < 0.60:
        trust = "medium"
    elif low_quality_ratio >= 0.50:
        trust = "low"
    elif intent_ratio > 0:
        trust = "medium"
    else:
        trust = "low"

    intent_scores.sort(key=lambda x: -x[0])
    top_comments = [{"text": text, "keyword": kw, "score": score, "post_code": pc}
                    for score, text, kw, pc in intent_scores[:5]]

    return {
        "comments_analyzed": total,
        "purchase_intent_ratio": round(intent_ratio, 3),
        "bot_comment_ratio": round(bot_ratio, 3),
        "engagement_pod_ratio": round(pod_ratio, 3),
        "low_quality_ratio": round(low_quality_ratio, 3),
        "trust_level": trust,
        "top_intent_comments": top_comments,
        "intent_keywords_found": sorted(intent_keywords_found),
        "pod_comment_samples": pod_samples,
        "pod_commenters": pod_commenters,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 阶段 5: 综合评分
# ═══════════════════════════════════════════════════════════════════════════════

def stage5_scoring(
    candidates: dict[str, dict[str, Any]],
    modash_profiles: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """计算综合评分，整合 Modash 数据增强。"""
    log("═══ 阶段 5: 综合评分 ═══")

    has_modash = bool(modash_profiles)
    if has_modash:
        log(f"  Modash profiles 数据可用: {len(modash_profiles)} 条")

    for handle, cand in candidates.items():
        if cand.get("status") not in ("include", "review"):
            cand["discovery_score"] = 0
            continue

        # 合并 Modash 数据
        if has_modash and handle in modash_profiles:
            modash_data = modash_profiles[handle]
            for key, val in modash_data.items():
                cand[key] = val

            # 如果 Modash credibility 太低，降级
            credibility = modash_data.get("modash_credibility")
            if credibility is not None and credibility < 0.60:
                cand["status"] = "exclude"
                cand.setdefault("filter_reasons", [])
                cand["filter_reasons"].append(f"low_credibility:{credibility}")
                cand["discovery_score"] = 0
                continue

        # 计算子分（含每项的计算依据 basis）
        scores, basis = _compute_sub_scores(cand, has_modash)
        cand["_sub_scores"] = scores

        use_modash = has_modash and handle in (modash_profiles or {})
        # 各维度权重（有 Modash 时 8 项，否则 6 项）
        if use_modash:
            weights = {"engagement": 0.20, "product_rec": 0.18, "purchase_intent": 0.17,
                       "authority": 0.12, "sponsorship_health": 0.08, "evidence": 0.05,
                       "credibility": 0.10, "audience_fit": 0.10}
        else:
            weights = {"engagement": 0.25, "product_rec": 0.20, "purchase_intent": 0.20,
                       "authority": 0.15, "sponsorship_health": 0.10, "evidence": 0.10}
        labels = {"engagement": "互动率", "product_rec": "产品推荐占比",
                  "purchase_intent": "评论购买意图", "authority": "护肤/设备权威度",
                  "sponsorship_health": "赞助健康度", "evidence": "发现证据强度",
                  "credibility": "粉丝真实度(Modash)", "audience_fit": "粉丝画像匹配(Modash)"}
        components = []
        total = 0.0
        for key, w in weights.items():
            raw = scores.get(key, 50)
            contrib = raw * w
            total += contrib
            components.append({
                "name": labels.get(key, key), "raw": round(raw, 1), "weight": w,
                "contribution": round(contrib, 1), "basis": basis.get(key, ""),
            })
        base_total = total
        # 赛道对口系数：跨垂类（如理财号）即便指标好，对护肤设备活动价值也低 → 打折
        fit = cand.get("campaign_fit", "related")
        fit_mult = {"core": 1.0, "related": 0.9, "off": 0.55, "unknown": 0.85}.get(fit, 0.9)
        total = base_total * fit_mult
        cand["discovery_score"] = round(total, 1)
        components.append({
            "name": "赛道对口系数", "raw": round(fit_mult * 100, 0), "weight": "×",
            "contribution": round(total - base_total, 1),
            "basis": f"主赛道「{cand.get('niche_primary_label','?')}」对本次护肤设备活动属「{cand.get('campaign_fit_label','?')}」"
                     f" → 系数 ×{fit_mult}（核心1.0/相关0.9/跨垂类0.55）",
        })
        cand["score_breakdown"] = {
            "formula": "Σ(各维度子分 0-100 × 权重) × 赛道对口系数，各项均有独立依据",
            "mode": "8 维（含 Modash）" if use_modash else "6 维（无 Modash）",
            "components": components,
            "base_total": round(base_total, 1),
            "fit_multiplier": fit_mult,
            "total": round(total, 1),
        }

    # 输出概览
    scored = [(h, c["discovery_score"]) for h, c in candidates.items()
              if c.get("status") in ("include", "review") and c.get("discovery_score", 0) > 0]
    scored.sort(key=lambda x: -x[1])
    log(f"  评分完成: {len(scored)} 个有效候选")
    for h, s in scored[:10]:
        log(f"    @{h}: {s}")

    return candidates


def _compute_sub_scores(cand: dict[str, Any], has_modash: bool) -> tuple[dict[str, float], dict[str, str]]:
    """计算各维度子分 (0-100) + 每项的计算依据 basis（可审计，非黑箱）。"""
    scores: dict[str, float] = {}
    basis: dict[str, str] = {}

    # 互动率：取 Reels/Static 较高者 × 15（≈6.7% 封顶 100）
    reels_er = cand.get("reels_engagement_rate", 0)
    static_er = cand.get("static_engagement_rate", 0)
    best_er = max(reels_er, static_er)
    scores["engagement"] = min(100, best_er * 15)
    basis["engagement"] = f"max(Reels {reels_er}%, Static {static_er}%)={best_er}% × 15 = {round(best_er*15,1)}（封顶100）"

    # 产品推荐占比 × 150（≈67% 封顶 100）
    rec_ratio = cand.get("product_rec_ratio", 0)
    scores["product_rec"] = min(100, rec_ratio * 150)
    basis["product_rec"] = f"产品推荐占比 {rec_ratio} × 150 = {round(rec_ratio*150,1)}（封顶100）"

    # 购买意图 × 400，再按社区真实度打折（刷量越多越不可信）
    intent = cand.get("purchase_intent_ratio", 0)
    lowq = cand.get("low_quality_ratio", 0)
    authenticity = max(0.2, 1.0 - lowq)
    scores["purchase_intent"] = min(100, intent * 400) * authenticity
    basis["purchase_intent"] = (f"购买意图 {intent} × 400 = {round(min(100,intent*400),1)}，"
                                f"再 × 真实度系数 {round(authenticity,2)}（=1−刷量占比 {lowq}）= {round(scores['purchase_intent'],1)}")

    # 权威度（详见 authority_breakdown）
    auth = cand.get("skincare_authority_score", 0)
    scores["authority"] = min(100, auth)
    basis["authority"] = f"护肤/设备权威度评分 {auth}（详见「权威度分解」：类目命中×权重×5）"

    # 赞助健康度：100 − 赞助占比 × 250（40% → 0）
    spon_ratio = cand.get("sponsored_ratio", 0)
    scores["sponsorship_health"] = max(0, 100 - spon_ratio * 250)
    basis["sponsorship_health"] = f"100 − 赞助占比 {spon_ratio} × 250 = {round(scores['sponsorship_health'],1)}（赞助40%→0分）"

    # 发现证据强度：发现源数 × 25
    sources = cand.get("discovery_sources") or []
    scores["evidence"] = min(100, len(sources) * 25)
    basis["evidence"] = f"发现源 {len(sources)} 个（{', '.join(sources) or '—'}）× 25 = {min(100,len(sources)*25)}"

    if has_modash:
        cred = cand.get("modash_credibility")
        if cred is not None:
            scores["credibility"] = min(100, cred * 110)
            basis["credibility"] = f"Modash 真实粉丝比例 {cred} × 110 = {round(min(100,cred*110),1)}"
        else:
            scores["credibility"] = 50
            basis["credibility"] = "无 Modash credibility 数据，取中间值 50"
        us_pct = cand.get("modash_audience_us_pct") or 0
        female_pct = cand.get("modash_audience_female_pct") or 0
        age_pct = cand.get("modash_audience_age_18_34_pct") or 0
        if us_pct or female_pct or age_pct:
            fit = (min(100, us_pct * 2.5) * 0.4 + min(100, female_pct * 1.5) * 0.3
                   + min(100, age_pct * 2.0) * 0.3)
            scores["audience_fit"] = round(fit, 1)
            basis["audience_fit"] = f"US {us_pct}%·女性 {female_pct}%·18-34 {age_pct}% 加权 = {round(fit,1)}"
        else:
            scores["audience_fit"] = 50
            basis["audience_fit"] = "无 Modash 画像数据，取中间值 50"

    return scores, basis


# ═══════════════════════════════════════════════════════════════════════════════
# 阶段 6: 输出
# ═══════════════════════════════════════════════════════════════════════════════

def stage6_output(
    candidates: dict[str, dict[str, Any]],
    run_id: str,
) -> tuple[Path, Path]:
    """输出 JSON + CSV。"""
    log("═══ 阶段 6: 输出 ═══")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # 按分数排序
    sorted_cands = sorted(
        candidates.values(),
        key=lambda c: (
            {"include": 2, "review": 1}.get(c.get("status", ""), 0),
            c.get("discovery_score", 0),
        ),
        reverse=True,
    )

    # ── JSON ──
    json_path = RUNS_DIR / f"discovery-{run_id}.json"
    output_data = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_candidates": len(sorted_cands),
            "include": sum(1 for c in sorted_cands if c.get("status") == "include"),
            "review": sum(1 for c in sorted_cands if c.get("status") == "review"),
            "exclude": sum(1 for c in sorted_cands if c.get("status") == "exclude"),
            "error": sum(1 for c in sorted_cands if c.get("status") == "error"),
        },
        "candidates": [_flatten_for_output(c) for c in sorted_cands],
    }
    json_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    log(f"  JSON → {json_path}")

    # ── CSV ──
    csv_path = RUNS_DIR / f"discovery-{run_id}.csv"
    flat_rows = [_flatten_for_csv(c) for c in sorted_cands]
    if flat_rows:
        all_keys = list(flat_rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys)
            writer.writeheader()
            writer.writerows(flat_rows)
    log(f"  CSV  → {csv_path}")

    # 输出摘要
    summary = output_data["summary"]
    log(f"\n  ══ 运行结果 ══")
    log(f"  总候选: {summary['total_candidates']}")
    log(f"  ✓ Include: {summary['include']}")
    log(f"  ? Review:  {summary['review']}")
    log(f"  ✗ Exclude: {summary['exclude']}")
    log(f"  ✗ Error:   {summary['error']}")

    # 打印 Top 10
    top = [c for c in sorted_cands if c.get("status") in ("include", "review")][:10]
    if top:
        log(f"\n  Top {len(top)} 候选:")
        for c in top:
            log(f"    [{c.get('status','?'):7s}] @{c.get('handle','?'):25s}"
                f"  score={c.get('discovery_score',0):5.1f}"
                f"  followers={c.get('follower_count',0):>7,}"
                f"  type={c.get('creator_archetype','?')}"
                f"  amazon={c.get('has_amazon_storefront','?')}"
                f"  trust={c.get('trust_level','?')}")

    return json_path, csv_path


# ─── 输出字段清理 ──────────────────────────────────────────────────────────────

OUTPUT_FIELDS = [
    "handle", "profile_url", "full_name", "follower_count", "following_count",
    "media_count", "is_verified", "tier",
    # Bio
    "has_amazon_storefront", "bio_link_type", "bio_link_url",
    # 内容
    "creator_archetype", "product_rec_ratio", "skincare_authority_score",
    "authority_evidence", "product_rec_breakdown", "authority_breakdown",
    # 赛道/垂类
    "niche_primary", "niche_primary_label", "niche_secondary",
    "campaign_fit", "campaign_fit_label", "niche_breakdown",
    # 赞助
    "sponsored_count", "sponsored_ratio", "organic_amazon_posts_count",
    # 互动率
    "reels_count", "reels_avg_likes", "reels_avg_comments", "reels_engagement_rate",
    "static_count", "static_avg_likes", "static_avg_comments", "static_engagement_rate",
    "meets_er_benchmark",
    # 评论
    "comments_analyzed", "purchase_intent_ratio", "bot_comment_ratio",
    "engagement_pod_ratio", "low_quality_ratio",
    "trust_level", "top_intent_comments", "intent_keywords_found",
    "pod_comment_samples", "pod_commenters", "pod_commenters_count",
    # Modash
    "modash_credibility", "modash_fake_pct", "modash_audience_us_pct",
    "modash_audience_female_pct", "modash_audience_age_18_34_pct",
    "modash_avg_likes", "modash_avg_reels_plays", "modash_er",
    # 综合
    "discovery_sources", "discovery_score", "score_breakdown", "status",
    "filter_reasons", "review_reasons",
]


def _flatten_for_output(cand: dict[str, Any]) -> dict[str, Any]:
    """清理候选者数据用于 JSON 输出。"""
    out = {}
    for key in OUTPUT_FIELDS:
        val = cand.get(key)
        if val is not None:
            out[key] = val
    out.setdefault("profile_url", f"https://instagram.com/{cand.get('handle', '')}")
    # 去掉内部字段
    out.pop("posts", None)
    out.pop("_sub_scores", None)
    return out


def _flatten_for_csv(cand: dict[str, Any]) -> dict[str, str]:
    """平铺为 CSV 行（所有值转字符串）。"""
    out = _flatten_for_output(cand)
    csv_row: dict[str, str] = {}
    for key in OUTPUT_FIELDS:
        if key in ("product_rec_breakdown", "authority_breakdown", "score_breakdown",
                   "niche_breakdown"):
            continue  # 复杂分解结构只进 JSON/HTML，不进 CSV（保持表格干净）
        val = out.get(key, "")
        if isinstance(val, list):
            val = " | ".join(
                str(v.get("text", v) if isinstance(v, dict) else v)
                for v in val
            )
        elif isinstance(val, bool):
            val = str(val)
        elif isinstance(val, dict):
            continue
        csv_row[key] = str(val) if val is not None else ""
    return csv_row


# ─── 扫描缓存（含 posts，用于离线调参）──────────────────────────────────────────

def save_scan_cache(candidates: dict[str, dict[str, Any]], run_id: str) -> Path:
    """保存阶段2扫描结果（含 posts）到缓存，供离线重跑漏斗。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"scan-cache-{run_id}.json"
    path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_scan_cache(path: str) -> dict[str, dict[str, Any]]:
    """从缓存载入候选（含 posts）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data


# ═══════════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    seeds = load_seeds_toml()

    parser = argparse.ArgumentParser(
        description="Amazon Finds 导购型红人发现",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 账号
    parser.add_argument("--username", default=None,
                        help="单账号模式：指定一个采集账号（默认用账号池轮换）")
    parser.add_argument("--rotate-every", type=int, default=10,
                        help="账号池模式：单账号用满 N 次成功请求就主动轮换（默认 10）")
    parser.add_argument("--wait-pool", action="store_true", default=False,
                        help="全量扫描：账号池全冷却时等待恢复再继续，而非耗尽退出")
    parser.add_argument("--cooldown", type=int, default=30,
                        help="账号池模式：账号受限后冷却分钟数（默认 30）")
    parser.add_argument("--include-cookie-accounts", action="store_true", default=False,
                        help="账号池模式：把原 5 个 cookie 账号也加入池子")
    parser.add_argument("--no-proxy", action="store_true", default=True,
                        help="不使用代理")

    # 种子源
    brands_default = seeds.get("brands", {}).get("handles", [])
    parser.add_argument("--brand-handles", type=lambda s: s.split(","),
                        default=brands_default,
                        help="品牌账号列表，逗号分隔")

    tags_default = seeds.get("hashtags", {}).get("tags", [])
    parser.add_argument("--hashtags", type=lambda s: s.split(","),
                        default=tags_default,
                        help="Hashtag 列表，逗号分隔")
    parser.add_argument("--use-hashtags", action="store_true", default=False,
                        help="启用 hashtag 采集（默认关闭；多数账号无权限且会软封 session）")
    # 种子源扩展（突破 A：私有端点可用，定位垂类创作者）
    parser.add_argument("--keyword-search", action="store_true", default=False,
                        help="启用关键词用户搜索 search_users（按垂类直接找人，种子无关）")
    parser.add_argument("--lookalike", action="store_true", default=False,
                        help="启用 lookalike 扩展 fbsearch_suggested_profiles（从对口种子找相似账号）")
    parser.add_argument("--brand-engagement", action="store_true", default=False,
                        help="启用品牌帖点赞者挖掘 media_likers")
    parser.add_argument("--expand-all", action="store_true", default=False,
                        help="一键启用上述三种种子源扩展")
    parser.add_argument("--max-candidates", type=int, default=0,
                        help="候选数上限（0=不限；用于控制规模/保护账号）")
    parser.add_argument("--from-scan", type=str, default=None,
                        help="从扫描缓存载入候选（含 posts），跳过登录+阶段1-2，直接跑漏斗。用于离线调参")
    parser.add_argument("--no-comments", action="store_true", default=False,
                        help="跳过阶段4评论分析（纯离线调参时用）")
    parser.add_argument("--comment-handles", type=str, default=None,
                        help="仅对这些 handle 做评论分析（逗号分隔；聚焦深抓真候选，省账号/时间）")
    parser.add_argument("--penetrate-linktree", action="store_true", default=False,
                        help="尝试 HTTP 穿透 linktr.ee/beacons 检测 Amazon（默认关，多被 bot 防护拦截）")

    # Modash
    parser.add_argument("--modash-search", type=str, default=None,
                        help="Modash 搜索导出 CSV 路径")
    parser.add_argument("--modash-profiles", type=str, default=None,
                        help="Modash Profile Report 导出 CSV 路径")

    # 采集参数
    collection = seeds.get("collection", {})
    parser.add_argument("--per-source", type=int,
                        default=collection.get("per_source", 15),
                        help="每个种子源采集帖子数")
    parser.add_argument("--candidate-posts", type=int,
                        default=collection.get("candidate_posts", 20),
                        help="每个候选者采集帖子数")
    parser.add_argument("--comments-per-post", type=int,
                        default=collection.get("comments_per_post", 25),
                        help="每条帖子采集评论数")
    parser.add_argument("--top-posts-for-comments", type=int,
                        default=collection.get("top_posts_for_comments", 4),
                        help="每个候选者取互动最高的 N 条帖子分析评论")
    parser.add_argument("--sleep", type=int,
                        default=collection.get("sleep_seconds", 5),
                        help="API 调用间隔秒数")

    # 筛选参数
    filters = seeds.get("filters", {})
    parser.add_argument("--min-followers", type=int,
                        default=filters.get("min_followers", 10_000))
    parser.add_argument("--max-followers", type=int,
                        default=filters.get("max_followers", 150_000))
    parser.add_argument("--max-sponsored-ratio", type=float,
                        default=filters.get("max_sponsored_ratio", 0.40))
    parser.add_argument("--min-product-rec-ratio", type=float,
                        default=filters.get("min_product_rec_ratio", 0.20))

    return parser.parse_args()


def _init_client(args: argparse.Namespace):
    """初始化在线 client：单账号 or 账号池轮换。"""
    if args.username:
        log(f"  模式: 单账号 ({args.username})")
        return build_client(args.username, no_proxy=args.no_proxy)
    accounts = load_pool_accounts(include_cookie_accounts=args.include_cookie_accounts)
    if not accounts:
        raise PoolExhausted("账号池为空（.secrets/account_pool.json 不存在），请先导入账号")
    log(f"  模式: 账号池轮换 ({len(accounts)} 个账号, "
        f"每账号 {args.rotate_every} 次轮换, cooldown {args.cooldown} 分钟)")
    pool = AccountPool(
        accounts,
        rotate_every=args.rotate_every,
        cooldown_minutes=args.cooldown,
        no_proxy=args.no_proxy,
        wait_on_exhaust=getattr(args, "wait_pool", False),
    )
    pool.warm_up()
    return pool


def main() -> int:
    args = parse_args()
    seeds = load_seeds_toml()
    global PENETRATE_LINKTREE
    PENETRATE_LINKTREE = args.penetrate_linktree
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"═══ Amazon Finds 红人发现 (run: {run_id}) ═══")
    log(f"  品牌: {args.brand_handles}")
    log(f"  Hashtags: {len(args.hashtags)} 个")
    if args.modash_search:
        log(f"  Modash Search CSV: {args.modash_search}")
    if args.modash_profiles:
        log(f"  Modash Profiles CSV: {args.modash_profiles}")

    cl = None
    if args.from_scan:
        # ── 从扫描缓存载入：跳过登录 + 阶段 1-2 ──
        log(f"  模式: 从扫描缓存载入 ({args.from_scan})")
        candidates = load_scan_cache(args.from_scan)
        log(f"  载入 {len(candidates)} 个候选（含 posts）")
        # 评论分析仍需在线 client（除非 --no-comments）
        if not args.no_comments:
            try:
                cl = _init_client(args)
            except PoolExhausted as exc:
                log(f"⚠ {exc}（将跳过评论分析）")
                cl = None
    else:
        try:
            cl = _init_client(args)
        except PoolExhausted as exc:
            log(f"⚠ {exc}")
            return 1

        # 阶段 1: Seed 采集
        disc = seeds.get("discovery", {})
        candidates = stage1_collect_seeds(
            cl=cl,
            brand_handles=args.brand_handles,
            hashtags=args.hashtags,
            per_source=args.per_source,
            sleep_sec=args.sleep,
            modash_search_path=args.modash_search,
            use_hashtags=args.use_hashtags,
            keyword_queries=disc.get("keyword_search_queries", []),
            use_keyword_search=args.keyword_search or args.expand_all,
            lookalike_seeds=disc.get("lookalike_seeds", []),
            use_lookalike=args.lookalike or args.expand_all,
            # media_likers 是封号磁铁 + 噪声大 → 不进 expand-all 默认，单独开
            use_brand_engagement=args.brand_engagement,
        )

        if not candidates:
            log("⚠ 候选池为空，结束。")
            return 1

        # 候选数上限（控制规模 / 保护账号 / demo 用）
        if args.max_candidates and len(candidates) > args.max_candidates:
            kept = dict(list(candidates.items())[: args.max_candidates])
            log(f"  候选数上限 {args.max_candidates}，从 {len(candidates)} 个截断")
            candidates = kept

        # 阶段 2: Profile + Posts
        candidates = stage2_scan_profiles(
            cl=cl,
            candidates=candidates,
            candidate_posts=args.candidate_posts,
            sleep_sec=args.sleep,
        )

        # 保存扫描缓存（含 posts），供离线重跑漏斗调参
        cache_path = save_scan_cache(candidates, run_id)
        log(f"  扫描缓存 → {cache_path}")

    # 筛选配置
    filter_cfg = {
        "min_followers": args.min_followers,
        "max_followers": args.max_followers,
        "max_sponsored_ratio": args.max_sponsored_ratio,
        "min_product_rec_ratio": args.min_product_rec_ratio,
        "brand_handles": args.brand_handles,
        "engagement_benchmarks": seeds.get("filters", {}).get("engagement_benchmarks", {}),
    }

    # 阶段 3: 漏斗筛选
    candidates = stage3_funnel(candidates, filter_cfg)

    # 阶段 4: 评论分析（需要在线 client；--no-comments 或无可用账号时跳过）
    if cl is not None and not args.no_comments:
        only = None
        if args.comment_handles:
            only = {h.strip().lower().lstrip("@") for h in args.comment_handles.split(",") if h.strip()}
        candidates = stage4_comment_analysis(
            cl=cl,
            candidates=candidates,
            top_posts=args.top_posts_for_comments,
            comments_per_post=args.comments_per_post,
            sleep_sec=args.sleep,
            only_handles=only,
        )
    else:
        log("═══ 阶段 4: 评论意图分析 ═══")
        log("  (跳过评论分析)")

    # Modash Profile 增强数据
    modash_profiles = None
    if args.modash_profiles:
        modash_profiles = parse_modash_profiles_csv(args.modash_profiles)

    # 阶段 5: 评分
    candidates = stage5_scoring(candidates, modash_profiles)

    # 阶段 6: 输出
    json_path, csv_path = stage6_output(candidates, run_id)

    # 自动入本地数据库（每日内容持久化，供随时查询/复核）
    try:
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "db.py"), "ingest", str(json_path)],
                       check=False, capture_output=True, timeout=60)
        log(f"  已入库 data/discovery.db（查询：scripts/db.py creator <handle> / list / stats）")
    except Exception as exc:
        log(f"  ⚠ 入库失败（不影响结果）: {exc}")

    log(f"\n✓ 完成！结果已保存:")
    log(f"  JSON: {json_path}")
    log(f"  CSV:  {csv_path}")

    # 账号池统计
    if isinstance(cl, AccountPool):
        s = cl.stats()
        log(f"\n  账号池统计: {s['total_requests']} 次请求, "
            f"{s['rotations']} 次轮换, 可用 {s['available']}/{s['total_accounts']}")

    # 如果有通过者但无 Modash 数据，提示用户
    include_count = sum(1 for c in candidates.values() if c.get("status") in ("include", "review"))
    if include_count > 0 and not args.modash_profiles:
        handles = [c["handle"] for c in candidates.values()
                   if c.get("status") in ("include", "review")]
        log(f"\n  💡 下一步: 在 Modash 查看以下 {len(handles)} 个候选者的 Profile Report 并导出 CSV:")
        for h in handles[:20]:
            log(f"     https://marketer.modash.io/instagram/{h}")
        log(f"  然后重新运行脚本并传入 --modash-profiles 参数以增强评分。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
