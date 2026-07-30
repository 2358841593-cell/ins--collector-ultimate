"""Storefront 统一语义。

当前 V2 业务口径不是 Amazon 专线：

- Amazon / LTK / ShopMy / 明确自营店 / 已识别的购物聚合入口都算有 Storefront；
- ``confirmed_no`` 只表示确认没有 Storefront，但仍可走 Without-Storefront；
- ``unknown`` 表示证据不足，交 Stage 4 Review，不能在采集阶段误淘汰。

历史数据里 ``storefront_status`` 曾被当作“是否有 Amazon”使用，因此所有下游都应通过
本模块读取有效状态；显式的 ``storefront_url`` / ``storefront_type`` 可以修正旧的
``confirmed_no`` / ``unknown``。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


AMAZON_TYPES = frozenset({"Amazon"})
STOREFRONT_TYPES = frozenset({"Amazon", "LTK", "ShopMy", "自营店", "链接聚合"})

_SOCIAL_HOSTS = (
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "twitter.com",
    "x.com", "facebook.com", "threads.net", "pinterest.com", "snapchat.com", "t.me",
)
_AGGREGATOR_HOSTS = (
    "linktr.ee", "linktree.com", "beacons.ai", "beacons.page", "linkin.bio",
    "lnk.bio", "snipfeed.co", "flow.page", "msha.ke", "tapl.ink", "milkshake.app",
    "campsite.bio", "withkoji.com", "desty.page", "desty.link", "carrd.co",
    "znap.link", "hoo.be", "direct.me", "url.bio", "pillar.io", "many.link",
    "tap.bio", "solo.to", "allmylinks.com", "linkpop.com", "wonderl.ink",
    "wonderlink.io", "linkme.bio", "link.me", "later.com", "lnk.to", "linkr.bio",
    "flowcode.com", "gravatar.com",
)
_SELF_STORE_HOSTS = (
    "myshopify.com", "shopify.com", "bigcartel.com", "gumroad.com", "etsy.com",
    "shop.app", "fourthwall.com", "spring.com", "teespring.com", "depop.com",
    "poshmark.com", "ebay.com", "mercari.com", "ko-fi.com",
)
_SHOP_PATH = re.compile(r"/(?:shop|store|storefront|collections?|products?|cart)(?:/|$)", re.I)


def _host_path(url: str) -> tuple[str, str]:
    raw = str(url or "").strip()
    if not raw:
        return "", ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
    except ValueError:
        return "", ""
    return (p.hostname or "").lower().removeprefix("www."), p.path or "/"


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def classify_url(url: str) -> str | None:
    """按 URL 识别认可的电商橱窗/购物入口；普通非社交网页不再默认算自营店。"""
    host, path = _host_path(url)
    if not host:
        return None
    if host == "amzn.to" or host.startswith("amazon.") or ".amazon." in host:
        return "Amazon"
    if any(x in host for x in ("liketoknow", "shopltk")) or host in {"ltk.app", "ltk.to"}:
        return "LTK"
    if "shopmy" in host:
        return "ShopMy"
    if any(_host_matches(host, h) for h in _AGGREGATOR_HOSTS):
        return "链接聚合"
    if any(_host_matches(host, h) for h in _SELF_STORE_HOSTS):
        return "自营店"
    if host.endswith(".store") or _SHOP_PATH.search(path):
        return "自营店"
    if any(_host_matches(host, h) for h in _SOCIAL_HOSTS):
        return None
    return None


def storefront_url(cand: dict) -> str | None:
    return cand.get("amazon_storefront_link") or cand.get("storefront_url")


def storefront_type(cand: dict) -> str | None:
    url = storefront_url(cand)
    inferred = classify_url(url) if url else None
    explicit = cand.get("storefront_type")
    if inferred:
        return inferred
    if url and explicit in STOREFRONT_TYPES:
        return explicit
    if cand.get("amazon_storefront_link"):
        return "Amazon"
    return None


def effective_status(cand: dict) -> str:
    """兼容历史字段漂移，返回通用 Storefront 三态。"""
    if storefront_url(cand) and storefront_type(cand):
        return "confirmed_yes"
    raw = cand.get("storefront_status")
    if raw in ("confirmed_yes", "confirmed_no", "unknown"):
        return raw
    return "unknown"


def has_storefront(cand: dict) -> bool:
    return effective_status(cand) == "confirmed_yes"


def normalize(cand: dict) -> dict:
    """原位补齐通用状态/type/url，返回同一个 dict。"""
    status = effective_status(cand)
    cand["storefront_status"] = status
    if status == "confirmed_yes":
        kind = storefront_type(cand)
        url = storefront_url(cand)
        if kind:
            cand["storefront_type"] = kind
        if url:
            cand["storefront_url"] = url
    return cand
