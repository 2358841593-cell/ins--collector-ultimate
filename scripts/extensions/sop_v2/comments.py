"""评论信任分析（C 模块：购买意图 / bot / 低质）。

对采集到的评论文本做三档购买意图识别 + bot/低质过滤，产出有效样本数、高意图条数/占比、
低质占比，供 scoring C 模块与 routing 固定 Review（有效样本<20）。
关键词表借鉴现有 discover.py 口径。
"""
from __future__ import annotations

import re
from collections.abc import Mapping


EMPTY_THREAD_EVIDENCE_SCHEMA = "instagram-empty-comment-thread-v1"
EMPTY_THREAD_EVIDENCE_SOURCE = "instagram_visible_dom+comments_endpoint"
EMPTY_THREAD_REASON = "verified_empty_thread_despite_reported_count"
# Stage 3 forces an English Instagram UI.  Keep this allow-list exact and
# intentionally narrow: a fuzzy body-text match could turn a loading/error page
# into false completion evidence.
VISIBLE_EMPTY_THREAD_MARKERS = frozenset({"No comments yet."})
_MEDIA_IDENTITY_RE = re.compile(
    r"/(?:[^/?#]+/)?(?:reel|p)/([A-Za-z0-9_-]+)", re.IGNORECASE
)

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
               "content", "keep it up", "great content", "so nice",
               # LLM 中文译文的泛夸赞/互赞噪声。具体产品热情在 LOW_INTENT 中优先识别。
               "太漂亮了", "好漂亮", "真漂亮", "太美了", "好美", "真美", "好可爱",
               "太可爱了", "很棒的内容", "内容很棒", "很棒的帖子", "拍得真好"]
EMOJI_RE = re.compile(r"^[\s\U0001F000-\U0001FAFF☀-➿←-⇿❤️♥️👏🔥😍�['\"]*]+$")
TAG_ONLY_RE = re.compile(r"^\s*(@[\w.]+\s*)+$")


def _media_identity(value) -> str:
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("code")
    match = _MEDIA_IDENTITY_RE.search(str(value or ""))
    return match.group(1) if match else ""


def comment_media_identity(value) -> str:
    """Return the stable shortcode identity used by strict retry validation."""
    return _media_identity(value)


def verified_empty_thread_evidence(
    post: Mapping | None,
    unavailable_entry: Mapping | None = None,
) -> bool:
    """Validate the fail-closed proof for a visibly empty IG comment thread.

    This state is deliberately distinct from ``verified_zero``: the post keeps
    its positive media-info/OG ``comment_count`` while a current, visible empty
    marker and the authenticated structured comments endpoint independently
    prove that no Instagram or Facebook comment rows are publicly available.
    """
    if not isinstance(post, Mapping):
        return False
    if post.get("comment_sampling_status") != "verified_empty_thread":
        return False
    try:
        reported_count = int(post.get("comment_count"))
        collected_count = int(post.get("comments_collected") or 0)
    except (TypeError, ValueError):
        return False
    if reported_count <= 0 or collected_count != 0:
        return False

    evidence = post.get("comment_empty_thread_evidence")
    if not isinstance(evidence, Mapping):
        return False
    marker = evidence.get("marker")
    if (
        evidence.get("schema") != EMPTY_THREAD_EVIDENCE_SCHEMA
        or evidence.get("source") != EMPTY_THREAD_EVIDENCE_SOURCE
        or evidence.get("marker_visible") is not True
        or marker not in VISIBLE_EMPTY_THREAD_MARKERS
        or evidence.get("reported_count") != reported_count
    ):
        return False

    endpoint = evidence.get("endpoint")
    if not isinstance(endpoint, Mapping):
        return False
    required_endpoint = {
        "http_status": 200,
        "status": "ok",
        "comment_count": 0,
        "comments_count": 0,
        "fb_comments_count": 0,
        "has_more_comments": False,
        "has_more_headload_comments": False,
        "has_more_headload_fb_comments": False,
    }
    if any(endpoint.get(key) != value for key, value in required_endpoint.items()):
        return False

    if unavailable_entry is None:
        return True
    if not isinstance(unavailable_entry, Mapping):
        return False
    return (
        _media_identity(unavailable_entry) == _media_identity(post)
        and unavailable_entry.get("reported_count") == reported_count
        and unavailable_entry.get("reason") == EMPTY_THREAD_REASON
        and unavailable_entry.get("source") == EMPTY_THREAD_EVIDENCE_SOURCE
        and unavailable_entry.get("marker") == marker
        and unavailable_entry.get("endpoint_summary") == dict(endpoint)
    )


def repeated_low_comment_unavailable(
    post: Mapping | None,
    unavailable_entry: Mapping | None,
) -> bool:
    """Validate the older bounded 1–2-comment retry terminal."""
    if not isinstance(post, Mapping) or not isinstance(
        unavailable_entry, Mapping
    ):
        return False
    try:
        reported_count = int(post.get("comment_count"))
        collected_count = int(post.get("comments_collected") or 0)
    except (TypeError, ValueError):
        return False
    return (
        post.get("comment_sampling_status") == "unavailable_after_retry"
        and reported_count in (1, 2)
        and collected_count == 0
        and _media_identity(unavailable_entry) == _media_identity(post)
        and unavailable_entry.get("reported_count") == reported_count
        and unavailable_entry.get("reason")
        == "reported_low_count_unavailable_after_retry"
    )


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
    "available now", "shop now", "grab yours", "in my bio", "tap the link", "on sale",
    "new drop", "launching", "available at", "get yours", "order now", "pre-order",
]
# 产品型帖（展示具体产品即算导购帖——很多带货帖不带联盟话术，只展示产品名/品类）
PRODUCT_TYPE_KW = [
    "cleanser", "serum", "moisturizer", "moisturiser", "sunscreen", "spf", "toner", "retinol",
    "niacinamide", "hyaluronic", "vitamin c", "facial oil", "face oil", "body lotion", "body butter",
    "eye cream", "face cream", "face mask", "sheet mask", "exfoliant", "peeling", "essence",
    "lip balm", "lip oil", "foundation", "concealer", "mascara", "lipstick", "bronzer", "blush",
    "led mask", "gua sha", "microcurrent", "roller", "bundle", "kit", "set", "collection",
]


def is_promotional(caption: str) -> bool:
    """caption 是否是导购/产品展示帖：命中带货话术 或 明确产品品类（展示具体产品也算）。"""
    c = (caption or "").lower()
    return any(k in c for k in PROMO_CAPTION_KW) or any(k in c for k in PRODUCT_TYPE_KW)


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
    # 德语
    "funktioniert das", "funktioniert es", "lohnt sich das", "lohnt es sich", "für fettige haut",
    "für trockene haut", "für empfindliche haut", "gut für", "welche", "welches", "empfiehlst du",
    "wie oft", "wie benutzt man",
    # 法语
    "est-ce que ça marche", "ça marche", "ça vaut le coup", "pour peau grasse", "pour peau sèche",
    "pour peau sensible", "tu recommandes", "lequel", "laquelle", "à quelle fréquence",
    # 意大利语
    "funziona", "ne vale la pena", "per pelle grassa", "per pelle secca", "per pelle sensibile",
    "lo consigli", "quale", "quanto spesso", "come si usa",
    # 任意语言经 LLM 统一翻成中文后使用同一意图口径
    "有效吗", "有用吗", "真的有效", "值得买吗", "值得入手吗", "怎么使用", "怎么用",
    "如何使用", "适合油皮", "适合干皮", "适合敏感肌", "适合痘痘肌", "推荐哪个",
    "哪个更好", "你推荐吗", "多久用一次", "多长时间用一次", "可以叠加吗",
    "适合我的皮肤吗", "对敏感皮肤安全吗",
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
    # 德语（DE 目标市场）
    "wo kann ich", "wo gibt es", "wo bekomme ich", "link bitte", "wo bestellen", "wo kaufen",
    "wie viel kostet", "was kostet", "gerade bestellt", "schon bestellt", "ich brauche das",
    "ich will das", "muss ich haben", "wo finde ich",
    # 法语（FR 目标市场）
    "où acheter", "où l'acheter", "le lien svp", "le lien stp", "combien ça coûte", "c'est combien",
    "je viens de commander", "j'ai commandé", "j'ai besoin de ça", "je le veux", "je veux ça",
    "où le trouver",
    # 意大利语（IT 目标市场）
    "dove comprare", "dove lo compro", "il link per favore", "quanto costa", "appena ordinato",
    "l'ho ordinato", "lo voglio", "mi serve questo", "dove si compra",
    # 任意语言经 LLM 统一翻成中文后使用同一意图口径
    "在哪里可以买", "在哪可以买", "哪里可以买", "在哪里买", "哪里购买", "购买链接",
    "求链接", "发一下链接", "把链接发给我", "链接在哪里", "链接是什么",
    "产品链接是什么", "产品链接在哪", "怎么购买", "怎么买",
    "多少钱", "价格是多少", "什么价格", "刚刚下单", "刚下单", "已经下单",
    "我已经买了", "刚买了", "加入购物车", "放进购物车", "我想买", "我要买",
]


def find_intent_in_text(page_text: str, max_snippets: int = 4, promo_context: bool = False) -> list[str]:
    """从帖子页可见文字中提取"有购买意图"的评论片段（多语言）。
    promo_context=True（已判定推广帖）时，额外纳入"考虑购买"问句（does this work / vale la pena /
    para piel grasa 等）——同一句在教育帖不算意图，在推广帖下才算，靠上下文消歧。
    返回清洗后的短句列表；无则空。用于判断该帖评论是否有意义、是否值得截图。"""
    if not page_text:
        return []
    phrases = INTENT_PHRASES + CONSIDER_PHRASES if promo_context else INTENT_PHRASES
    out, seen = [], set()
    for s in segment_comments(page_text):
        low = s.lower()
        if any(ph in low for ph in phrases):
            key = low[:40]
            if key not in seen:
                seen.add(key)
                out.append(s[:120])
                if len(out) >= max_snippets:
                    break
    return out


_TL_META = re.compile(r"^\s*(\d+\s*(天|周|小时|分钟|d|w|h|min|semanas?|días?|horas?)|回复|responder|reply|"
                      r"me gusta|likes?|verified|已验证|查看翻译|ver traducción|traducir|"
                      r"following|follow|seguir|siguiendo|liked by|comment|add a comment|"
                      r"more posts|más publicaciones)\s*$", re.I)


def segment_comments(page_text: str) -> list[str]:
    """把帖子页可见文字切成"候选评论行"（去 UI/时间戳/回复等噪声）。
    供 find_intent_in_text（找购买意图）与 analyze（评论质量/有效样本数）复用。"""
    if not page_text:
        return []
    out = []
    for s in re.split(r"[\n\r]+|(?<=[.?!。？！])\s+", page_text):
        s = s.strip()
        if not s or len(s) < 4 or len(s) > 200 or _TL_META.match(s):
            continue
        out.append(s)
    return out


# 低级意图：真诚的"想要/产品热情"（不必求链接；非水军的夸赞也算）。排掉夸人/夸照片的泛词。
LOW_INTENT = [
    "need this", "i need this", "need it", "want this", "i want this", "want it", "i want one",
    "obsessed", "must have", "must-have", "have to try", "gotta try", "want to try", "need to try",
    "trying this", "on my list", "adding to my list", "wishlist", "wish list", "in love",
    "so good", "looks amazing", "looks so good", "love this", "i love this", "love it",
    "the formula", "game changer", "life changing", "the best", "best ever", "best product",
    "amazing product", "obsessed with",
    "lo quiero", "lo necesito", "necesito esto", "quiero probar", "quiero uno", "me encanta",
    "amo esto", "el mejor", "la mejor", "increíble", "necesito uno", "quiero comprar",
    "amei", "preciso disso", "o melhor", "maravilhoso", "adorei",
    # 德语：真诚想要/产品热情
    "brauche das", "will das", "ich brauche", "muss ich haben", "auf meiner liste", "liebe es",
    "liebe das", "so gut", "so schön", "wunderschön", "das beste", "der beste", "die beste",
    "besessen", "unbedingt", "will ich haben", "sieht toll aus", "sieht so gut aus",
    # 法语：真诚想要/产品热情
    "j'adore", "besoin de ça", "je veux ça", "il me faut", "sur ma liste", "le meilleur",
    "la meilleure", "incroyable", "magnifique", "trop bien", "j'en ai besoin", "obsédée",
    # 意大利语：真诚想要/产品热情
    "lo adoro", "mi serve", "lo voglio", "il migliore", "la migliore", "bellissimo",
    "bellissima", "incredibile", "ne ho bisogno", "sulla mia lista", "stupendo",
    # 任意语言经 LLM 统一翻成中文后的真实产品热情
    "我需要这个", "我想要这个", "我也想要", "必须拥有", "一定要试试", "我想试试",
    "种草了", "加入愿望清单", "放进愿望清单", "加入我的清单", "这个产品太棒了",
    "太好用了", "我喜欢这个产品", "最好的产品", "改变游戏规则",
]
# 互赞团/夸内容/夸人 → 水军，不算意图（哪怕是"夸"）
_POD_RE = re.compile(r"your\s+(content|videos?|reels?|feed|page|style)|content\s+(creator|is|looks)|"
                     r"keep\s+(it\s+up|posting|going)|love\s+your|(great|amazing)\s+content|"
                     r"nice\s+(pic|post|shot|photo)|great\s+post|"
                     r"你的(内容|视频|主页|风格)|继续(加油|发布|更新)|很棒的(内容|帖子)|"
                     r"喜欢你的(内容|视频)|照片拍得(真好|很棒)", re.I)


def grade_intent(text: str):
    """购买意图三级：high(求链接/已下单) / medium(考虑/问适用) / low(真诚产品热情/想要) / None。
    None = 水军/夸人夸照片/纯emoji/纯tag（不算意图）。"""
    t = (text or "").strip()
    if not t or len(t) < 3 or EMOJI_RE.match(t) or TAG_ONLY_RE.match(t):
        return None
    low = t.lower()
    if any(p in low for p in INTENT_PHRASES):
        return "high"
    if any(p in low for p in CONSIDER_PHRASES):
        return "medium"
    if _POD_RE.search(low):            # 互赞团/夸内容/夸照片 → 水军
        return None
    if any(p in low for p in LOW_INTENT):   # 真诚"想要/产品好"——低级意图（用户口径）
        return "low"
    return None


_GRADE_ZH = {"high": "高", "medium": "中", "low": "低"}


def find_intent_comments(comments: list[dict], promo_context: bool = True, max_out: int = 8) -> list[dict]:
    """从 {username,text} 评论对里挑有购买意图的（三级），保留"谁说的" + 级别。
    返回 [{'username','text','grade','grade_zh'}]（去重、按级别高→低排）。"""
    graded, seen = [], set()
    for c in comments or []:
        t = (c.get("text") or "").strip()
        if not t or len(t) > 220:
            continue
        g = grade_intent(t)
        if not g:
            continue
        key = t.lower()[:40]
        if key in seen:
            continue
        seen.add(key)
        graded.append({"username": (c.get("username") or "").lstrip("@"), "text": t[:180],
                       "grade": g, "grade_zh": _GRADE_ZH[g]})
    order = {"high": 0, "medium": 1, "low": 2}
    graded.sort(key=lambda x: order[x["grade"]])
    return graded[:max_out]


def _is_low_quality(text: str) -> bool:
    t = text.strip()
    if not t or EMOJI_RE.match(t) or TAG_ONLY_RE.match(t):
        return True
    tl = t.lower()
    # 短且仅泛泛吹捧
    if len(t) < 30 and any(k in tl for k in LOW_QUALITY) and _intent(t) is None:
        return True
    return False


def is_low_quality(text: str) -> bool:
    """公开的低质判断入口，供 LLM 中文译文复用同一业务语义。"""
    return _is_low_quality(text)


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
