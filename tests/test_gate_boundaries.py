"""硬门槛边界用例（P0-11 / QA-01）。所有恰值语义按 config 半开区间。"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2 import gates  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.contracts import GateVerdict  # noqa: E402

CFG = load_config()


def verdict(cand):
    return gates.gate_summary(gates.evaluate_gates(cand, CFG))


def base(**kw):
    # 默认让所有无关门槛通过，单个用例只覆盖自己要测的字段。
    # GATE-13 当前以 real_er_median 为准；旧数据缺中位数时才回退 Modash general_er。
    c = {
        "platform": "instagram",
        "campaign_track": "paid",
        "storefront_status": "confirmed_yes",
        "real_er_median": 2.0,
        "fake_pct": 10.0,
        "creator_country": "US",
        "top_audience_country": "US",
        "general_er": 3.0,
        "sponsorship_saturation": 20.0,
        "brand_account_type": "personal",
        "shein_temu_partnership": False,
    }
    c.update(kw)
    return c


class TestFollowers(unittest.TestCase):
    def test_paid_boundaries(self):
        self.assertEqual(verdict(base(follower_count=9999)), GateVerdict.EXCLUDE)
        self.assertEqual(verdict(base(follower_count=10000)), GateVerdict.PASS)
        self.assertEqual(verdict(base(follower_count=150000)), GateVerdict.PASS)
        self.assertEqual(verdict(base(follower_count=150001)), GateVerdict.EXCLUDE)

    def test_gifting_boundaries(self):
        g = lambda f: verdict(base(campaign_track="gifting", follower_count=f))
        self.assertEqual(g(4999), GateVerdict.EXCLUDE)     # 下界外
        self.assertEqual(g(5000), GateVerdict.PASS)        # 标准池含下界
        self.assertEqual(g(29999), GateVerdict.PASS)       # 标准池上界内
        self.assertEqual(g(30000), GateVerdict.REVIEW)     # 优秀池 → Priority Review
        self.assertEqual(g(50000), GateVerdict.REVIEW)     # 优秀池含上界
        self.assertEqual(g(50001), GateVerdict.EXCLUDE)    # 超出不进 Gifting


class TestModashGates(unittest.TestCase):
    def test_fake(self):
        self.assertEqual(verdict(base(follower_count=50000, fake_pct=24.99)), GateVerdict.PASS)
        self.assertEqual(verdict(base(follower_count=50000, fake_pct=25.0)), GateVerdict.EXCLUDE)

    def test_general_er_reference_only(self):
        # 客户 2026-07-15 放宽：Modash General ER 降为参考、不再硬淘汰（general_er_reference_only）。
        # 实算 ER(GATE-13) 才是硬门槛。低 Modash ER 不再 EXCLUDE。
        self.assertEqual(verdict(base(follower_count=50000, general_er=1.0)), GateVerdict.PASS)
        self.assertEqual(verdict(base(follower_count=50000, general_er=2.0)), GateVerdict.PASS)

    def test_real_er_hard_gate(self):
        # 2026-07-16 放宽：实算中位 ER <1% 均 Review 浮现，不再硬淘汰；>=1% PASS。
        self.assertEqual(
            verdict(base(follower_count=50000, real_er_median=0.3)),
            GateVerdict.REVIEW,
        )
        self.assertEqual(
            verdict(base(follower_count=50000, real_er_median=0.7)),
            GateVerdict.REVIEW,
        )
        self.assertEqual(
            verdict(base(follower_count=50000, real_er_median=1.5)),
            GateVerdict.PASS,
        )
        self.assertEqual(
            verdict(
                base(
                    follower_count=50000,
                    real_er_median=None,
                    sampled_posts=[],
                    general_er=None,
                )
            ),
            GateVerdict.REVIEW,
        )


class TestSponsorship(unittest.TestCase):
    def s(self, v):
        return verdict(base(follower_count=50000, sponsorship_saturation=v))

    def test_bands(self):
        self.assertEqual(self.s(29.99), GateVerdict.PASS)
        self.assertEqual(self.s(30.0), GateVerdict.REVIEW)   # 含下界
        self.assertEqual(self.s(40.0), GateVerdict.REVIEW)   # 含上界
        self.assertEqual(self.s(40.01), GateVerdict.EXCLUDE)


class TestCountry(unittest.TestCase):
    def test_target_and_match(self):
        self.assertEqual(verdict(base(follower_count=50000, creator_country="US",
                                       top_audience_country="US")), GateVerdict.PASS)
        self.assertEqual(verdict(base(follower_count=50000, creator_country="US",
                                       top_audience_country="BR")), GateVerdict.EXCLUDE)
        self.assertEqual(verdict(base(follower_count=50000, creator_country="BR",
                                       top_audience_country="BR")), GateVerdict.EXCLUDE)
        # tier2 可接受
        self.assertEqual(verdict(base(follower_count=50000, creator_country="NL",
                                       top_audience_country="NL")), GateVerdict.PASS)

    def test_non_target_creator_excluded_even_when_audience_missing(self):
        self.assertEqual(verdict(base(follower_count=50000, creator_country="Russia",
                                       top_audience_country=None)), GateVerdict.EXCLUDE)
        self.assertEqual(verdict(base(follower_count=50000, creator_country="South Korea",
                                       top_audience_country=None)), GateVerdict.EXCLUDE)

    def test_non_target_top_audience_excluded_as_mismatch(self):
        self.assertEqual(verdict(base(follower_count=50000, creator_country="DE",
                                       top_audience_country="Ukraine")), GateVerdict.EXCLUDE)
        self.assertEqual(verdict(base(follower_count=50000, creator_country="UK",
                                       top_audience_country="Iran")), GateVerdict.EXCLUDE)

    def test_full_target_names_are_normalized(self):
        self.assertEqual(verdict(base(follower_count=50000, creator_country="United Kingdom",
                                       top_audience_country="GB")), GateVerdict.PASS)

    def test_unknown_country_is_review_not_guessed(self):
        self.assertEqual(verdict(base(follower_count=50000, creator_country="Atlantis",
                                       top_audience_country="US")), GateVerdict.REVIEW)


class TestStorefrontAndCollect(unittest.TestCase):
    def test_storefront_dual_track(self):
        for s in ("confirmed_yes", "confirmed_no"):
            self.assertEqual(verdict(base(follower_count=50000, storefront_status=s)), GateVerdict.PASS)
        self.assertEqual(verdict(base(follower_count=50000, storefront_status="unknown")),
                         GateVerdict.REVIEW)

    def test_private_excluded(self):
        self.assertEqual(verdict(base(follower_count=50000, is_private=True)), GateVerdict.EXCLUDE)

    def test_collect_fail_review(self):
        self.assertEqual(verdict(base(follower_count=50000, collect_failed=True)), GateVerdict.REVIEW)


class TestSheinTemuAndBrand(unittest.TestCase):
    def test_shein_temu(self):
        self.assertEqual(verdict(base(follower_count=50000, shein_temu_partnership=True)),
                         GateVerdict.EXCLUDE)
        # 普通提及（非合作）不淘汰
        self.assertEqual(verdict(base(follower_count=50000, shein_temu_partnership=False)),
                         GateVerdict.PASS)

    def test_brand_account(self):
        self.assertEqual(verdict(base(follower_count=50000, brand_account_type="brand")),
                         GateVerdict.EXCLUDE)
        self.assertEqual(verdict(base(follower_count=50000, brand_account_type="medical")),
                         GateVerdict.REVIEW)


if __name__ == "__main__":
    unittest.main()
