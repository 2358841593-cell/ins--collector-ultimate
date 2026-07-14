#!/usr/bin/env python3
"""把一次 run 的 discovery JSON 导成「带字段说明行」的详细 CSV。

格式（正是客户要的）：
  第 1 行 = 字段名
  第 2 行 = 该字段的【评分逻辑 / 样本数 / 数据来源】（方法学说明）
  第 3 行起 = 每个账号的具体数据，**每一格都填**（无值也给"结果"：N/A·需Modash / 未分析 / 无 / —）
末列「score_detail 评分逐项依据」= 该账号综合分的每一维 原始值×权重=贡献 + 依据（极其详细）。

默认读最新 data/runs/discovery-*.json。
用法: export_explained_csv.py [输入.json] [输出.csv]
"""
from __future__ import annotations

import csv
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "runs"

# 列顺序（与标准 discovery CSV 一致）
COLS = [
    "handle", "profile_url", "full_name", "follower_count", "following_count",
    "media_count", "is_verified", "tier",
    "has_amazon_storefront", "bio_link_type", "bio_link_url",
    "creator_archetype", "product_rec_ratio", "skincare_authority_score", "authority_evidence",
    "niche_primary", "niche_primary_label", "niche_secondary", "campaign_fit", "campaign_fit_label",
    "sponsored_count", "sponsored_ratio", "organic_amazon_posts_count",
    "reels_count", "reels_avg_likes", "reels_avg_comments", "reels_engagement_rate",
    "static_count", "static_avg_likes", "static_avg_comments", "static_engagement_rate",
    "meets_er_benchmark",
    "comments_analyzed", "purchase_intent_ratio", "bot_comment_ratio", "engagement_pod_ratio",
    "low_quality_ratio", "trust_level", "top_intent_comments", "intent_keywords_found",
    "pod_comment_samples", "pod_commenters", "pod_commenters_count",
    "modash_credibility", "modash_fake_pct", "modash_audience_us_pct", "modash_audience_female_pct",
    "modash_audience_age_18_34_pct", "modash_avg_likes", "modash_avg_reels_plays", "modash_er",
    "discovery_sources", "discovery_score", "status", "filter_reasons", "review_reasons",
]

# 第 2 行：每个字段的评分逻辑 / 样本数 / 来源
METHOD = {
    "handle": "IG 用户名（唯一标识）。来源：profile。",
    "profile_url": "主页直链，供人工抽查。",
    "full_name": "IG 显示名（原样）。",
    "follower_count": "粉丝数（profile 原值）。漏斗硬门槛：1万–15万，越界即排除。",
    "following_count": "关注数（profile 原值，真人特征参考）。",
    "media_count": "历史发帖总数（profile 原值，活跃度参考）。",
    "is_verified": "IG 蓝V 认证（profile 原值）。",
    "tier": "量级：粉丝<10万=micro，10万–15万=mid。决定互动率基准线。",
    "has_amazon_storefront": "Amazon 橱窗。True=bio 直链/文本含 amazon.com/shop；unverified=检到 Linktree/Beacons/LTK 等聚合页但默认不穿透（交浏览器/人工核实）；False=无。4 遍扫描全部 bio 链接。",
    "bio_link_type": "bio 链接类型：amazon_direct / amazon_in_bio_text / linktree / beacons / ltk / stan_store / other / none。",
    "bio_link_url": "命中的 bio 链接 URL（证据，可点开核验）。",
    "creator_archetype": "内容类型：产品推荐占比≥40%=amazon_finds，≥20%=mixed，<20%=lifestyle。样本：近 20 帖。",
    "product_rec_ratio": "产品推荐占比 = 含推荐词的帖数 / 20。样本：近 20 帖 caption。",
    "skincare_authority_score": "护肤/设备权威度 = Σ(各类目命中词数×权重)×5，封顶 100。权重：成分×3·设备规格×4·皮肤科学×3·适用场景×2。样本：20 帖 caption 全文。",
    "authority_evidence": "命中的专业术语（证据，支撑权威度分）。",
    "niche_primary": "主赛道（英文键）。打分=bio 命中词×3 + caption 命中词×1，取最高。",
    "niche_primary_label": "主赛道（中文）。共 11 垂类。",
    "niche_secondary": "次赛道（得分≥主赛道 30% 者）。",
    "campaign_fit": "对口度：core=护肤/美容仪/美妆 · related=美发/时尚/健身/母婴/生活 · off=理财/美食/旅行。",
    "campaign_fit_label": "对口度中文（对口/相关/跨垂类）。直接决定综合分系数 ×1.0 / 0.9 / 0.55。",
    "sponsored_count": "赞助帖数。双信号：caption 含赞助标记 或 sponsor_tags。样本：近 15 帖。",
    "sponsored_ratio": "赞助占比 = 赞助帖 / 15。漏斗门槛：≤40%（过饱和判不真诚）。",
    "organic_amazon_posts_count": "非赞助的自然产品推荐帖数（近 15 帖）。",
    "reels_count": "近 20 帖中 Reels 数（media_type=2 / clip）。",
    "reels_avg_likes": "Reels 平均赞。",
    "reels_avg_comments": "Reels 平均评论。",
    "reels_engagement_rate": "Reels 互动率 % =（均赞+均评）/ 粉丝 ×100。",
    "static_count": "近 20 帖中图文数。",
    "static_avg_likes": "图文平均赞。",
    "static_avg_comments": "图文平均评论。",
    "static_engagement_rate": "图文互动率 % =（均赞+均评）/ 粉丝 ×100。",
    "meets_er_benchmark": "是否达互动基准。micro：Reels≥3.0% 或 图文≥1.8%；mid：≥1.5%。",
    "comments_analyzed": "实际分析评论数。样本：互动最高 4 帖 + 最近 4 帖（去重≤8 帖）× 每帖 25 条 → 最多≈200 条。",
    "purchase_intent_ratio": "购买意图占比 = 意图评论 / 有效评论（总−低质）。意图词分强(×3:where to buy/link/购买)·中(×2)·弱(×1)。",
    "bot_comment_ratio": "机器评论占比 = bot(纯 emoji/纯@/单词吹捧/spam) / 总评论。",
    "engagement_pod_ratio": "互赞团占比 = pod / 总评论。三信号：命中水军库 / 同账号跨≥2 帖刷评 / 泛泛吹捧。",
    "low_quality_ratio": "刷量占比 =（bot+pod）/ 总评论。≥50% → 信任 low 且 include 降级 review。",
    "trust_level": "评论信任：high(意图≥15% 且 低质<40%) / medium / low。",
    "top_intent_comments": "最强购买意图评论样例（含原帖 code 可跳转）。证据。",
    "intent_keywords_found": "命中的购买意图词（证据）。",
    "pod_comment_samples": "互赞团评论样例（证据）。",
    "pod_commenters": "被判互赞团的账号（回写持久水军库）。",
    "pod_commenters_count": "本次新判互赞团账号数。",
    "modash_credibility": "Modash 真实粉丝比例。本工具未接入 Modash → N/A。<0.60 则在评分阶段直接排除。",
    "modash_fake_pct": "Modash 假粉率。需 Modash API。",
    "modash_audience_us_pct": "Modash 粉丝美国占比。需 Modash API。",
    "modash_audience_female_pct": "Modash 粉丝女性占比。需 Modash API。",
    "modash_audience_age_18_34_pct": "Modash 粉丝 18-34 占比。需 Modash API。",
    "modash_avg_likes": "Modash 历史均赞。需 Modash API。",
    "modash_avg_reels_plays": "Modash Reels 均播放。需 Modash API。",
    "modash_er": "Modash 口径互动率。需 Modash API。",
    "discovery_sources": "发现来源（如 brand_tagged:currentbody / lookalike:xxx）。每源在评分里 ×25。",
    "discovery_score": "综合分 = Σ(各维子分 0-100 × 权重) × 赛道对口系数。无 Modash 权重：互动0.25·产品推荐0.20·购买意图0.20·权威0.15·赞助健康0.10·证据0.10；系数 core1.0/related0.9/off0.55。详见末列。",
    "status": "判定：include 纳入 / review 待核验 / exclude 排除。",
    "filter_reasons": "排除原因（粉丝越界 / 无橱窗 / 品牌号 等）。",
    "review_reasons": "待核验原因（linkinbio 需人工查 / 互动低 / 评论不可用 等）。",
}

EXTRA = "score_detail（评分逐项依据）"
METHOD_EXTRA = "综合分每一维：原始值×权重=贡献分 + 依据；末乘赛道对口系数。仅 include/review 有。"

# 这些是"深度分析"字段：账号若在漏斗早被排除则未计算
ANALYSIS_FIELDS = {
    "creator_archetype", "product_rec_ratio", "skincare_authority_score", "authority_evidence",
    "sponsored_count", "sponsored_ratio", "organic_amazon_posts_count",
    "reels_count", "reels_avg_likes", "reels_avg_comments", "reels_engagement_rate",
    "static_count", "static_avg_likes", "static_avg_comments", "static_engagement_rate",
    "meets_er_benchmark", "comments_analyzed", "purchase_intent_ratio", "bot_comment_ratio",
    "engagement_pod_ratio", "low_quality_ratio", "trust_level", "top_intent_comments",
    "intent_keywords_found", "pod_comment_samples", "pod_commenters", "pod_commenters_count",
    "discovery_score",
}


def fmt(cand: dict, key: str) -> str:
    v = cand.get(key, None)
    status = cand.get("status", "")
    if key.startswith("modash_") and (v is None or v == ""):
        return "N/A·需Modash"
    if v is None or v == "" or v == []:
        if key in ANALYSIS_FIELDS and status == "exclude":
            return "未分析（漏斗已排除）"
        if v == []:
            return "无"
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, list):
        if v and isinstance(v[0], dict):  # top_intent_comments
            parts = []
            for d in v[:5]:
                t = (d.get("text") or "").replace("\n", " ").strip()
                kw = d.get("keyword", "")
                parts.append(f"「{t}」[{kw}]")
            return " ｜ ".join(parts)
        return "; ".join(str(x) for x in v)
    return str(v)


def score_detail(cand: dict) -> str:
    sb = cand.get("score_breakdown")
    if not isinstance(sb, dict) or not sb.get("components"):
        return "未评分（漏斗已排除）" if cand.get("status") == "exclude" else "—"
    lines = []
    for c in sb["components"]:
        lines.append(f"{c.get('name')}: {c.get('raw')}×{c.get('weight')}={c.get('contribution')} ｜ {c.get('basis','')}")
    lines.append(f"= 基础 {sb.get('base_total')} × 对口系数 {sb.get('fit_multiplier')} = 总分 {sb.get('total')}（{sb.get('mode','')}）")
    return "\n".join(lines)


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        js = sorted(glob.glob(str(RUNS / "discovery-*.json")))
        if not js:
            raise SystemExit("找不到 data/runs/discovery-*.json")
        src = js[-1]
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    cands = data.get("candidates", data if isinstance(data, list) else [])
    rank = {"include": 0, "review": 1, "exclude": 2, "error": 3}
    cands.sort(key=lambda c: (rank.get(c.get("status", ""), 9), -(c.get("discovery_score") or 0)))

    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(src).with_name(Path(src).stem + "-explained.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLS + [EXTRA])
        w.writerow([METHOD.get(k, "") for k in COLS] + [METHOD_EXTRA])
        for c in cands:
            w.writerow([fmt(c, k) for k in COLS] + [score_detail(c)])

    n_full = sum(1 for c in cands if c.get("status") in ("include", "review"))
    print(f"输入: {src}")
    print(f"账号: {len(cands)}（深度分析 {n_full} 个 + 排除 {len(cands)-n_full} 个）")
    print(f"输出: {out}")


if __name__ == "__main__":
    main()
