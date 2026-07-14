"""五池路由测试（P0-11 / QA-03）：互斥、固定 Review 优先、分数分层。"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

from extensions.sop_v2 import gates, routing, scoring  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.contracts import Pool  # noqa: E402
from _fixtures import clean_full  # noqa: E402

CFG = load_config()


def full_route(cand, score_norm=None):
    gr = gates.evaluate_gates(cand, CFG)
    if score_norm is None:
        summ = scoring.normalize(scoring.score_all(cand, CFG))
    else:
        summ = {"normalized_total": score_norm, "ai_vetting_score": round(score_norm / 10, 1)}
    return routing.route(cand, gr, summ, CFG)


class TestPools(unittest.TestCase):
    def test_clean_include_with_storefront(self):
        r = full_route(clean_full())
        self.assertEqual(r["pool"], Pool.INCLUDE_WITH_STOREFRONT)

    def test_include_without_storefront(self):
        # confirmed_no：storefront gate PASS、D2 N/A、非固定 Review
        r = full_route(clean_full(storefront_status="confirmed_no"))
        self.assertEqual(r["pool"], Pool.INCLUDE_WITHOUT_STOREFRONT)

    def test_hard_gate_exclude(self):
        r = full_route(clean_full(fake_pct=25.0))
        self.assertEqual(r["pool"], Pool.EXCLUDE)
        self.assertIn("fake_followers_high", r["exclude_reasons"])

    def test_fixed_review_overrides_high_score(self):
        # 满分向但缺实际报价 → 固定 Review，不得 Include
        r = full_route(clean_full(paid_cpm=None))
        self.assertEqual(r["pool"], Pool.REVIEW)
        self.assertIn("paid_quote_missing", r["review_reasons"])

    def test_storefront_unknown_fixed_review(self):
        r = full_route(clean_full(storefront_status="unknown"))
        self.assertEqual(r["pool"], Pool.REVIEW)
        self.assertIn("storefront_unknown", r["review_reasons"])

    def test_comments_insufficient_review(self):
        r = full_route(clean_full(valid_comments=19))
        self.assertEqual(r["pool"], Pool.REVIEW)
        self.assertIn("comments_insufficient", r["review_reasons"])

    def test_gifting_priority_pool(self):
        r = full_route(clean_full(campaign_track="gifting", follower_count=40000, paid_cpm=None))
        # gifting 缺 cpm 不算固定 Review（gifting F1 N/A），30-50K → Priority Review
        self.assertEqual(r["pool"], Pool.PRIORITY_REVIEW)

    def test_lifestyle_cap(self):
        r = full_route(clean_full(core_niche_key="lifestyle"))
        self.assertEqual(r["pool"], Pool.REVIEW)
        self.assertIn("lifestyle_cap", r["review_reasons"])

    def test_score_tier_boundary_749_not_include(self):
        # normalized 74.9 用 normalized_total 判层 → Priority Review，不因 round 成 7.5 进 Include
        r = full_route(clean_full(), score_norm=74.9)
        self.assertEqual(r["pool"], Pool.PRIORITY_REVIEW)

    def test_score_tier_750_include(self):
        r = full_route(clean_full(), score_norm=75.0)
        self.assertIn(r["pool"], (Pool.INCLUDE_WITH_STOREFRONT, Pool.INCLUDE_WITHOUT_STOREFRONT))

    def test_mutex_single_pool(self):
        # 每个候选只返回一个 pool（结构保证），抽样几个变体确认无异常
        for kw in ({}, {"fake_pct": 25.0}, {"paid_cpm": None}, {"core_niche_key": "lifestyle"},
                   {"campaign_track": "gifting", "follower_count": 40000}):
            r = full_route(clean_full(**kw))
            self.assertIsInstance(r["pool"], Pool)


if __name__ == "__main__":
    unittest.main()
