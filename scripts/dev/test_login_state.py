"""逐个检测账号 cookie 注入后是否真登录（访问 IG 根域看登录态/风控挑战）。"""
import sys, time
from playwright.sync_api import sync_playwright
from browser_collect_v2 import load_proxy, load_accounts, open_ctx

acct_file = sys.argv[1] if len(sys.argv) > 1 else None
proxy = load_proxy()
accts = load_accounts(acct_file)
print(f"测 {len(accts)} 个号 (文件: {acct_file or 'accounts_raw.txt'})")
with sync_playwright() as pw:
    for a in accts:
        try:
            ctx = open_ctx(pw, a, proxy, headless=True)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=40000)
            pg.wait_for_timeout(5000)
            st = pg.evaluate(r"""()=>{
              const b=document.body.innerText.slice(0,300);
              const challenge=/verify you'?re a real person|请验证你是真人|confirm you'?re human|suspicious|unusual/i.test(b);
              const loginForm=!!document.querySelector('input[name="username"],input[name="password"]');
              const loggedIn=!!document.querySelector('svg[aria-label*="Home" i],svg[aria-label*="New post" i],a[href="/explore/"]');
              return {challenge, loginForm, loggedIn, head:b.replace(/\n/g,'|').slice(0,80)};
            }""")
            status = ("✅ 已登录" if st["loggedIn"] and not st["loginForm"] else
                      "🟡 风控挑战" if st["challenge"] else
                      "🔴 已登出" if st["loginForm"] else "❓ 未知")
            print(f"{a['username']:26} {status}  | {st['head'][:60]}")
            ctx.close()
        except Exception as e:
            print(f"{a['username']:26} ERR {str(e).splitlines()[0][:50]}")
        time.sleep(3)
