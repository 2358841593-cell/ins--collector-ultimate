"""评论信任分析（C 模块：购买意图 / bot / 低质）。

对采集到的评论文本做三档购买意图识别 + bot/低质过滤，产出有效样本数、高意图条数/占比、
低质占比，供 scoring C 模块与 routing 固定 Review（有效样本<20）。
关键词表借鉴现有 discover.py 口径。
"""
from __future__ import annotations

import re

STRONG = ["ordered", "just bought", "in my cart", "link please", "where is the link",
          "where's the link", "purchased", "bought this", "just ordered", "adding to cart",
          "need the link", "dropping the link"]
MEDIUM = ["does this work", "what wavelength", "worth it", "is it in your storefront",
          "which folder", "for sensitive skin", "how much", "what's the code", "how do you use",
          "does it help with", "is this good for"]
WEAK = ["need this", "want this", "obsessed", "adding to list", "wishlist", "code", "discount",
        "where to buy", "so good"]
LOW_QUALITY = ["beautiful", "love this", "nice pic", "gorgeous", "wow", "so pretty", "amazing",
               "stunning", "great post", "love it", "perfect", "cute", "queen", "goals",
               "content", "keep it up", "great content", "so nice"]
EMOJI_RE = re.compile(r"^[\s\U0001F000-\U0001FAFF☀-➿←-⇿❤️♥️👏🔥😍�['\"]*]+$")
TAG_ONLY_RE = re.compile(r"^\s*(@[\w.]+\s*)+$")


def _intent(text: str):
    t = text.lower()
    if any(k in t for k in STRONG):
        return "strong"
    if any(k in t for k in MEDIUM):
        return "medium"
    if any(k in t for k in WEAK):
        return "weak"
    return None


def _is_low_quality(text: str) -> bool:
    t = text.strip()
    if not t or EMOJI_RE.match(t) or TAG_ONLY_RE.match(t):
        return True
    tl = t.lower()
    # 短且仅泛泛吹捧
    if len(t) < 30 and any(k in tl for k in LOW_QUALITY) and _intent(t) is None:
        return True
    return False


def analyze(comment_texts: list[str], pod_library: set | None = None) -> dict:
    pod_library = pod_library or set()
    total = len(comment_texts)
    if total == 0:
        return {"comments_analyzed": 0, "valid_comments": 0, "high_intent_count": 0,
                "high_intent_ratio": None, "low_quality_ratio": None, "top_intent": []}

    low = 0
    intents = []
    top_intent = []
    for text in comment_texts:
        if _is_low_quality(text):
            low += 1
            continue
        it = _intent(text)
        if it:
            intents.append(it)
            if it in ("strong", "medium") and len(top_intent) < 3:
                top_intent.append(text.strip()[:120])

    valid = total - low
    high_intent = len([i for i in intents if i in ("strong", "medium", "weak")])
    high_ratio = round(high_intent / valid * 100, 1) if valid else 0.0
    return {
        "comments_analyzed": total,
        "valid_comments": valid,
        "high_intent_count": high_intent,
        "high_intent_ratio": high_ratio,
        "low_quality_ratio": round(low / total * 100, 1),
        "top_intent": top_intent,
    }
