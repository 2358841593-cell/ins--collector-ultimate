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

import ipaddress
import re
from urllib.parse import urlsplit


AMAZON_TYPES = frozenset({"Amazon"})
STOREFRONT_TYPES = frozenset({"Amazon", "LTK", "ShopMy", "自营店", "链接聚合"})

_AMAZON_HOSTS = (
    "amazon.com", "amazon.ca", "amazon.com.mx", "amazon.com.br", "amazon.co.uk",
    "amazon.de", "amazon.fr", "amazon.it", "amazon.es", "amazon.nl", "amazon.se",
    "amazon.pl", "amazon.com.be", "amazon.ie", "amazon.co.jp", "amazon.in",
    "amazon.com.au", "amazon.sg", "amazon.ae", "amazon.sa", "amazon.com.tr",
    "amazon.eg", "amazon.co.za", "amazon.cn", "amzn.to",
)
_LTK_HOSTS = ("liketoknow.it", "shopltk.com", "ltk.app", "ltk.to")
_SHOPMY_HOSTS = ("shopmy.us",)

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
    "flowcode.com", "gravatar.com", "atom.bio", "bio.site", "komi.io",
    "linkbio.co", "linktw.in", "paa.ge", "taplink.cc", "vana.ly", "zez.am",
    "myyshop.com", "myyfinds.io",
)
_SELF_STORE_HOSTS = (
    "myshopify.com", "shopify.com", "bigcartel.com", "gumroad.com", "etsy.com",
    "shop.app", "fourthwall.com", "spring.com", "teespring.com", "depop.com",
    "poshmark.com", "ebay.com", "mercari.com", "ko-fi.com", "stan.store",
    "sumupstore.com",
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
    if any(_host_matches(host, recognized) for recognized in _AMAZON_HOSTS):
        return "Amazon"
    if any(_host_matches(host, recognized) for recognized in _LTK_HOSTS):
        return "LTK"
    if any(_host_matches(host, recognized) for recognized in _SHOPMY_HOSTS):
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
    if url and isinstance(explicit, str) and explicit in STOREFRONT_TYPES:
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


def is_safe_absolute_http_url(value: object) -> bool:
    """Return whether ``value`` is a safe, absolute delivery hyperlink.

    This is deliberately a side-effect-free validator: formal acceptance must
    not perform DNS lookups.  It nevertheless rejects active-content/custom
    schemes, credentials, local/IP-literal targets and non-web ports before a
    URL can reach the HTML/XLSX deliverables.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return False
    host = parsed.hostname.rstrip(".").casefold()
    if (
        not host
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith(".local")
        or "." not in host
    ):
        return False
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        # Reject browser-ambiguous numeric spellings such as 0177.0.0.1.
        if re.fullmatch(
            r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
            host,
            re.I,
        ):
            return False
    else:
        # A Storefront is an external named service, never an IP-literal link.
        return False
    return True


def delivery_validation_reasons(cand: dict) -> list[str]:
    """Validate the fail-closed Storefront contract for formal delivery.

    ``effective_status`` remains intentionally forgiving for historical
    routing.  Formal acceptance is stricter: the raw three-state declaration
    must agree with that normalized status, and its fields must be internally
    consistent.  Ordinary ``bio_links`` are evidence, not Storefront claims,
    and are therefore intentionally ignored for ``confirmed_no``.
    """
    reasons: list[str] = []
    raw_status = cand.get("storefront_status")
    normalized_status = effective_status(cand)
    valid_statuses = ("confirmed_yes", "confirmed_no", "unknown")

    if raw_status not in valid_statuses:
        reasons.append("raw_status_invalid")
    if raw_status != normalized_status:
        reasons.append(
            f"status_conflict(raw={raw_status!r},effective={normalized_status!r})"
        )

    if raw_status == "unknown":
        reasons.append("status_unknown")
        return reasons

    if raw_status == "confirmed_no":
        if cand.get("storefront_url") is not None and cand.get("storefront_url") != "":
            reasons.append("confirmed_no_storefront_url_present")
        if (
            cand.get("amazon_storefront_link") is not None
            and cand.get("amazon_storefront_link") != ""
        ):
            reasons.append("confirmed_no_amazon_storefront_link_present")
        if cand.get("storefront_type") is not None and cand.get("storefront_type") != "":
            reasons.append("confirmed_no_storefront_type_present")
        return reasons

    if raw_status != "confirmed_yes":
        return reasons

    raw_url = cand.get("storefront_url")
    raw_type = cand.get("storefront_type")
    if not isinstance(raw_url, str) or not raw_url:
        reasons.append("confirmed_yes_storefront_url_missing")
        return reasons
    if not is_safe_absolute_http_url(raw_url):
        reasons.append("storefront_url_not_safe_absolute_http")

    recognized_type = classify_url(raw_url)
    if recognized_type is None:
        reasons.append("storefront_url_unrecognized")
    raw_type_valid = isinstance(raw_type, str) and raw_type in STOREFRONT_TYPES
    if not raw_type_valid:
        reasons.append("confirmed_yes_storefront_type_missing_or_invalid")
    elif recognized_type is not None and raw_type != recognized_type:
        reasons.append(
            f"storefront_url_type_conflict(declared={raw_type!r},recognized={recognized_type!r})"
        )

    # The compatibility accessors prefer the legacy Amazon field.  A stale
    # legacy value must not silently override the formal storefront_url/type.
    normalized_url = storefront_url(cand)
    normalized_type = storefront_type(cand)
    if normalized_url != raw_url:
        reasons.append("storefront_url_conflict")
    if raw_type_valid and normalized_type != raw_type:
        reasons.append("storefront_type_conflict")

    amazon_url = cand.get("amazon_storefront_link")
    if amazon_url is not None and amazon_url != "":
        if not is_safe_absolute_http_url(amazon_url):
            reasons.append("amazon_storefront_link_not_safe_absolute_http")
        if classify_url(amazon_url) != "Amazon":
            reasons.append("amazon_storefront_link_not_amazon")
        if raw_type != "Amazon" or amazon_url != raw_url:
            reasons.append("amazon_storefront_link_conflict")
    return reasons


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
