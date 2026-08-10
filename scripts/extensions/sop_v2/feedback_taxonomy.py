"""客户反馈的稳定分类合同。

这里定义的是客户为什么批准/拒绝账号的结构化标签，而不是机器 Gate 的
``reason_code``。客户原始文字必须始终另存为 ``reason_raw``；LLM 只能建议标签，
不能自行把 ``policy_signal`` 提升为 ``confirmed_policy``。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable


TAXONOMY_VERSION = "1.0.0"


@dataclass(frozen=True)
class FeedbackReason:
    code: str
    label_zh: str
    category: str
    change_class: str


REASON_DEFINITIONS: tuple[FeedbackReason, ...] = (
    FeedbackReason("geo_creator_outside", "创作者本人不在目标国家", "国家地区", "system_or_policy"),
    FeedbackReason("geo_audience_outside", "主要受众不在目标国家", "国家地区", "system_or_policy"),
    FeedbackReason("geo_mismatch", "创作者国家与主要受众国家不一致", "国家地区", "system_or_policy"),
    FeedbackReason("identity_not_influencer", "不是个人创作者 / Influencer", "账号身份", "ranking_signal"),
    FeedbackReason("identity_brand_or_store", "品牌、店铺或机构账号", "账号身份", "system_defect"),
    FeedbackReason("identity_medical", "医生、诊所或医疗机构", "账号身份", "policy_confirmation"),
    FeedbackReason("niche_skincare_insufficient", "护肤内容不足", "内容赛道", "ranking_signal"),
    FeedbackReason("niche_wellness_insufficient", "Wellness 内容不足", "内容赛道", "policy_confirmation"),
    FeedbackReason("niche_makeup_dominant", "彩妆内容占比过高", "内容赛道", "ranking_signal"),
    FeedbackReason("niche_hair_dominant", "美发内容占比过高", "内容赛道", "ranking_signal"),
    FeedbackReason("niche_travel_dominant", "旅行内容占比过高", "内容赛道", "ranking_signal"),
    FeedbackReason("format_product_hold_talk", "主要是手持产品讲解", "内容形式", "policy_confirmation"),
    FeedbackReason("format_no_human_face", "缺少真人露脸", "内容形式", "policy_confirmation"),
    FeedbackReason("content_ai_generated", "疑似 AI 生成内容", "内容形式", "ranking_signal"),
    FeedbackReason("quality_video_low", "视频制作质量较低", "内容质量", "ranking_signal"),
    FeedbackReason("quality_brand_fit_low", "品牌调性匹配度低", "内容质量", "ranking_signal"),
    FeedbackReason("engagement_low", "整体互动偏低", "互动质量", "ranking_signal"),
    FeedbackReason("engagement_no_comments", "评论过少或没有评论", "互动质量", "data_quality_or_ranking"),
    FeedbackReason("comments_fake_or_pod", "评论疑似虚假或互赞群", "互动质量", "ranking_signal"),
    FeedbackReason("followers_fake_high", "疑似假粉比例过高", "互动质量", "system_defect"),
    FeedbackReason("commerce_link_missing", "缺少可用购物或商品入口", "商业基础", "policy_confirmation"),
    FeedbackReason("profile_unavailable", "主页不可访问或暂时异常", "可采集性", "temporary_recheck"),
    FeedbackReason("brand_safety_skin_severity", "皮肤问题呈现不符合品牌安全要求", "品牌安全", "policy_confirmation"),
    FeedbackReason("other", "其他（请在补充说明中描述）", "其他", "manual_review"),
)

REASON_TAGS = frozenset(item.code for item in REASON_DEFINITIONS)
FEEDBACK_SCOPES = frozenset(
    {"account", "fact_correction", "policy_signal", "confirmed_policy"}
)
REJECTION_SCOPES = frozenset({"global", "campaign", "temporary"})
EVIDENCE_STATUSES = frozenset({"unverified", "verified", "contradicted"})


class FeedbackTaxonomyError(ValueError):
    """结构化客户反馈不符合当前 taxonomy。"""


def normalize_reason_tags(
    value: Iterable[str] | None,
) -> list[str]:
    """校验、去重并保留标签顺序。

    原因始终可选：客户只给出 verdict 也构成完整反馈。空标签只回填账号结果，
    后续分析不得把它推断成任何策略信号。
    """
    if value is None:
        tags: list[str] = []
    elif isinstance(value, (str, bytes)):
        raise FeedbackTaxonomyError("reason_tags 必须是字符串数组")
    else:
        try:
            tags = list(value)
        except TypeError as exc:
            raise FeedbackTaxonomyError("reason_tags 必须是字符串数组") from exc
    normalized: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            raise FeedbackTaxonomyError("reason_tags 只能包含非空字符串")
        code = tag.strip()
        if code not in REASON_TAGS:
            raise FeedbackTaxonomyError(f"未知 reason_tag：{code}")
        if code not in normalized:
            normalized.append(code)
    return normalized


def normalize_feedback_scope(value: str | None, *, default: str = "account") -> str:
    scope = default if value in (None, "") else value
    if not isinstance(scope, str) or scope not in FEEDBACK_SCOPES:
        raise FeedbackTaxonomyError(f"未知 feedback_scope：{scope!r}")
    return scope


def normalize_rejection_scope(value: str | None, *, default: str = "campaign") -> str:
    scope = default if value in (None, "") else value
    if not isinstance(scope, str) or scope not in REJECTION_SCOPES:
        raise FeedbackTaxonomyError(f"未知 rejection_scope：{scope!r}")
    return scope


def normalize_evidence_status(value: str | None, *, default: str = "unverified") -> str:
    status = default if value in (None, "") else value
    if not isinstance(status, str) or status not in EVIDENCE_STATUSES:
        raise FeedbackTaxonomyError(f"未知 evidence_status：{status!r}")
    return status


def taxonomy_payload() -> dict:
    """供交付 HTML 和文档生成器消费，避免前后端维护两套标签。"""
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "reasons": [asdict(item) for item in REASON_DEFINITIONS],
        "feedback_scopes": sorted(FEEDBACK_SCOPES),
        "rejection_scopes": sorted(REJECTION_SCOPES),
        "evidence_statuses": sorted(EVIDENCE_STATUSES),
    }
