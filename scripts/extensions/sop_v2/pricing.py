"""展示型预估报价派生。

客户口径（2026-07-28）：

- 曝光基准 = 最近 10 个非置顶 Reels 的平均播放量；
- 默认 CPM = 35 USD，参考区间 = 35–40 USD；
- 预估报价 = 平均播放量 / 1000 × CPM。

这是估算字段，不是红人/代理给出的实际报价，也不写入 ``paid_cpm``，默认不参与 Gate、
F 模块评分或最终路由。IG 播放样本缺失时可用 Modash ``avg_reels_plays`` 作明确标注的
第三方 fallback，不能冒充“最近 10 个非置顶 Reels”。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import re


def _positive_number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _observed_views(post: dict):
    if post.get("play_count_status") not in (None, "observed"):
        return None
    metric_source = post.get("play_count_source")
    # 一旦样本声明了来源，只接受 IG 原生 ig_play_count。总 play_count 可能含 FB
    # cross-post，不得进入均播。无来源仅保留给旧数据/纯函数测试兼容。
    if metric_source and metric_source != "ig_media_info.ig_play_count":
        return None
    value = post.get("play_count")
    if value is None:
        value = post.get("view_count")
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _is_reel(post: dict) -> bool:
    return bool(
        post.get("is_reel")
        or "/reel/" in str(post.get("code") or post.get("url") or "")
    )


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _post_identity(post: dict):
    values = [post.get("code"), post.get("url")]
    for value in values:
        raw = str(value or "").strip()
        match = re.search(r"/(?:[^/?#]+/)?(reel|p)/([A-Za-z0-9_-]+)", raw)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
    raw = str(next((value for value in values if value), "")).strip()
    return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/") or None


def _ordered_posts(cand: dict) -> list[dict]:
    """优先使用 Reels 专页的显式 grid_rank；否则只在时间戳完整时按时间倒序。

    当时间戳有缺失/混合类型时保留采集顺序，避免“较旧但有时间戳”的 Reel 挤掉
    “较新但时间戳暂缺”的 Reel。
    """
    posts = list(cand.get("pricing_reel_samples") or [])
    if posts and all(
        isinstance(post.get("grid_rank"), int)
        and not isinstance(post.get("grid_rank"), bool)
        for post in posts
    ):
        return sorted(posts, key=lambda post: post["grid_rank"])

    numeric_times = []
    for post in posts:
        try:
            numeric_times.append(float(post.get("taken_at")))
        except (TypeError, ValueError):
            return posts
    return [
        post
        for _, post in sorted(
            zip(numeric_times, posts), key=lambda pair: pair[0], reverse=True
        )
    ]


def derive_quote_estimate(cand: dict, cfg: dict) -> dict:
    p = cfg.get("pricing_estimate", {})
    window = int(p.get("reels_window", 10))
    default_cpm = float(p.get("default_cpm_usd", 35.0))
    low_cpm = float(p.get("cpm_min_usd", 35.0))
    high_cpm = float(p.get("cpm_max_usd", 40.0))

    eligible = []
    seen = set()
    posts = _ordered_posts(cand)
    pinned_excluded = sum(
        1 for post in posts if _is_reel(post) and post.get("pinned") is True
    )
    for post in posts:
        if not _is_reel(post):
            continue
        if post.get("pinned") is True:
            continue
        if post.get("pinned") is not False:
            continue
        views = _observed_views(post)
        if views is None:
            continue
        identity = _post_identity(post)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        eligible.append(
            {
                "code": post.get("code"),
                "url": post.get("url"),
                "taken_at": post.get("taken_at"),
                "play_count": int(round(views)),
                "metric_status": post.get("play_count_status") or "observed",
                "metric_source": (
                    post.get("play_count_source")
                    or "ig_media_info.ig_play_count"
                ),
                "pinned": False,
                "source": "instagram_media_info_ig_play_count",
                "captured_at": post.get("captured_at") or cand.get("captured_at"),
            }
        )
        if len(eligible) >= window:
            break

    if eligible:
        avg_decimal = sum(Decimal(x["play_count"]) for x in eligible) / Decimal(len(eligible))
        avg_views = float(avg_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        source = "instagram_media_info_ig_play_count"
        sample_count = len(eligible)
        samples = eligible
        status = "complete" if sample_count >= window else "partial"
    else:
        fallback = _positive_number(
            cand.get("avg_reels_plays") or cand.get("modash_avg_reels_plays")
        )
        avg_decimal = Decimal(str(fallback)) if fallback is not None else None
        avg_views = float(avg_decimal) if avg_decimal is not None else None
        source = "modash_profile_fallback" if avg_views is not None else "missing"
        sample_count = 0
        samples = []
        status = "fallback_modash" if avg_views is not None else "missing"

    out = {
        "schema_version": 1,
        "status": status,
        "currency": "USD",
        "method": "mean_recent_non_pinned_reels_x_cpm",
        "requested_reels": window,
        "sample_count": sample_count,
        "average_plays": avg_views,
        "source": source,
        "pinned_excluded": pinned_excluded,
        "cpm_usd": {"default": default_cpm, "min": low_cpm, "max": high_cpm},
        "quote_usd": {"default": None, "min": None, "max": None},
        "captured_at": cand.get("pricing_captured_at") or cand.get("captured_at"),
        "reels": samples,
    }
    if avg_decimal is not None:
        divisor = Decimal("1000")
        out["quote_usd"] = {
            "default": _money(avg_decimal / divisor * Decimal(str(default_cpm))),
            "min": _money(avg_decimal / divisor * Decimal(str(low_cpm))),
            "max": _money(avg_decimal / divisor * Decimal(str(high_cpm))),
        }
    return out
