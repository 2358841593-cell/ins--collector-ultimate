"""验证登出态浏览器浅扫（英文 locale）：粉丝/橱窗/品牌/赛道/帖子网格是否稳。"""
import time
from playwright.sync_api import sync_playwright
from browser_collect_v2 import load_proxy, load_accounts, open_ctx, fetch_profile_browser, _resolve_storefront

proxy = load_proxy()
acct = load_accounts()[0]   # 死号→登出态，正好测登出浅扫
tests = ["montanasalgado_", "bubeecosmetics_", "dra.aliciapaola", "la.mayor"]
with sync_playwright() as pw:
    ctx = open_ctx(pw, acct, proxy, headless=True)
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    for h in tests:
        t = time.time()
        try:
            pf = fetch_profile_browser(pg, h)
            if not pf:
                print(f"\n{h}: None (goto failed)"); continue
            if pf.get("_wall"):
                print(f"\n{h}: WALL (登录墙/挑战,无公开数据)"); continue
            _resolve_storefront(pf, pg)
            print(f"\n=== {h}  ({time.time()-t:.1f}s) ===")
            print(f"  followers : {pf.get('follower_count')}   name: {pf.get('full_name')}")
            print(f"  external  : {pf.get('external_url')}")
            print(f"  storefront: {pf.get('storefront_status')}  brand_type: {pf.get('brand_account_type')}  niche: {pf.get('core_niche_key')}")
            print(f"  codes     : {len(pf.get('codes') or [])}   bio: {(pf.get('biography') or '')[:80].strip()}")
        except Exception as e:
            print(f"\n{h}: ERR {type(e).__name__}: {str(e).splitlines()[0][:60]}")
        time.sleep(5)
    ctx.close()
