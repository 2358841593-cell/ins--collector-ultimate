#!/usr/bin/env python3
"""测试账号能否使用发现/扩展端点（search_users / chaining / fbsearch / media_likers）。

像之前测 hashtag 一样——确认这些私有端点对我们的账号是否可用（不是 login_required）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from discover import AccountPool, load_pool_accounts, as_plain  # noqa: E402


def show(label, fn):
    print(f"\n>>> {label}")
    try:
        r = fn()
        items = list(r or [])
        print(f"    ✓ 返回 {len(items)} 项")
        for it in items[:5]:
            d = as_plain(it)
            if isinstance(d, dict):
                print(f"      - @{d.get('username')} ({d.get('full_name','')[:30]}) pk={d.get('pk')}")
            else:
                print(f"      - {str(it)[:60]}")
        return items
    except Exception as exc:
        print(f"    ✗ {type(exc).__name__}: {str(exc)[:130]}")
        return None


def main():
    pool = AccountPool(load_pool_accounts(), rotate_every=20, cooldown_minutes=30)
    pool.warm_up()

    # 1. 关键词用户搜索（最重要——种子无关，相当于私有版 bio 搜索）
    show("search_users('skincare')", lambda: pool.search_users("skincare", 20))
    time.sleep(8)
    show("search_users('led mask')", lambda: pool.search_users("led mask", 20))
    time.sleep(8)
    show("search_users('amazon finds skincare')", lambda: pool.search_users("amazon finds skincare", 20))
    time.sleep(8)

    # 2. 取一个护肤创作者 pk 做 chaining / fbsearch（lookalike 扩展）
    seed = "drculver"
    try:
        u = pool.user_info_by_username_v1(seed)
        seed_pk = str(as_plain(u).get("pk"))
        print(f"\n种子 @{seed} pk={seed_pk}")
        time.sleep(6)
        show(f"chaining({seed} 相似账号)", lambda: pool.chaining(seed_pk))
        time.sleep(8)
        show(f"fbsearch_suggested_profiles({seed})", lambda: pool.fbsearch_suggested_profiles(seed_pk))
    except Exception as exc:
        print(f"\n种子解析失败: {exc}")

    # 3. 品牌帖点赞者（挖掘品牌互动用户）
    time.sleep(8)
    try:
        bu = pool.user_info_by_username_v1("currentbody")
        bpk = str(as_plain(bu).get("pk"))
        medias = pool.user_medias_v1(bpk, amount=1)
        if medias:
            mid = str(as_plain(medias[0]).get("pk"))
            print(f"\n品牌帖 media_pk={mid}")
            time.sleep(6)
            show(f"media_likers({mid})", lambda: pool.media_likers(mid))
    except Exception as exc:
        print(f"\nmedia_likers 测试失败: {exc}")

    print(f"\n账号池: {pool.stats() if hasattr(pool,'stats') else ''}")


if __name__ == "__main__":
    main()
