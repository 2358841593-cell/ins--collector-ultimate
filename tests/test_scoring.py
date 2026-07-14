"""评分测试（P0-11 / QA-02）：N/A 归一化、AI Score、9.5 封顶。"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

from extensions.sop_v2 import scoring  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from _fixtures import clean_full  # noqa: E402

CFG = load_config()


class TestNormalization(unittest.TestCase):
    def test_na_removed_from_denominator(self):
        items = scoring.score_all(clean_full(), CFG)
        # C5 save_share（avg_reels_shares None）与 E4 age_gender 应为 N/A
        na = [i for i in items if i.available is None]
        na_names = {(i.module, i.item) for i in na}
        self.assertIn(("C", "save_share"), na_names)
        self.assertIn(("E", "age_gender"), na_names)
        summ = scoring.normalize(items)
        # 满分向候选应接近满分且 <= 100
        self.assertGreater(summ["normalized_total"], 90)
        self.assertLessEqual(summ["normalized_total"], 100)

    def test_gifting_cpm_na(self):
        items = scoring.score_all(clean_full(campaign_track="gifting", paid_cpm=None), CFG)
        cpm = next(i for i in items if i.module == "F" and i.item == "cpm")
        self.assertIsNone(cpm.available)   # F1 记 N/A，不进分母

    def test_ai_score_is_one_decimal(self):
        summ = scoring.normalize(scoring.score_all(clean_full(), CFG))
        self.assertEqual(summ["ai_vetting_score"], round(summ["normalized_total"] / 10, 1))


class TestExceptionalCap(unittest.TestCase):
    def test_full_conditions_not_capped(self):
        cand = clean_full(storefront_active=True)
        self.assertEqual(scoring.apply_exceptional_cap(9.9, cand, CFG), 9.9)

    def test_missing_vo_capped_to_94(self):
        cand = clean_full(has_vo=False)   # VO 缺 → 六条件不满足
        self.assertEqual(scoring.apply_exceptional_cap(9.9, cand, CFG), 9.4)

    def test_below_floor_untouched(self):
        cand = clean_full(has_vo=False)
        self.assertEqual(scoring.apply_exceptional_cap(8.3, cand, CFG), 8.3)


if __name__ == "__main__":
    unittest.main()
