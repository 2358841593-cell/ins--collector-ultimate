#!/usr/bin/env python3
"""浏览器采集 V2：代理 + 老号轮换 + 拟人节奏 + 截图嵌流程（应对 IG 反爬）。

实测结论固化：
- IG 对直连 IP 激进限流；**必须走代理换 IP**（.secrets/proxy.txt）。
- 老号（sessionid+ds_user_id）session 有效；新号那批失效。
- 帖子/评论 DOM 高度混淆 → **评论走截图 + 视觉读**（也正是客户要的证据）。

流程（每个候选，截图当场落盘到证据库，不回头补）：
  1. profile 页 → 截图 + 提取 og 全名/粉丝/bio/外链
  2. 帖子网格 → 取 shortcode
  3. 采样帖子 → 开帖页 → **截评论区**（证据）+ 取 caption/赞
  4. Storefront 穿透 → 截图（用于门槛，不作评论证据）
  5. evidence.json 即时记录每张截图

产出：候选 JSON（含派生内容信号 + 证据索引）；评论购买意图由后续视觉分析读截图填。
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".secrets"
EVIDENCE_ROOT = ROOT / "data" / "evidence"
sys.path.insert(0, str(ROOT / "scripts"))
from extensions.sop_v2 import content as content_mod  # noqa: E402
from extensions.sop_v2 import storefront as storefront_mod  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402

_CFG = load_config()
AGG = ("linktr.ee", "linktree.com", "beacons", "ltk", "liketoknow", "shopltk", "shopmy",
       "komi.io", "stan.store", "vana.ly", "bio.site", "linkin.bio", "lnk.bio", "snipfeed",
       "flow.page", "msha.ke", "tapl.ink", "milkshake", "campsite.bio", "withkoji",
       "desty.page", "desty.link", "carrd.co", "znap.link", "hoo.be", "direct.me", "url.bio",
       "pillar.io", "many.link", "tap.bio", "solo.to", "allmylinks", "linkpop",
       "wonderl.ink", "wonderlink", "linkme.bio", "link.me", "later.com", "lnk.to",
       "linkr.bio", "s.shopmy", "flowcode", "koji", "gravatar")   # 补欧洲/新型聚合链
def load_proxy(session: str | None = None, ttl: int = 900):
    """读代理。session 给定则用青果 sticky 参数 `-S-{通道}-T-{秒}` 把出口 IP 固定住。

    ⚠ 根因（2026-07-17 实测）：默认隧道是"每请求随机换 IP"——浏览器一个页面并行 40+ 连接，
    每条连接一个不同出口 IP，导致**同一个登录 session 的流量散在几十个 IP 上打 IG**，被判成
    盗号级异常 → 挑战/限流（"越跑越挂"的真相）。绑定 session 名 → 同通道同 IP，一账号一 IP、
    一条会话跑到底，像真人。实测 overseas.tunnel.qg.net 支持 -S-/-T-（同名 sticky、异名换 IP）。"""
    f = SECRETS / "proxy.txt"
    if not f.exists():
        return None
    url = f.read_text().strip()
    m = re.match(r"https?://([^:]+):([^@]+)@(.+)", url)
    if not m:
        return {"server": url}
    user = m.group(1)
    if session:
        safe = re.sub(r"[^a-zA-Z0-9]", "", session)[:16] or "s"   # 通道名只保守用字母数字
        user = f"{user}-S-{safe}-T-{int(ttl)}"
    return {"server": f"http://{m.group(3)}", "username": user, "password": m.group(2)}


def load_accounts(path=None):
    """老号：username | pw | totp | cookie(含 sessionid+ds_user_id) | ...。
    path 默认 .secrets/accounts_raw.txt；可传备份文件路径。"""
    src = Path(path) if path else (SECRETS / "accounts_raw.txt")
    out = []
    for raw in src.read_text().splitlines():
        if not raw.strip():
            continue
        p = raw.strip().split("|")
        cb = p[3] if len(p) > 3 else ""
        sid = dsid = ""
        for kv in cb.split(";"):
            kv = kv.strip()
            if kv.startswith("sessionid="):
                sid = unquote(kv[len("sessionid="):])
            elif kv.startswith("ds_user_id="):
                dsid = kv[len("ds_user_id="):]
        if sid and dsid:
            out.append({"username": p[0], "sessionid": sid, "ds_user_id": dsid})
    return out


BRAND_CATEGORIES = ("cosmetic", "skin care service", "product", "shopping", "retail",
                    "brand", "store", "company", "e-commerce", "wholesale")
# 判**品牌号淘汰**只用零歧义的零售类目——不含 "skin care service"/"cosmetic"/"product"
# （皮肤科医生/个人护肤号的类目正是这些，会被误杀，dra.aliciapaola 教训）。
STORE_CATEGORIES = ("shopping & retail", "shopping and retail", "e-commerce", "wholesale",
                    "cosmetics store", "beauty store", "shopping mall", "clothing store",
                    "retail company", "grocery store")
# 店铺/品牌**无歧义**信号（店铺型名词）。刻意不含 cosmetics/cosmetica/farmacia/official 等
# 歧义词——个人护肤博主 bio 常提这些，会误判成品牌号（dra.aliciapaola 曾被误杀）。
BRAND_NAME_KW = ("store", "tienda", "boutique", "wholesale", "supply", "distribuidor",
                 "e-commerce", "we sell", "dm to order", "official store", "tienda oficial",
                 "online shop", "our shop")


def _parse_count(s):
    """'1.2M' / '12.3K' / '12,345' / '12 mil' → int。"""
    if not s:
        return None
    s = s.strip().replace(",", "").replace(" ", "").lower()
    m = re.match(r"([\d.]+)\s*([kmb万mil]*)", s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    if "m" in unit or "b" in unit:
        n *= 1_000_000 if "m" in unit and "mil" not in unit else 1_000
    elif "k" in unit or "mil" in unit:
        n *= 1_000
    elif "万" in unit:
        n *= 10_000
    return int(n)


def _post_stats(og_desc):
    """从帖子 og:description 解析 (likes, comments, 干净 caption)。
    格式如 '1,234 likes, 56 comments - Name (@user) on Instagram: caption'。"""
    if not og_desc:
        return None, None, ""
    likes = comments = None
    ml = re.search(r"([\d.,]+\s*[KMkm]?)\s+likes?", og_desc, re.I)
    mc = re.search(r"([\d.,]+\s*[KMkm]?)\s+comments?", og_desc, re.I)
    if ml:
        likes = _parse_count(ml.group(1))
    if mc:
        comments = _parse_count(mc.group(1))
    # 干净 caption：取 'Instagram: ' 之后；无则用整段
    m = re.search(r"on Instagram:\s*", og_desc)
    caption = og_desc[m.end():] if m else og_desc
    return likes, comments, caption.strip()


_PROFILE_JS = r"""() => {
  const meta=(p)=>{const e=document.querySelector(`meta[property="${p}"]`);return e?e.content:null;};
  const body=(document.body.innerText||'');
  // bio 外链：头部锚点里指向 l.instagram.com/?u= 或非 instagram/Meta 域名的（排除页脚 Meta 链接）
  let ext=null;
  const EXCL=/instagram\.com|threads\.(net|com)|facebook\.com|meta\.com|meta\.ai|fb\.me|foa_web|help\.|privacy|about\.|\/legal\//i;
  for(const a of document.querySelectorAll('header a, section a')){
    const h=a.getAttribute('href')||'';
    if(/l\.instagram\.com\/\?u=/.test(h)){ try{ext=decodeURIComponent(h.split('u=')[1].split('&')[0]);}catch(e){ext=h;} break; }
    if(/^https?:\/\//.test(h) && !EXCL.test(h)){ ext=h; break; }
  }
  // 商业按钮信号：只认专业号**独有**按钮（Email/Contact/Call/Book/View shop）——
  // 不含 Message/Follow（登录态看任何号都有，会让 is_business 恒真）
  const biz=/\b(Email|Correo electr|Contact options|Contactar|Call|Llamar|Book now|Reservar|View shop|Ver tienda)\b/i.test(body.slice(0,1500));
  // bio 链接：现代 IG 渲染成叶子 <div>（非 <a>），文字以域名开头，形如
  // "linktr.ee/xxx and 2 more" —— 完整首链就在文字里；"and N more" 表示还有隐藏链接（需点开弹层）。
  let biolink='';
  const domStart=/^[\s\p{Emoji}\p{So}👉➡🔗•·|]*((?:https?:\/\/)?(?:[a-z0-9-]+\.)+[a-z]{2,})(\/|\s|$)/iu;
  for(const el of document.querySelectorAll('header a, header div, section a, section div, main div')){
    if(el.children.length>0) continue;
    const t=(el.innerText||'').trim();
    if(t && t.length<200 && domStart.test(t) && !/instagram\.com|threads\.|^@/i.test(t)){ biolink=t; break; }
  }
  // 帖子网格 shortcode + 置顶标记。置顶只在 profile 网格可可靠识别，进入帖子页后通常不显示。
  const anchors=[...document.querySelectorAll('a[href]')]
     .filter(a=>/\/(p|reel)\//.test(a.getAttribute('href')||''));
  const isPinned=(a)=>{
    // 每个 a 自己就是一个完整网格卡片；向上爬到 row/grid 会把同排甚至全页都误判为置顶。
    const marker=[a.getAttribute('aria-label')||'',a.getAttribute('title')||''];
    a.querySelectorAll('[aria-label],[title],svg title').forEach(e=>{
      marker.push(e.getAttribute('aria-label')||e.getAttribute('title')||e.textContent||'');
    });
    // “没看见置顶图标”不是确认未置顶；false 只能由 media-info 的 pin lists 给出。
    return /\bPinned(?: post| reel)?\b|置顶|Fijado|Fixado|Épinglé/i.test(marker.join(' '))
      ? true : null;
  };
  const post_refs=anchors.map(a=>({url:a.getAttribute('href'), pinned:isPinned(a)}));
  const codes=post_refs.map(x=>x.url);
  const challenge=/verify you'?re a real person|请验证你是真人|confirm you'?re human|suspicious|unusual activity/i.test(body.slice(0,400));
  const loginForm=!!document.querySelector('input[name="username"],input[name="password"]');
  return {
    ogdesc: meta('og:description')||'', ogtitle: meta('og:title')||'',
    header: body.slice(0, 900), external_url: ext, bio_link_text: biolink, biz_buttons: biz,
    codes: [...new Set(codes)].slice(0,30),
    post_refs: post_refs.filter((x,i,a)=>a.findIndex(y=>y.url===x.url)===i).slice(0,30),
    challenge, login_form: loginForm,
    private: /This account is private|Esta cuenta es privada|cuenta privada|账号私密|This Account is Private/i.test(body)
  };
}"""


def _first_url_from_bio(text):
    """从 bio 链接 div 文字（'linktr.ee/x and 2 more'）取首个完整 URL。"""
    if not text:
        return None
    t = re.sub(r"\s+and\s+\d+\s+more\s*$", "", text.strip(), flags=re.I)
    m = re.search(r"((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?)", t, re.I)
    if not m:
        return None
    u = m.group(1).rstrip(".,)")
    return u if u.lower().startswith("http") else "https://" + u


def _bio_has_more(text):
    return bool(text and re.search(r"\band\s+\d+\s+more\b", text, re.I))


def _expand_bio_links(pg):
    """点开 bio 链接（'and N more'）→ 读弹层里全部链接 URL。返回列表（去 IG/threads）。
    实测可点击祖先是 <button>（链路 DIV→DIV→BUTTON）；JS .click() 不触发 IG 的 React 处理，
    故先给该 button 打标，再用 Playwright 真点击。"""
    try:
        tagged = pg.evaluate(r"""() => {
          const el=[...document.querySelectorAll('div,span')].find(e=>
            e.children.length===0 && /\band \d+ more\b/i.test(e.innerText||''));
          if(!el) return false;
          let t=el; for(let i=0;i<6&&t;i++){ if(t.tagName==='BUTTON'||t.getAttribute('role')==='button'){break;} t=t.parentElement; }
          (t||el).setAttribute('data-bioexpand','1'); return true;
        }""")
        if not tagged:
            return []
        try:
            pg.click('[data-bioexpand="1"]', timeout=4000)
        except Exception:  # noqa: BLE001
            return []
        pg.wait_for_timeout(2000)
        urls = pg.evaluate(r"""() => {
          const dlgs=[...document.querySelectorAll('div[role="dialog"]')];
          const dlg=dlgs[dlgs.length-1]; if(!dlg) return [];
          const out=new Set();
          dlg.querySelectorAll('a[href]').forEach(a=>{const h=a.getAttribute('href')||''; if(/^https?:/.test(h)) out.add(h);});
          dlg.querySelectorAll('div,span').forEach(e=>{ if(e.children.length===0){
            const m=(e.innerText||'').match(/(?:[a-z0-9-]+\.)+[a-z]{2,}\/[^\s]*/i); if(m) out.add('https://'+m[0]); }});
          return [...out];
        }""")
        try:
            pg.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return [u for u in (urls or []) if not re.search(r"instagram\.com|threads\.", u, re.I)]
    except Exception:  # noqa: BLE001
        return []


# bio 外链在现代 IG profile 页常不是 <a href>，而是 JS 点击的截断文字——但真实 URL 仍在
# 原始 HTML 里（实测：linktr.ee/X、vana.ly/X、l.instagram.com/?u=<编码>）。故从 pg.content() 抠。
_BIO_LINK_DOMS = [
    r"amazon\.[a-z.]+/shop/[\w./\-]+", r"amzn\.to/[\w\-]+", r"amazon\.[a-z.]+/[\w./\-]*shop[\w./\-]*",
    r"linktr\.ee/[\w.\-]+", r"beacons\.ai/[\w.\-]+", r"vana\.ly/[\w.\-]+", r"stan\.store/[\w.\-]+",
    r"shopmy\.us/[\w.\-]+", r"liketoknow\.it/[\w.\-]+", r"shopltk\.com/[\w./\-]+", r"linkin\.bio/[\w.\-]+",
    r"bio\.site/[\w.\-]+", r"komi\.io/[\w.\-]+", r"snipfeed\.co/[\w.\-]+", r"flow\.page/[\w.\-]+",
    r"linktree\.com/[\w.\-]+", r"lnk\.bio/[\w.\-]+", r"msha\.ke/[\w.\-]+", r"tapl\.ink/[\w.\-]+",
    r"desty\.page/[\w.\-]+", r"desty\.link/[\w.\-]+", r"carrd\.co/[\w.\-]+", r"znap\.link/[\w.\-]+",
    r"hoo\.be/[\w.\-]+", r"solo\.to/[\w.\-]+", r"allmylinks\.com/[\w.\-]+",
]


def _extract_bio_link(html):
    """从 profile 页原始 HTML 抠 bio 外链（锚点抓不到时的可靠兜底）。
    HTML 里 URL 常带 JSON 转义斜杠（linktr.ee\\/X），先归一化 \\/ → /。"""
    if not html:
        return None
    h = html.replace("\\/", "/")
    # 1) IG 的 l.instagram.com/?u=<编码> 包装（bio 外链规范形态）——解码取真 URL
    m = re.search(r"l\.instagram\.com/\?u=([^\"'&<>\s\\]+)", h)
    if m:
        try:
            return unquote(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    # 2) 已知 link-in-bio / 橱窗 / 联系域名裸串（精确路径，避开 amazon_media 之类误命中）
    for pat in _BIO_LINK_DOMS:
        m = re.search(pat, h, re.I)
        if m:
            return "https://" + m.group(0).rstrip("\"'\\")
    return None


def fetch_profile_browser(pg, handle):
    """浏览器渲染 profile 页一次拿全浅扫字段 + 品牌判定 + 帖子网格（绕开限流 API，登出态也能扫公开号）。
    返回 dict（含 codes）；纯登录墙/风控挑战 → {_wall:True}；导航失败 None。"""
    if not _goto(pg, f"https://www.instagram.com/{handle}/"):
        return None
    pg.wait_for_timeout(5000)
    d = pg.evaluate(_PROFILE_JS)
    for _ in range(3):  # 帖子网格懒加载
        if d.get("codes"):
            break
        pg.mouse.wheel(0, 1200)
        pg.wait_for_timeout(3000)
        d = pg.evaluate(_PROFILE_JS)
    # og 元标签服务端渲染，登出/挂登录 banner 也在；据此解析粉丝/名字/**干净 bio**
    og = d.get("ogdesc") or ""
    m = re.search(r"([\d.,]+\s*[KMkm]?)\s*(?:Followers|Seguidores|seguidores|Abonnés)", og, re.I)
    followers = _parse_count(m.group(1)) if m else None
    codes = d.get("codes") or []
    # 挑战页/纯登录墙且拿不到任何公开数据 → 判 wall（无法浅扫）
    if (d.get("challenge") or (d.get("login_form") and not followers and not codes)):
        return {"_wall": True}
    # 干净 bio：og:description 里 'on Instagram:' 之后（避开 body.slice 的导航噪声）
    mb = re.search(r"on Instagram:\s*", og)
    bio = (og[mb.end():].strip() if mb else "").strip('"').strip()
    if not bio:                                    # og 无 bio → 退回头部文本
        bio = d.get("header") or ""
    name = (d.get("ogtitle") or "").split("(@")[0].strip() or None
    # 外链三级取：① 锚点(l.instagram 包装) ② bio 链接 div 文字(现代 IG 主要形态) ③ 原始 HTML 兜底
    ext = d.get("external_url") or _first_url_from_bio(d.get("bio_link_text"))
    if not ext:
        try:
            ext = _extract_bio_link(pg.content())
        except Exception:  # noqa: BLE001
            ext = None
    blob = f"{name or ''} {bio}".lower()
    is_business = bool(d.get("biz_buttons")) or any(k in blob for k in BRAND_CATEGORIES)
    # 品牌号淘汰：只认**无歧义**店铺词 或 零售类目（不用宽 BRAND_CATEGORIES，避免误杀个人护肤号）
    is_brand = any(k in blob for k in BRAND_NAME_KW) or any(k in blob for k in STORE_CATEGORIES)
    pf = {
        "handle": handle, "follower_count": followers, "full_name": name,
        "biography": bio, "external_url": ext,
        "is_private": bool(d.get("private")), "is_business": is_business,
        "category": None, "brand_account_type": "brand" if is_brand else "personal",
        "_scan_source": "browser", "codes": codes, "post_refs": d.get("post_refs") or [],
        "_bio_has_more": _bio_has_more(d.get("bio_link_text")),   # 多链接号：Amazon 可能藏在 "and N more"
    }
    pf["core_niche_key"] = content_mod.derive_niche(bio, name)
    return pf


# 拦掉的资源类型：抽取全程零像素依赖（_PROFILE_JS/_DEEP_READ_JS/_COMMENTS_JS/_GRID_JS 只读
# meta 标签、innerText、a[href]），且深采已弃截图（_shot 是死代码、comment_shots 硬编码 []）。
# 图片/视频/字体纯烧带宽和请求数——正是把隧道出口 IP 打到限流的元凶（青果默认每秒仅 5 并发）。
# ⚠ 绝不能拦 stylesheet：_load_comments 靠 mouse.move(820,500) 这个 CSS 双栏布局算出来的硬编码
# 坐标滚右侧评论列，拦了 CSS 布局塌成单栏，x=820 处不再是评论列 → 评论直接抽不到。
# ⚠ 也不能拦 script：IG 评论区是 React 渲染的，拦了就没评论。
_BLOCK_TYPES = frozenset(("image", "media", "font"))


def _install_blockers(ctx):
    """挂 context 级路由拦无用资源。挂 ctx 不挂 page：_base 用的是 ctx.pages[0]（预建页），
    且 _resolve_storefront 会用同一页去聚合站穿透，context 级一并覆盖。"""
    def _h(route):
        try:
            if route.request.resource_type in _BLOCK_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:  # noqa: BLE001  页面已关等竞态
            pass
    ctx.route("**/*", _h)


def open_ctx(pw, acct, proxy, headless=True):
    pd = SECRETS / "chrome-instagram-profiles" / acct["username"]
    pd.mkdir(parents=True, exist_ok=True)
    # 强制英文 UI locale：登出态页面否则随代理 IP 出中文("粉丝"/"关注")，粉丝数/类目解析全失效
    kw = dict(user_data_dir=str(pd), channel="chrome", headless=headless,
              viewport={"width": 1000, "height": 1300}, locale="en-US",
              service_workers="block",          # SW 发起的请求会绕过 route，且 SW 自己也在重发
              extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
              # 渲染器层面直接禁图：请求根本不发出，比 route 更省（route 是发出后才 abort，
              # 每请求一次跨进程往返）。两者互补：blink 拦图、route 兜住视频/字体/漏网。
              args=["--no-first-run", "--no-default-browser-check", "--lang=en-US",
                    "--blink-settings=imagesEnabled=false"])
    if proxy:
        kw["proxy"] = proxy
    ctx = pw.chromium.launch_persistent_context(**kw)
    _install_blockers(ctx)                      # 必须在任何导航前挂上
    ctx.add_cookies([
        {"name": "sessionid", "value": acct["sessionid"], "domain": ".instagram.com", "path": "/", "secure": True},
        {"name": "ds_user_id", "value": acct["ds_user_id"], "domain": ".instagram.com", "path": "/", "secure": True},
    ])
    return ctx


def close_ctx(ctx):
    """关闭带 route 的持久 context；先解除路由，避免未决 continue_ 让 close 永久等待。"""
    if not ctx:
        return
    try:
        ctx.unroute_all(behavior="ignoreErrors")
    except TypeError:
        try:
            ctx.unroute_all()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    for page in list(ctx.pages):
        try:
            page.close(run_before_unload=False)
        except Exception:  # noqa: BLE001
            pass
    try:
        ctx.close()
    except Exception:  # noqa: BLE001
        pass


def _pause(a=4.0, b=8.0):
    time.sleep(random.uniform(a, b))


LAST_NAV_ERR = None   # 最近一次导航失败的真面目（超时/HTTP状态/网络错误）——供诊断，别再对着黑盒猜


def _goto(pg, url, tries=2):
    """代理抖动时重试导航。失败时把真实原因记进 LAST_NAV_ERR（区分 timeout / net 错 / 非200）。"""
    global LAST_NAV_ERR
    for t in range(tries):
        try:
            # 单次导航硬上限与 Stage worker 的 20s watchdog 接近；不要在失效代理上
            # 45s×2/页磨完整个 10 帖窗口。
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=25000)
            # goto 成功但状态非 2xx/3xx（如 429 限流 / 302 跳登录）也算问题，记下来
            st = resp.status if resp else None
            if st and st >= 400:
                LAST_NAV_ERR = f"HTTP{st}"
                time.sleep(random.uniform(2, 4)); continue
            LAST_NAV_ERR = None
            return True
        except Exception as e:  # noqa: BLE001
            raw = str(e)
            net_code = re.search(r"net::([A-Z0-9_]+)", raw)
            if net_code:
                # 只保留稳定且不含 URL/代理凭证的 Chromium 网络错误码。
                LAST_NAV_ERR = f"net::{net_code.group(1)}"
            elif "Timeout" in type(e).__name__:
                LAST_NAV_ERR = "timeout"
            else:
                LAST_NAV_ERR = f"{type(e).__name__}:{raw[:60]}"
            time.sleep(random.uniform(2, 4))
    return False


def _shot(pg, path):
    """安全截图：短超时 + 失败跳过（不卡在字体加载 30 秒）。"""
    try:
        pg.screenshot(path=str(path), timeout=8000, animations="disabled")
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_comments(pg, rounds=4):
    """加载更多评论：点"查看更多评论"(+) + 在右侧评论列滚动。桌面帖页评论在右侧列(x≈820)。"""
    for _ in range(rounds):
        try:
            pg.evaluate(r"""() => {
              // 点开"查看更多评论"的 + 按钮 / "View all N comments"
              const btns=[...document.querySelectorAll('button,[role="button"],span,a')];
              for(const b of btns){const t=(b.getAttribute('aria-label')||b.innerText||'').toLowerCase();
                if(/more comment|view all|más comentario|ver los|load more|查看.*评论|加载更多/.test(t)){b.click();break;}}
            }""")
        except Exception:  # noqa: BLE001
            pass
        try:
            pg.mouse.move(820, 500)
            pg.mouse.wheel(0, 1400)
        except Exception:  # noqa: BLE001
            pass
        pg.wait_for_timeout(1100)


def _scroll_snippet_into_view(pg, snippet):
    """把含购买意图原话的评论 DOM 滚到视口中央——保证截图真的拍到那条评论。"""
    try:
        key = (snippet or "")[:40]
        if not key:
            return False
        return bool(pg.evaluate(r"""(key) => {
          const w=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          let n; while(n=w.nextNode()){ if(n.textContent && n.textContent.includes(key)){
            (n.parentElement||n).scrollIntoView({block:'center'}); return true; } }
          return false; }""", key))
    except Exception:  # noqa: BLE001
        return False


def collect_candidate(pg, handle, ev_dir, n_posts=10):
    """采一个候选：纯浏览器浅扫(profile+品牌+帖子网格) + 推广帖购买意图截图 + Storefront + 实算ER。
    零 API/instaloader。n_posts 默认 10 以支撑"前 10 帖实算 ER"。"""
    from extensions.sop_v2 import creator_cache
    ev = []
    cand = {"handle": handle, "profile_url": f"https://www.instagram.com/{handle}/",
            "discovery_source": "Modash Discover / ai_search", "discovered_via": "modash_search",
            "captured_at": time.strftime("%Y-%m-%d %H:%M")}

    # 1) 先查缓存库（30 天新鲜命中 → 零 IG 请求）；否则浏览器渲染 profile 页一次拿全
    codes = []
    cached = creator_cache.get(handle, max_age_days=30)
    if cached:
        cand.update(cached)
    else:
        pf = fetch_profile_browser(pg, handle)     # 浏览器浅扫（纯浏览器，零 API）
        if pf is None:
            return None, "profile_fetch_failed"
        if pf.get("_wall"):
            return None, "login_wall"   # 纯登录墙/风控挑战：本号不可用，交上层轮换/上报
        codes = pf.pop("codes", [])
        cand.update(pf)

    if cand.get("is_private"):
        return cand, ev  # 私密号：交给 gate 判 Exclude，不深采
    # 品牌号早筛：直接标记，跳过昂贵的帖子/评论采集
    if cand.get("brand_account_type") == "brand":
        cand["_skipped"] = "brand_account"
        _resolve_storefront(cand, pg)   # 品牌号也可能有橱窗，供参考
        _cache_save(cand)
        return cand, ev

    # Storefront 只记录不早淘汰：V2 同时支持 With / Without Storefront，
    # Amazon、LTK、ShopMy、自营店和购物聚合入口均可作为有效橱窗。
    _resolve_storefront(cand, pg)

    # 2-3) 深采（帖子网格 + 推广帖购买意图 + 实算 ER）——抽出为 deep_collect，stage3 复用
    cand["codes"] = codes
    return deep_collect(pg, cand, ev_dir, n_posts)


# 深采窗口大小：og:description 里读赞/评 + 推广帖判定的帖子文本
_DEEP_READ_JS = r"""(shortcode) => {
  const meta=(p)=>{const e=document.querySelector(`meta[property="${p}"]`);return e?e.content:null;};
  const html=document.documentElement.innerHTML||'';
  const body=(document.body.innerText||'');
  let taken=meta('article:published_time');
  if(!taken){const m=html.match(/"taken_at"\s*:\s*(\d+)/i);if(m)taken=Number(m[1]);}
  return {caption: meta('og:description')||'', video: !!meta('og:video'),
          taken_at:taken, text: body.slice(0, 12000)}; }"""
_GRID_JS = r"""() => {
  const anchors=[...document.querySelectorAll('a[href]')]
    .filter(a=>/\/(p|reel)\//.test(a.getAttribute('href')||''));
  const isPinned=(a)=>{
    const marker=[a.getAttribute('aria-label')||'',a.getAttribute('title')||''];
    a.querySelectorAll('[aria-label],[title],svg title').forEach(e=>{
      marker.push(e.getAttribute('aria-label')||e.getAttribute('title')||e.textContent||'');
    });
    return /\bPinned(?: post| reel)?\b|置顶|Fijado|Fixado|Épinglé/i.test(marker.join(' '))
      ? true : null;
  };
  const refs=anchors.map(a=>({url:a.getAttribute('href'),pinned:isPinned(a)}))
    .filter((x,i,a)=>a.findIndex(y=>y.url===x.url)===i);
  return {codes:refs.map(x=>x.url).slice(0,30),post_refs:refs.slice(0,30),
    logged_out:/创建新账户|Create new account|Log into Instagram/.test(document.body.innerText.slice(0,120))};
}"""

# Reel 详情页当前不在可见 DOM / OG 元数据中展示播放量。登录 Cookie 下的 Web 同源详情接口
# 会返回 IG 自身播放量、FB 转发播放量及两个置顶列表。这里只返回报价所需的有界字段，
# 不落完整响应、不回显 Cookie。优先 ig_play_count，避免 FB 转发把 IG 合作预算虚高。
_IG_MEDIA_INFO_JS = r"""async (mediaId) => {
  try {
    const r=await fetch(`/api/v1/media/${mediaId}/info/`, {
      headers:{'x-ig-app-id':'936619743392459'}, credentials:'include'
    });
    if(!r.ok) return {http_status:r.status};
    const data=await r.json();
    const m=(data.items||[])[0];
    if(!m) return {http_status:r.status, empty:true};
    const clipsPins=Array.isArray(m.clips_tab_pinned_user_ids)
      ? m.clips_tab_pinned_user_ids : null;
    const timelinePins=Array.isArray(m.timeline_pinned_user_ids)
      ? m.timeline_pinned_user_ids : null;
    const pinnedKnown=clipsPins!==null || timelinePins!==null;
    return {
      http_status:r.status, code:m.code||null, taken_at:m.taken_at??null,
      ig_play_count:m.ig_play_count??null, total_play_count:m.play_count??null,
      fb_play_count:m.fb_play_count??null,
      like_count:m.like_count??null, comment_count:m.comment_count??null,
      pinned:pinnedKnown ? ((clipsPins||[]).length>0 || (timelinePins||[]).length>0) : null,
      pinned_source:pinnedKnown ? 'ig_media_info_pin_lists' : null,
      like_and_view_counts_disabled:m.like_and_view_counts_disabled??null
    };
  } catch(e) {
    return {fetch_error:(e&&e.name)||'fetch_error'};
  }
}"""

_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _shortcode_to_media_id(shortcode):
    """Instagram shortcode 的 base64-like 可逆整数编码（无需额外请求）。"""
    code = str(shortcode or "").strip().strip("/")
    if "/" in code:
        code = code.split("/")[-1]
    if not code or any(ch not in _SHORTCODE_ALPHABET for ch in code):
        return None
    value = 0
    for ch in code:
        value = value * 64 + _SHORTCODE_ALPHABET.index(ch)
    return value


def _normalize_media_metric(payload):
    """把同源 media info 的有界响应归一为报价样本字段。"""
    payload = payload or {}
    status = payload.get("http_status")
    if status != 200:
        detail = (
            f"api_http_{status}" if status is not None
            else f"api_{payload.get('fetch_error') or 'failed'}"
        )
        return {
            "play_count": None,
            "play_count_status": detail,
            "play_count_source": None,
            "pinned": None,
            "pinned_source": None,
            "taken_at": None,
        }

    def _count(value):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None

    ig_count = _count(payload.get("ig_play_count"))
    total_count = _count(payload.get("total_play_count"))
    fb_count = _count(payload.get("fb_play_count"))
    # 客户报价口径只接受 Instagram 原生播放。play_count 可能混入 Facebook
    # cross-post，故仅保留作审计，绝不能回退成报价样本。
    chosen = ig_count
    source = "ig_media_info.ig_play_count" if ig_count is not None else None
    raw = (
        f"ig={ig_count};total={total_count};fb={fb_count}"
        if any(value is not None for value in (ig_count, total_count, fb_count))
        else None
    )
    return {
        "play_count": chosen,
        "play_count_status": "observed" if chosen is not None else "ig_not_exposed",
        "play_count_source": source,
        "play_count_raw": raw,
        "ig_play_count": ig_count,
        "total_play_count": total_count,
        "fb_play_count": fb_count,
        "like_count": _count(payload.get("like_count")),
        "comment_count": _count(payload.get("comment_count")),
        "pinned": payload.get("pinned") if isinstance(payload.get("pinned"), bool) else None,
        "pinned_source": payload.get("pinned_source"),
        "taken_at": payload.get("taken_at"),
        "like_and_view_counts_disabled": payload.get("like_and_view_counts_disabled"),
    }


def _fetch_reel_metric(pg, href):
    shortcode = str(href or "").rstrip("/").split("/")[-1]
    media_id = _shortcode_to_media_id(shortcode)
    if media_id is None:
        return _normalize_media_metric({"fetch_error": "invalid_shortcode"})
    try:
        return _normalize_media_metric(pg.evaluate(_IG_MEDIA_INFO_JS, str(media_id)))
    except Exception as exc:  # noqa: BLE001
        return _normalize_media_metric({"fetch_error": type(exc).__name__})

# 直接抽评论 {用户名,原话} 配对（不 OCR、不截图）。2026-07 实测：IG 帖子页**无 <article>**，
# 评论正文在 span[dir=auto]，与用户名链接是**兄弟节点**（旧代码在父链找正文→抽 0，dra.aliciapaola/
# rusky1415 教训）。新法：遍历 dir=auto 文本块 → 就近祖先取用户名 → 滤 UI 噪声（时间戳/赞数/回复/
# 翻译/导航）→ 排除帖主 caption。owner 传帖主 handle。
_COMMENTS_JS = r"""(owner) => {
  const isUser=h=>/^\/[a-zA-Z0-9._]+\/$/.test(h||'') && !/\/(p|reel|reels|explore|stories|direct)\//.test(h||'');
  // UI 噪声：时间戳(20w/5d/3h) / 赞数回复 / 翻译 / 导航项 / follow 等——非评论正文
  const NOISE=/^(\d+\s*(w|d|h|m|s|y)|\d[\d,.]*\s*(likes?|replies|reply)|reply|responder|see translation|hide|view( all)?( replies| \d)|查看翻译|ver traducci|verified|已验证|edited|editado|now|me gusta|author|pinned|más|more|profile|home|search|explore|reels|messages|notifications|create|settings|switch appearance|log ?out|meta|threads|about|help|press|api|jobs|privacy|terms|following|follow|message)$/i;
  const SKIPUSER=/^(meta|about|blog|jobs|help|api|privacy|terms|locations|instagram|threads|contact|popular|uploads|directory|explore|reels|p|reel|accounts|emails)$/i;
  const out=[], seen=new Set();
  for(const s of document.querySelectorAll('span[dir="auto"], div[dir="auto"]')){
    let txt=(s.innerText||'').trim();
    if(txt.length<2 || txt.length>400 || NOISE.test(txt)) continue;
    // 就近祖先里的用户名链接（评论作者）
    let box=s, uname='';
    for(let i=0;i<7&&box;i++){
      const a=[...box.querySelectorAll('a[href]')].find(a=>isUser(a.getAttribute('href')));
      if(a){ uname=a.getAttribute('href').replace(/\//g,''); break; }
      box=box.parentElement;
    }
    if(!uname || SKIPUSER.test(uname)) continue;
    if(owner && uname===owner) continue;                  // 帖主 caption/自评不算评论
    if(txt.startsWith(uname+' ')) txt=txt.slice(uname.length).trim();
    if(txt===uname || txt.length<2 || NOISE.test(txt)) continue;
    const key=uname+'|'+txt.slice(0,24);
    if(!seen.has(key)){ seen.add(key); out.push({username:uname, text:txt.slice(0,240)}); }
    if(out.length>=100) break;
  }
  return out;
}"""


_COMMENT_UI_NOISE_RE = re.compile(
    r"^(?:"
    r"liked by .+ and others|"
    r"\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?) ago|"
    r"this reel has \d+ comments? from facebook\.?|"
    r"hide all replies"
    r")$",
    re.IGNORECASE,
)


def _clean_comment_pairs(pairs, owner=None):
    """二次清掉已在真实结果中观察到的 IG UI 文本，避免把 UI 当评论完成证据。"""
    owner_key = str(owner or "").strip().lstrip("@").casefold()
    out, seen = [], set()
    for pair in pairs or []:
        if not isinstance(pair, dict):
            continue
        username = str(pair.get("username") or "").strip().lstrip("@")
        text = str(pair.get("text") or "").strip()
        if (
            not username
            or not text
            or username.casefold() == owner_key
            or _COMMENT_UI_NOISE_RE.fullmatch(text)
        ):
            continue
        key = (username.casefold(), text.casefold())
        if key not in seen:
            seen.add(key)
            out.append({"username": username, "text": text[:240]})
    return out


def _comment_retry_needed(comment_count, paired_count):
    """评论首抽不足时是否再加载一次。

    正评论数或未知指标且首抽为 0，不能因为低于旧的 15 条阈值就直接失败；
    已明确 0 评论仍由 deep_collect 在调用本函数前短路，不做无意义加载。
    """
    if paired_count == 0:
        return comment_count != 0
    return (
        comment_count is not None
        and comment_count >= 15
        and paired_count < max(5, comment_count // 3)
    )


def _sample_comment_pairs(pg, owner, comment_count):
    """加载并抽取一帖评论；返回 ``(pairs, attempts)``。

    二次加载只提高真实评论出现的机会，不会把 ``comment_count>0`` 本身视为
    完成证据；两次仍为空时调用方继续标记 ``comment_failed``。
    """
    _load_comments(pg, rounds=4)
    paired = _clean_comment_pairs(
        pg.evaluate(_COMMENTS_JS, owner) or [], owner
    )
    attempts = 1
    if _comment_retry_needed(comment_count, len(paired)):
        _load_comments(pg, rounds=3)
        paired = _clean_comment_pairs(
            pg.evaluate(_COMMENTS_JS, owner) or [], owner
        )
        attempts += 1
    return paired, attempts


def _media_identity(value):
    """把相对/绝对、带 handle/不带 handle 的 IG 媒体 URL 归一为同一 identity。"""
    raw = str(value or "").strip()
    match = re.search(r"/(?:[^/?#]+/)?(reel|p)/([A-Za-z0-9_-]+)", raw)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _comment_media_identity(value):
    """评论重试按 shortcode 识别媒体；``/p/X`` 与 ``/reel/X`` 是同一帖子别名。"""
    raw = str(value or "").strip()
    match = re.search(
        r"/(?:[^/?#]+/)?(?:reel|p)/([A-Za-z0-9_-]+)",
        raw,
    )
    return match.group(1) if match else _media_identity(raw)


def _comment_failure_identities(entries):
    """把上一轮 ``comment_failed_posts`` 归一为媒体 identity 集合。"""
    if isinstance(entries, (str, dict)):
        entries = [entries]
    if not isinstance(entries, (list, tuple, set)):
        return set()
    identities = set()
    for entry in entries:
        url = entry.get("url") if isinstance(entry, dict) else entry
        identity = _comment_media_identity(url)
        if identity:
            identities.add(identity)
    return identities


def _merge_post_refs(*groups):
    out, seen = [], set()
    for group in groups:
        for item in group or []:
            if isinstance(item, str):
                item = {"url": item, "pinned": None}
            url = item.get("url") or item.get("code")
            identity = _media_identity(url)
            if not url or identity in seen:
                continue
            seen.add(identity)
            out.append({"url": url, "pinned": item.get("pinned")})
    return out


def _refs_from_pricing_samples(samples, limit=10):
    """把历史报价样本转换为最低优先级的深采 refs，保持样本顺序并按媒体去重。"""
    refs = []
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        url = sample.get("code") or sample.get("url")
        if not url:
            continue
        refs.append({"url": url, "pinned": sample.get("pinned")})
    return _merge_post_refs(refs)[:max(0, int(limit))]


def _collect_profile_grid_refs(pg, handle, target_posts=10, max_scrolls=3):
    """刷新主页主网格的当前帖子窗口（普通帖 + Reels，保持页面顺序）。

    这是评论/ER 的“最近 N 帖”来源，和报价专用 Reels 专页严格分开。导航、登出或
    多轮渲染后仍为空都视为刷新失败，由调用方决定是否回退历史 refs。
    """
    if not _goto(pg, f"https://www.instagram.com/{handle}/"):
        return [], f"profile_grid_nav_failed:{LAST_NAV_ERR or 'unknown'}"
    pg.wait_for_timeout(5000)
    refs = []
    wanted = max(1, int(target_posts))
    for i in range(max_scrolls + 1):
        grid = pg.evaluate(_GRID_JS)
        if grid.get("logged_out"):
            return [], "logged_out"
        refs = _merge_post_refs(refs, grid.get("post_refs"))
        if len(refs) >= wanted or i >= max_scrolls:
            break
        pg.mouse.wheel(0, 1200)
        pg.wait_for_timeout(2200)
    if not refs:
        return [], "profile_grid_empty"
    return refs[:wanted], None


def _collect_grid_refs(pg, handle, min_non_pinned_reels=10, max_scrolls=8):
    """从 Reels 专页收集候选引用；返回 (refs, error)。

    不能使用主页主网格：创作者可把 Reel 从主网格移除，但它仍保留在 Reels 专页。
    DOM 未见 pin marker 只算 unknown；最终 true/false 由 media-info pin lists 确认。
    """
    if not _goto(pg, f"https://www.instagram.com/{handle}/reels/"):
        return [], f"grid_nav_failed:{LAST_NAV_ERR or 'unknown'}"
    pg.wait_for_timeout(5000)
    refs = []
    for i in range(max_scrolls + 1):
        g = pg.evaluate(_GRID_JS)
        if g.get("logged_out"):
            return refs, "logged_out"
        refs = _merge_post_refs(refs, g.get("post_refs"))
        usable = [
            r for r in refs
            if "/reel/" in r["url"] and r.get("pinned") is not True
        ]
        if len(usable) >= min_non_pinned_reels or i >= max_scrolls:
            break
        pg.mouse.wheel(0, 1500)
        pg.wait_for_timeout(2200)
    return refs, None


def collect_pricing_evidence(pg, cand):
    """独立采集展示型报价证据；不改评分/路由，也不要求重跑评论深采。

    返回 ``(state, error)``。state 含刷新后的 refs/codes 与按 href 索引的媒体指标，
    供完整 Stage 3 复用；同时直接写入 cand 的 pricing_* 字段，供历史候选价格补采。
    """
    from extensions.sop_v2 import pricing as pricing_mod

    handle = cand.get("handle")
    pricing_window = int(_CFG.get("pricing_estimate", {}).get("reels_window", 10))
    # 始终重新打开 Reels 专页：①保证 media-info fetch 有 IG 同源上下文；②拿到真实
    # Reels 顺序（不是可能缺 Reel 的主页主网格）；③多拿 3 条缓冲，剔除置顶后继续补足。
    fresh_refs, grid_error = _collect_grid_refs(
        pg, handle, min_non_pinned_reels=pricing_window + 3
    )
    if not fresh_refs and grid_error:
        return None, grid_error
    refs = _merge_post_refs(fresh_refs)

    codes = [r["url"] for r in refs]
    _pause(2, 4)

    pricing_reels, pricing_metric_by_href, pricing_sample_by_href = [], {}, {}
    eligible_pricing = metric_fail_streak = 0
    for grid_rank, ref in enumerate(
        [r for r in refs if "/reel/" in r["url"]]
    ):
        href = ref["url"]
        purl = href if str(href).startswith("http") else f"https://www.instagram.com{href}"
        sample = {
            "code": href,
            "url": purl,
            "is_video": True,
            "is_reel": True,
            "pinned": ref.get("pinned"),
            "pinned_source": "ig_profile_grid" if ref.get("pinned") is not None else None,
            "grid_rank": grid_rank,
            "captured_at": _now(),
        }
        if ref.get("pinned") is True:
            sample.update({
                "play_count": None,
                "play_count_status": "skipped_pinned",
                "play_count_source": None,
                "taken_at": None,
            })
        else:
            metric = _fetch_reel_metric(pg, href)
            sample.update(metric)
            if str(metric.get("play_count_status") or "").startswith("api_"):
                metric_fail_streak += 1
            else:
                metric_fail_streak = 0
        pricing_reels.append(sample)
        pricing_metric_by_href[href] = sample
        pricing_sample_by_href[href] = sample
        if sample.get("pinned") is False and sample.get("play_count_status") == "observed":
            eligible_pricing += 1
        if eligible_pricing >= pricing_window or metric_fail_streak >= 3:
            break
        if ref.get("pinned") is not True:
            _pause(0.6, 1.2)

    cand["pricing_reel_samples"] = pricing_reels
    cand["pricing_captured_at"] = _now()
    cand["pricing_estimate"] = pricing_mod.derive_quote_estimate(cand, _CFG)
    return {
        "refs": refs,
        "codes": codes,
        "pricing_reels": pricing_reels,
        "metric_by_href": pricing_metric_by_href,
        "sample_by_href": pricing_sample_by_href,
        "grid_error": grid_error,
    }, None


def deep_collect(pg, cand, ev_dir, n_posts=10):
    """③深采：打开目标帖子、逐帖读取赞评并采集评论，再派生意图与实算 ER。

    ``comments_read`` 只保留为“深采函数执行过”的历史兼容字段；正式完整性由
    ``deep_collection_status`` 及逐帖状态判断。已确认 0 评论属于完整证据；页面报告有
    评论（或赞评指标整体未读到）却抽不到评论，首次必须换号重试。只有同一 URL
    连续两轮都不可见、且本轮明确仅报告 1–2 条时，才以透明的 unavailable 终态收敛。
    """
    from extensions.sop_v2 import comments as cmt_mod
    from extensions.sop_v2 import pricing as pricing_mod

    handle = cand.get("handle")
    ev = []
    ev_dir.mkdir(parents=True, exist_ok=True)
    previous_comment_retry_ids = _comment_failure_identities(
        cand.get("comment_failed_posts")
    )
    # 若整号因其他帖子仍 incomplete 而再次运行，已收敛的低量帖应保持终态；
    # 显式 requeue 会清掉该字段，届时才从首轮重新计数。
    previous_comment_retry_ids.update(
        _comment_failure_identities(cand.get("comment_unavailable_posts"))
    )
    previous_pricing = {
        key: copy.deepcopy(cand[key])
        for key in (
            "pricing_reel_samples",
            "pricing_captured_at",
            "pricing_estimate",
        )
        if key in cand
    }

    # 评论/ER 必须先刷新当前主页主网格；历史 stage_json 的 codes 可能已删除、失效或
    # 不再属于最近窗口。只有主页刷新失败时才回退旧 refs。报价专用 Reels 专页仍与
    # 此窗口分离，绝不能把“最近 N 帖”悄悄改成“最近 N 条 Reels”。
    historical_primary_refs = _merge_post_refs(
        cand.get("post_refs"),
        [{"url": code, "pinned": None} for code in (cand.get("codes") or [])],
    )
    fresh_primary_refs, primary_refresh_error = _collect_profile_grid_refs(
        pg, handle, target_posts=n_posts
    )
    if fresh_primary_refs:
        primary_refs = fresh_primary_refs
        cand["post_refs"] = [dict(ref) for ref in fresh_primary_refs]
        cand["codes"] = [ref["url"] for ref in fresh_primary_refs]
        cand.pop("primary_grid_refresh_error", None)
        cand["primary_refs_source"] = "current_profile_grid"
        cand.pop("primary_refs_fallback_reason", None)
    else:
        primary_refs = historical_primary_refs
        cand["primary_grid_refresh_error"] = primary_refresh_error
        if historical_primary_refs:
            cand["primary_refs_source"] = "historical_primary_refs"
            cand["primary_refs_fallback_reason"] = primary_refresh_error
    pricing_state, pricing_error = collect_pricing_evidence(pg, cand)
    if pricing_state is None:
        cand["pricing_reel_samples"] = []
        cand["pricing_captured_at"] = _now()
        cand["pricing_estimate"] = pricing_mod.derive_quote_estimate(cand, _CFG)
        cand["pricing_estimate"]["collection_error"] = pricing_error
        pricing_state = {
            "refs": [],
            "codes": [],
            "pricing_reels": [],
            "metric_by_href": {},
            "sample_by_href": {},
        }
    # 极少数候选没有主页 shortcode 时，才按层级使用 Reels 引用兜底核心深采：
    # 本次报价 refs 优先；若它也为空，再用进入本轮前已有的报价样本。后者只作为
    # 评论/ER 的访问窗口，previous_pricing 的完整证据对象不会被覆盖或改写。
    if primary_refs:
        refs = primary_refs
    elif pricing_state["refs"]:
        refs = pricing_state["refs"]
        cand["primary_refs_source"] = "current_pricing_refs"
        cand["primary_refs_fallback_reason"] = (
            f"{primary_refresh_error or 'profile_grid_empty'};"
            "historical_primary_refs_empty"
        )
    else:
        refs = _refs_from_pricing_samples(
            previous_pricing.get("pricing_reel_samples"), limit=n_posts
        )
        if refs:
            cand["primary_refs_source"] = "existing_pricing_samples"
            cand["primary_refs_fallback_reason"] = (
                f"{primary_refresh_error or 'profile_grid_empty'};"
                "historical_primary_refs_empty;current_pricing_refs_empty"
            )
            cand["post_refs"] = [dict(ref) for ref in refs]
            cand["codes"] = [ref["url"] for ref in refs]
        else:
            cand["primary_refs_source"] = "unavailable"
            cand["primary_refs_fallback_reason"] = (
                f"{primary_refresh_error or 'profile_grid_empty'};"
                "historical_primary_refs_empty;current_pricing_refs_empty;"
                "existing_pricing_samples_empty"
            )
    codes = [ref["url"] for ref in refs]

    posts_meta, intent_posts, all_intent, promo_count = [], [], [], 0
    pricing_reels = pricing_state["pricing_reels"]
    pricing_metric_by_href = pricing_state["metric_by_href"]
    pricing_sample_by_href = pricing_state["sample_by_href"]
    all_comments, seen_c = [], set()           # 累积评论样本（供 comments.analyze 算有效样本/信任分）
    nav_fails = 0                              # 连续帖子页导航失败数（限流探测）
    failed_post_urls = []
    metric_missing_urls = []
    comment_failed_urls = []
    comment_unavailable_posts = []
    comment_attempted_posts = 0
    comment_completed_posts = 0
    comments_cfg = _CFG.get("comments", {})
    per_post_max = max(1, int(comments_cfg.get("per_post_max", 10)))
    primary_hrefs = codes[:n_posts]
    # 额外报价 Reels 已由同源详情接口读取，无需逐条导航；页面深采仍只跑原来的前 N 帖窗口。
    visit_hrefs = list(dict.fromkeys(primary_hrefs))
    for i, href in enumerate(visit_hrefs):
        purl = href if str(href).startswith("http") else f"https://www.instagram.com{href}"
        if not _goto(pg, purl):
            # 实测：隧道出口 IP 在高频翻帖下被限流，表现为帖子页连续导航失败。
            # 旧行为是傻等着挨个超时(45s×2/帖)、10 帖全废还静默产出空 cand（18% 的号中招）。
            # ⚠ 不要 sleep 退避：HTTPS 走 CONNECT，出口 IP 在建连时就定死，Chrome 的 keep-alive
            # 隧道在 sleep 期间不会断——醒来还是同一个被限流的 IP，重试必然再失败（实测 3✗4✗）。
            # 正解：快速失败 → _base 收到 error 会 ctx.close() 并换号重开 context → 新 CONNECT
            # → **新出口 IP**。这才是真正的"换 IP 退避"。
            nav_fails += 1
            if nav_fails >= 3 and not posts_meta:
                # → stage3 判 error → _base 关 ctx 换号换 IP
                return None, f"proxy_throttled:{LAST_NAV_ERR or 'unknown'}"
            failed_post_urls.append(purl)
            continue
        nav_fails = 0
        pg.wait_for_timeout(3000)
        shortcode = href.rstrip("/").split("/")[-1]
        info = pg.evaluate(_DEEP_READ_JS, shortcode)
        likes, comments, caption = _post_stats(info.get("caption", ""))   # 赞/评/干净caption(算实算ER)
        ref = next((r for r in refs if r["url"] == href), {"pinned": None})
        metric = pricing_metric_by_href.get(href) or {}
        if likes is None or comments is None:
            # OG 元标签会因地区/页面版本缺赞评；同源 media-info 对普通帖与 Reels 都可用。
            # 只在缺字段时补一次，避免已拿到 DOM 指标时增加请求。
            engagement_metric = (
                metric
                if metric.get("like_count") is not None
                or metric.get("comment_count") is not None
                else _fetch_reel_metric(pg, href)
            )
            if likes is None:
                likes = engagement_metric.get("like_count")
            if comments is None:
                comments = engagement_metric.get("comment_count")
        pinned = metric.get("pinned")
        if not isinstance(pinned, bool):
            pinned = ref.get("pinned")
        post_meta = {
            "code": href,
            "url": purl,
            "caption": caption,
            # 历史 og:video 在真实数据上恒 false；/reel/ 路径才是稳定 Reel 标记。
            "is_video": "/reel/" in href or bool(info.get("video")),
            "is_reel": "/reel/" in href,
            "like_count": likes,
            "comment_count": comments,
            "play_count": metric.get("play_count"),
            "play_count_status": metric.get("play_count_status") or (
                "not_collected" if "/reel/" in href else "not_applicable"
            ),
            "play_count_source": metric.get("play_count_source"),
            "play_count_raw": metric.get("play_count_raw"),
            "ig_play_count": metric.get("ig_play_count"),
            "total_play_count": metric.get("total_play_count"),
            "fb_play_count": metric.get("fb_play_count"),
            "taken_at": metric.get("taken_at") or info.get("taken_at"),
            "pinned": pinned,
            "pinned_source": metric.get("pinned_source") or (
                "ig_profile_grid" if ref.get("pinned") is not None else None
            ),
            "captured_at": _now(),
        }
        if likes is None or comments is None:
            metric_missing_urls.append(purl)
            post_meta["metrics_status"] = "missing"
        else:
            post_meta["metrics_status"] = "observed"
        if href in primary_hrefs:
            posts_meta.append(post_meta)
        if href in pricing_sample_by_href:
            pricing_sample_by_href[href].update({
                "caption": caption,
                "like_count": likes,
                "comment_count": comments,
                "taken_at": post_meta.get("taken_at"),
            })
        is_promo = cmt_mod.is_promotional(caption)
        if is_promo:
            promo_count += 1
        # 完整交付必须逐帖核验评论，不能再把“非推广/低评论”直接视作已经读过。
        # 已明确为 0 条的帖子无需展开，但要留下“已核实无评论”状态；其余帖子都尝试采样。
        if comments == 0:
            post_meta["comment_sampling_status"] = "verified_zero"
            post_meta["comments_collected"] = 0
            comment_completed_posts += 1
            _pause(1.5, 3)
            continue
        comment_attempted_posts += 1
        # 直接抽 {用户名,原话} 配对（owner=帖主，排除其 caption；不 OCR、不截图）。
        # 正评论数/未知指标若首抽为 0，无论是否达到 15 条都再加载一次。
        paired, sampling_attempts = _sample_comment_pairs(
            pg, handle, comments
        )
        paired = paired[:per_post_max]
        post_meta["comment_sampling_attempts"] = sampling_attempts
        post_meta["comments_collected"] = len(paired)
        if paired:
            post_meta["comment_sampling_status"] = "collected"
            comment_completed_posts += 1
        else:
            repeated_low_unavailable = (
                comments in (1, 2)
                and _comment_media_identity(purl) in previous_comment_retry_ids
            )
            if repeated_low_unavailable:
                post_meta["comment_sampling_status"] = (
                    "unavailable_after_retry"
                )
                comment_completed_posts += 1
                comment_unavailable_posts.append(
                    {
                        "url": purl,
                        "reported_count": comments,
                        "reason": (
                            "reported_low_count_unavailable_after_retry"
                        ),
                    }
                )
            else:
                post_meta["comment_sampling_status"] = (
                    "failed_unknown_metrics"
                    if comments is None
                    else "failed_reported_comments"
                )
                comment_failed_urls.append(purl)
        for c in paired:
            k = (c.get("username", "") + "|" + (c.get("text") or "")[:24]).lower()
            if c.get("text") and k not in seen_c:
                seen_c.add(k)
                all_comments.append(c["text"])
        # 有购买意图的评论(三级) → 结构化证据（谁说了什么 + 级别 + 帖子链接），不截图
        hits = cmt_mod.find_intent_comments(paired)
        if hits:
            all_intent.extend(hits)
            intent_posts.append({"post_url": purl, "promotional": is_promo, "intent_comments": hits})
            ev.append({"type": "intent_comment", "source_url": purl, "captured_at": _now(),
                       "comments": hits})   # 证据 = 用户名+原话+级别+帖子链接（客户点链接可核验）
        _pause(2, 4)

    cand["intent_posts"] = intent_posts
    tiers = {"high": 0, "medium": 0, "low": 0}
    for x in all_intent:
        tiers[x["grade"]] = tiers.get(x["grade"], 0) + 1
    cand["intent_by_grade"] = tiers                  # 分级计数 高/中/低
    cand["high_intent_count"] = tiers["high"] + tiers["medium"]   # C3 scoring 口径（强+中）
    cand["intent_total"] = sum(tiers.values())       # 含低级（展示口径）
    # ⚠ 展示必须**按级别排序**再取前 8——all_intent 是按帖子顺序累积的，高/中意向若出现在靠后帖子，
    # 会被前面一堆低意向挤出前 8 截掉（客户实测：表头 高1中1低35 却只展示低）。高→中→低，最有价值的先露。
    _go = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(all_intent, key=lambda x: _go.get(x.get("grade"), 3))
    cand["high_intent_snippets"] = [f"@{x['username']}（{x['grade_zh']}）: {x['text']}"
                                    for x in ranked[:8]]           # 交付展示：@用户(级别): 原话，高在前
    cand["promo_intent_hits"] = len(all_intent)
    cand["promotional_post_count"] = promo_count
    cand["comments_read"] = True
    cand["comment_shots"] = []                        # 已弃截图（改结构化文本证据）
    # 存原始评论样本：供以后按语言重判意图（不用重采）+ 客户抽查透明。德/法/意号评论非英文，
    # 意图短语表补齐后可对这些样本离线重判，不必重新烧号。
    cand["comment_sample"] = all_comments[:120]
    # 评论信任分析（有效样本数/低质占比，供 routing comments_insufficient）——高意图数用上面的分级口径
    analysis = cmt_mod.analyze(all_comments)
    cand["comments_analyzed"] = analysis["comments_analyzed"]
    cand["valid_comments"] = analysis["valid_comments"]
    cand["low_quality_ratio"] = analysis["low_quality_ratio"]
    cand["high_intent_ratio"] = (round(cand["high_intent_count"] / analysis["valid_comments"] * 100, 1)
                                 if analysis["valid_comments"] else None)
    cand["sampled_posts"] = posts_meta
    expected_posts = min(max(0, int(n_posts)), len(visit_hrefs))
    cand["deep_target_posts"] = max(0, int(n_posts))
    cand["deep_available_posts"] = len(visit_hrefs)
    cand["deep_successful_posts"] = len(posts_meta)
    cand["deep_failed_posts"] = failed_post_urls
    cand["deep_metric_missing_posts"] = metric_missing_urls
    cand["comment_attempted_posts"] = comment_attempted_posts
    cand["comment_completed_posts"] = comment_completed_posts
    cand["comment_failed_posts"] = comment_failed_urls
    cand["comment_unavailable_posts"] = comment_unavailable_posts
    cand["deep_collection_status"] = (
        "complete"
        if (
            expected_posts > 0
            and len(posts_meta) == expected_posts
            and not failed_post_urls
            and not metric_missing_urls
            and not comment_failed_urls
        )
        else "incomplete"
    )
    cand["pricing_reel_samples"] = pricing_reels
    cand["pricing_captured_at"] = _now()
    cand["pricing_estimate"] = pricing_mod.derive_quote_estimate(cand, _CFG)
    if pricing_error:
        cand["pricing_estimate"]["collection_error"] = pricing_error
    # 评论/ER 重采不得让原本完整的报价证据倒退成 partial/missing。新证据只有质量不低于
    # 旧证据时才替换；相同完整度时保留更新鲜的一次。
    old_estimate = previous_pricing.get("pricing_estimate")
    new_estimate = cand.get("pricing_estimate")
    if _pricing_evidence_rank(old_estimate) > _pricing_evidence_rank(new_estimate):
        for key in (
            "pricing_reel_samples",
            "pricing_captured_at",
            "pricing_estimate",
        ):
            if key in previous_pricing:
                cand[key] = previous_pricing[key]
            else:
                cand.pop(key, None)
    cand["evidence_dir"] = str(ev_dir.relative_to(ROOT))
    # 内容信号（赞助/导购/成分词 + 实算 ER，从帖子赞评/caption 派生）
    cand["posts"] = [{"caption_text": p["caption"], "media_type": 2 if p["is_video"] else 1,
                      "like_count": p.get("like_count"), "comment_count": p.get("comment_count"),
                      "play_count": p.get("play_count"), "pinned": p.get("pinned"),
                      "taken_at": p.get("taken_at")} for p in posts_meta]
    cand.update(content_mod.derive_content_signals(cand, _CFG))
    cand.pop("posts", None)
    caps = " ".join(p.get("caption", "") for p in posts_meta)
    cand["core_niche_key"] = content_mod.derive_niche(cand.get("biography"), cand.get("full_name"), caps)
    _cache_save(cand)   # 浅扫字段回写（stage3 另经 creator_cache.advance 写 stage_json）
    return cand, ev


def _pricing_evidence_rank(estimate):
    """报价证据单调等级；原生样本优先于第三方和 missing。"""
    if not isinstance(estimate, dict):
        return (0, 0)
    status = estimate.get("status")
    sample = int(estimate.get("sample_count") or 0)
    order = {
        "missing": 1,
        "fallback_modash": 2,
        "partial": 3,
        "complete": 4,
    }
    return (order.get(status, 0), sample)


def _cache_save(cand):
    try:
        from extensions.sop_v2 import creator_cache
        creator_cache.upsert(cand)
    except Exception:  # noqa: BLE001
        pass


# 社交/内容链（不算橱窗，不放进"有什么橱窗放什么"）
_SOCIAL = ("instagram.com", "tiktok.com", "youtube.", "youtu.be", "twitter.", "x.com",
           "facebook.", "threads.net", "pinterest.", "snapchat.", "t.me")
_SHOP_RANK = {"Amazon": 0, "LTK": 1, "ShopMy": 2, "自营店": 3, "链接聚合": 4}


def _shop_type(u: str):
    """把一条链接分类成认可橱窗；普通官网/日历/社交链接不冒充自营店。"""
    return storefront_mod.classify_url(u)


def _resolve_storefront(cand, pg):
    """判通用 Storefront（零 API）。

    客户 2026-07-17 口径：Amazon 没有就有什么橱窗放什么。Amazon / LTK / ShopMy /
    明确自营店 / 已识别购物聚合入口均为 ``confirmed_yes``；普通网页不冒充店铺。
    聚合页会优先穿透寻找更具体的 Amazon/购物链接，但穿透失败不能把已识别的聚合入口
    降级成 ``confirmed_no``。
    """
    for key in ("storefront_status", "storefront_url", "storefront_type",
                "amazon_storefront_link"):
        cand.pop(key, None)
    ext = cand.get("external_url")
    links = [ext] if ext else []
    if cand.get("_bio_has_more"):        # "and N more"：点开弹层拿隐藏链接
        try:
            links += _expand_bio_links(pg)
        except Exception:  # noqa: BLE001
            pass
    seen, all_links = set(), []
    for u in links:
        if u and u not in seen:
            seen.add(u)
            all_links.append(u)
    cand["bio_links"] = all_links

    def _best_storefront(urls):
        best, bt = None, None
        for u in urls:
            t = _shop_type(u)
            if t and (bt is None or _SHOP_RANK.get(t, 9) < _SHOP_RANK.get(bt, 9)):
                best, bt = u, t
        return best, bt

    def _set_storefront(url, kind):
        cand["storefront_status"] = "confirmed_yes"
        cand["storefront_url"] = url
        cand["storefront_type"] = kind
        if kind == "Amazon":
            cand["amazon_storefront_link"] = url

    if not all_links:
        cand["storefront_status"] = "unknown"     # 没抓到外链 ≠ 没橱窗 → 交 Review，不误杀
        return
    # 1) 直链里优先 Amazon；其次 LTK / ShopMy / 自营店。
    best, best_type = _best_storefront(all_links)
    if best_type == "Amazon":
        _set_storefront(best, best_type)
        return
    if best_type and best_type != "链接聚合":
        _set_storefront(best, best_type)
        return

    # 2) 通用聚合入口逐个穿透，优先保存更具体的购物目的地。
    aggregators = [u for u in all_links if _shop_type(u) == "链接聚合"]
    for u in aggregators:
        if not _goto(pg, u):
            continue
        pg.wait_for_timeout(4000)
        try:
            hrefs = pg.evaluate(
                "() => [...document.querySelectorAll('a[href]')].map(a => a.href)"
            ) or []
        except Exception:  # noqa: BLE001
            hrefs = []
        target, target_type = _best_storefront(
            [h for h in hrefs if _shop_type(h) != "链接聚合"]
        )
        if target_type:
            _set_storefront(target, target_type)
            return

    # 3) 已识别的购物聚合入口本身也按客户口径保留为橱窗；普通未知网页只记 unknown。
    if best_type == "链接聚合":
        _set_storefront(best, best_type)
        return
    cand["storefront_status"] = "unknown"


def _to_int(s):
    if not s:
        return None
    s = s.strip().replace(",", "")
    mult = 1
    if s and s[-1] in "Kk":
        mult, s = 1000, s[:-1]
    elif s and s[-1] in "Mm":
        mult, s = 1_000_000, s[:-1]
    elif s and s[-1] == "万":
        mult, s = 10000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--posts", type=int, default=10, help="每候选开几个帖子取赞评算实算 ER（客户口径 10）")
    args = ap.parse_args()

    proxy = load_proxy()
    accts = load_accounts()
    if not accts:
        sys.exit("无可用老号")
    print(f"代理={'有' if proxy else '无'} · 账号池 {len(accts)} 个")

    pool = json.loads(Path(args.pool).read_text())
    recs = pool.get("candidates", pool)
    if args.limit:
        recs = recs[:args.limit]

    batch_ev = EVIDENCE_ROOT / args.batch_id
    batch_ev.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    cands = []
    evidence_index = []
    with sync_playwright() as pw:
        for i, rec in enumerate(recs):
            h = (rec.get("handle") or "").lstrip("@")
            if not h:
                continue
            acct = accts[i % len(accts)]
            ev_dir = batch_ev / h
            ev_dir.mkdir(parents=True, exist_ok=True)
            print(f"  [{i+1}/{len(recs)}] @{h} · {acct['username']} …", flush=True)
            ctx = None
            try:
                ctx = open_ctx(pw, acct, proxy)
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                cand, ev = collect_candidate(pg, h, ev_dir, args.posts)
                if cand is None:
                    print(f"     ✗ {ev}", flush=True)
                    cands.append({"handle": h, "collect_failed": True, "note": ev, "campaign_track": None})
                else:
                    cand["modash_er"] = _erf(rec.get("er"))   # Modash ER 作参考并列
                    for e in ev:
                        e["handle"] = h
                        evidence_index.append(e)
                    cands.append(cand)
                    if cand.get("_skipped"):
                        tag = "品牌号跳过"
                    else:
                        tag = f"购买意图{cand.get('high_intent_count',0)}条·截图{len(cand.get('comment_shots',[]))}张"
                    print(f"     ✓ {tag} · 橱窗={cand.get('storefront_status')} · 粉丝={cand.get('follower_count')}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"     ✗ {type(e).__name__}: {str(e)[:60]}", flush=True)
                cands.append({"handle": h, "collect_failed": True, "campaign_track": None})
            finally:
                if ctx:
                    ctx.close()
            # 增量保存：每候选后写盘，中途失败也不丢已采数据
            Path(args.out).write_text(json.dumps(cands, ensure_ascii=False, indent=2))
            (batch_ev / "evidence_index.json").write_text(json.dumps(evidence_index, ensure_ascii=False, indent=2))
            _pause(4, 7)  # 候选间停顿

    Path(args.out).write_text(json.dumps(cands, ensure_ascii=False, indent=2))
    (batch_ev / "evidence_index.json").write_text(json.dumps(evidence_index, ensure_ascii=False, indent=2))
    ok = sum(1 for c in cands if not c.get("collect_failed"))
    print(f"采集 {ok}/{len(cands)} 成功 → {args.out} · 证据 {batch_ev}")
    return 0


def _erf(s):
    if not s:
        return None
    m = re.search(r"([\d.]+)", str(s))
    return float(m.group(1)) if m else None


if __name__ == "__main__":
    raise SystemExit(main())
