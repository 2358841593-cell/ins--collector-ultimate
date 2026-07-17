"""② 浅扫合格：seed → qualified | rejected。纯浏览器零 API（1 次 profile 页渲染）。

便宜硬门槛（PIPELINE_SPEC 坑C：号问题绝不 reject，只 error 重试）：
  私密 / 品牌号 / 粉丝越界(<2k 或 >300k 宽粗筛) / 无 Amazon 橱窗 / 非护肤美妆赛道 → reject。
  精确粉丝分档（paid 10-150k / gifting 双池）留 run_v2 gate_followers。

用法：cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.stage2_qualify \
        --batch-id SKINCARE-20260716 [--limit 40] [--resume]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.pipeline._base import run_browser_stage  # noqa: E402


def qualify_one(pg, cand, cfg):
    import browser_collect_v2 as bc
    p = cfg.get("pipeline", {})
    h = cand["handle"]
    pf = bc.fetch_profile_browser(pg, h)
    if pf is None:
        return ("error", "profile_fetch_failed")      # 导航失败 = 号/网络问题，可重试
    if pf.get("_wall"):
        return ("error", "login_wall")                # 登录墙/风控 = 号问题，换号重试
    codes = pf.pop("codes", [])
    cand.update(pf)
    cand["codes"] = codes                             # 深采复用，省网格重取
    # ── 便宜硬门槛（仅候选不合格才 reject）──
    # 均带 cand 回写：淘汰号也留住已抓浅扫数据（粉丝/bio/橱窗/赛道），进 Exclude 池可展示（客户铁律）
    if cand.get("is_private"):
        return ("reject", "private", cand)
    if cand.get("brand_account_type") == "brand":
        return ("reject", "brand_account", cand)
    f = cand.get("follower_count")
    lo, hi = p.get("coarse_min_followers", 2000), p.get("coarse_max_followers", 300000)
    if f is not None and (f < lo or f > hi):           # 与 track 无关的宽粗筛
        return ("reject", "followers_out_of_range", cand)
    bc._resolve_storefront(cand, pg)
    if cand.get("storefront_status") == "confirmed_no":
        return ("reject", "no_amazon_storefront", cand)
    niche = cand.get("core_niche_key")
    if niche not in p.get("core_niches", ["skincare", "beauty_device", "beauty_wellness"]):
        bio = (cand.get("biography") or "").lower()
        nk = cfg.get("discovery", {}).get("niche_keywords", [])
        if not any(k in bio for k in nk):
            return ("reject", "off_niche", cand)
    return ("advance", "qualified", cand)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    p = cfg.get("pipeline", {})
    run_browser_stage("seed", args.batch_id, args.limit, args.resume,
                      lambda pg, cand: qualify_one(pg, cand, cfg),
                      per_account=p.get("qualify_per_account", 8),
                      stale_minutes=p.get("resume_stale_minutes", 30))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
