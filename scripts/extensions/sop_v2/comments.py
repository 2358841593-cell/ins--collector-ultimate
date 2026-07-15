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


# 推广帖识别：caption 命中这些词 = 博主在带货/导购（复用 content.PRODUCT_REC 口径 + 电商域）
PROMO_CAPTION_KW = [
    "amazon find", "amazon storefront", "amazon must", "link in bio", "shop my", "linked it",
    "use code", "discount code", "code ", "must have", "must-have", "obsessed with", "holy grail",
    "repurchase", "haul", "restock", "linktree", "storefront", "en mi amazon", "en amazon",
    "código", "codigo", "descuento", "link en", "enlace en", "compra", "disponible en",
    "lo encuentras", "te dejo el link", "swipe up", "shopmy", "ltk", "liketoknow",
]


def is_promotional(caption: str) -> bool:
    """caption 是否是明显的带货/导购推广帖。"""
    c = (caption or "").lower()
    return any(k in c for k in PROMO_CAPTION_KW)


# 购买"考虑"问句：仅在已判定为推广帖时才算意图（context 消歧——同一句在教育帖不算）
CONSIDER_PHRASES = [
    "does this work", "does it work", "do they work", "did it work", "is it worth",
    "worth it", "how do you use", "how to use", "how do i use", "for oily skin", "for dry skin",
    "for sensitive skin", "for acne", "good for oily", "is this good for", "is it good for",
    "which one", "which shade", "what shade", "what size", "which do you recommend",
    "do you recommend", "would you recommend", "is it safe", "how often",
    "funciona", "vale la pena", "cómo se usa", "como se usa", "cómo lo usas", "como lo usas",
    "para piel grasa", "para piel seca", "para piel sensible", "para el acné", "para acne",
    "cuál me recomiendas", "cual me recomiendas", "cuál es mejor", "cual es mejor",
    "qué tono", "que tono", "lo recomiendas", "sirve para", "es bueno para", "cada cuánto",
    "funciona mesmo", "vale a pena", "serve para", "recomenda",
]

# 明确购买意图短语（高精度：只留清晰买信号，不含"有效/推荐"等辩论也会中的宽词）
INTENT_PHRASES = [
    # 求链接/在哪买（最强信号）
    "where is the link", "where's the link", "link please", "drop the link", "send the link",
    "where can i buy", "where to buy", "how do i buy", "is it in your storefront",
    "in your storefront", "which storefront", "share the link",
    "dónde lo compro", "donde lo compro", "dónde se compra", "donde se compra", "dónde comprar",
    "donde comprar", "cómo lo compro", "como lo compro", "dónde consigo", "donde consigo",
    "pásame el link", "pasame el link", "el link porfa", "onde comprar", "onde compro",
    "link do produto",
    # 问价
    "how much is", "how much does", "what's the price", "what is the price",
    "cuánto cuesta", "cuanto cuesta", "cuánto vale", "cuanto vale", "cuánto sale", "qué precio",
    "que precio", "quanto custa",
    # 购买确认
    "just ordered", "just bought", "i just bought", "already ordered", "in my cart",
    "adding to cart", "lo compré", "ya lo compré", "acabo de comprar", "lo pedí", "ya lo pedí",
    "lo acabo de comprar", "comprado ✓",
    # 明确想要 + 具体产品
    "i need this", "i want this", "lo quiero", "lo necesito", "quiero uno", "quiero comprar",
]


def find_intent_in_text(page_text: str, max_snippets: int = 4, promo_context: bool = False) -> list[str]:
    """从帖子页可见文字中提取"有购买意图"的评论片段（多语言）。
    promo_context=True（已判定推广帖）时，额外纳入"考虑购买"问句（does this work / vale la pena /
    para piel grasa 等）——同一句在教育帖不算意图，在推广帖下才算，靠上下文消歧。
    返回清洗后的短句列表；无则空。用于判断该帖评论是否有意义、是否值得截图。"""
    if not page_text:
        return []
    phrases = INTENT_PHRASES + CONSIDER_PHRASES if promo_context else INTENT_PHRASES
    # 按行/句切分，逐段找意图短语
    segs = re.split(r"[\n\r]+|(?<=[.?!。？！])\s+", page_text)
    out, seen = [], set()
    tl_meta = re.compile(r"^\s*(\d+\s*(天|周|小时|分钟|d|w|h|min|semanas?|días?|horas?)|回复|responder|reply|"
                         r"me gusta|likes?|verified|已验证|查看翻译|ver traducción|traducir)\s*$", re.I)
    for s in segs:
        s = s.strip()
        if not s or len(s) < 4 or len(s) > 140 or tl_meta.match(s):
            continue
        low = s.lower()
        if any(ph in low for ph in phrases):
            key = low[:40]
            if key not in seen:
                seen.add(key)
                out.append(s[:120])
                if len(out) >= max_snippets:
                    break
    return out


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
