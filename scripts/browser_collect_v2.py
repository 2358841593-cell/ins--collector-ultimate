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
from extensions.sop_v2.config import load_config  # noqa: E402

_CFG = load_config()
AGG = ("linktr.ee", "linktree.com", "beacons", "ltk", "liketoknow", "shopltk", "shopmy",
       "komi.io", "stan.store", "vana.ly", "bio.site", "linkin.bio", "lnk.bio", "snipfeed",
       "flow.page", "msha.ke", "tapl.ink", "milkshake", "campsite.bio", "withkoji",
       "desty.page", "desty.link", "carrd.co", "znap.link", "hoo.be", "direct.me", "url.bio",
       "pillar.io", "many.link", "tap.bio", "solo.to", "allmylinks", "linkpop")
AMAZON = ("amazon.", "amzn.to", "/shop/", "storefront")


def load_proxy():
    f = SECRETS / "proxy.txt"
    if not f.exists():
        return None
    url = f.read_text().strip()
    m = re.match(r"https?://([^:]+):([^@]+)@(.+)", url)
    if m:
        return {"server": f"http://{m.group(3)}", "username": m.group(1), "password": m.group(2)}
    return {"server": url}


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
  // 帖子网格 shortcode
  const codes=[...document.querySelectorAll('a')].map(a=>a.getAttribute('href'))
     .filter(h=>h&&/\/(p|reel)\//.test(h));
  const challenge=/verify you'?re a real person|请验证你是真人|confirm you'?re human|suspicious|unusual activity/i.test(body.slice(0,400));
  const loginForm=!!document.querySelector('input[name="username"],input[name="password"]');
  return {
    ogdesc: meta('og:description')||'', ogtitle: meta('og:title')||'',
    header: body.slice(0, 900), external_url: ext, bio_link_text: biolink, biz_buttons: biz,
    codes: [...new Set(codes)].slice(0,12), challenge, login_form: loginForm,
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
        "_scan_source": "browser", "codes": codes,
        "_bio_has_more": _bio_has_more(d.get("bio_link_text")),   # 多链接号：Amazon 可能藏在 "and N more"
    }
    pf["core_niche_key"] = content_mod.derive_niche(bio, name)
    return pf


def open_ctx(pw, acct, proxy, headless=True):
    pd = SECRETS / "chrome-instagram-profiles" / acct["username"]
    pd.mkdir(parents=True, exist_ok=True)
    # 强制英文 UI locale：登出态页面否则随代理 IP 出中文("粉丝"/"关注")，粉丝数/类目解析全失效
    kw = dict(user_data_dir=str(pd), channel="chrome", headless=headless,
              viewport={"width": 1000, "height": 1300}, locale="en-US",
              extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
              args=["--no-first-run", "--no-default-browser-check", "--lang=en-US"])
    if proxy:
        kw["proxy"] = proxy
    ctx = pw.chromium.launch_persistent_context(**kw)
    ctx.add_cookies([
        {"name": "sessionid", "value": acct["sessionid"], "domain": ".instagram.com", "path": "/", "secure": True},
        {"name": "ds_user_id", "value": acct["ds_user_id"], "domain": ".instagram.com", "path": "/", "secure": True},
    ])
    return ctx


def _pause(a=4.0, b=8.0):
    time.sleep(random.uniform(a, b))


def _goto(pg, url, tries=2):
    """代理抖动时重试导航。"""
    for t in range(tries):
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            return True
        except Exception:  # noqa: BLE001
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

    # Amazon 导购硬门槛：本版只做该赛道 → 无 Amazon 橱窗直接早跳（省昂贵深采）
    _resolve_storefront(cand, pg)
    if cand.get("storefront_status") == "confirmed_no":
        cand["_skipped"] = "no_amazon_storefront"
        _cache_save(cand)
        return cand, ev

    # 2-3) 深采（帖子网格 + 推广帖购买意图 + 实算 ER）——抽出为 deep_collect，stage3 复用
    cand["codes"] = codes
    return deep_collect(pg, cand, ev_dir, n_posts)


# 深采窗口大小：og:description 里读赞/评 + 推广帖判定的帖子文本
_DEEP_READ_JS = r"""() => {
  const meta=(p)=>{const e=document.querySelector(`meta[property="${p}"]`);return e?e.content:null;};
  const art=document.querySelector('article')||document.body;
  return {caption: meta('og:description')||'', video: !!meta('og:video'),
          text: (art.innerText||'').slice(0, 12000)}; }"""
_GRID_JS = r"""() => { const codes=[...document.querySelectorAll('a')].map(a=>a.getAttribute('href'))
    .filter(h=>h&&/\/(p|reel)\//.test(h)); return {codes:[...new Set(codes)].slice(0,12),
    logged_out:/创建新账户|Create new account|Log into Instagram/.test(document.body.innerText.slice(0,120))}; }"""

# 直接抽评论 {用户名,原话} 配对（不 OCR、不截图）：从帖子页评论 DOM 抠用户名链接 + 相邻正文
_COMMENTS_JS = r"""() => {
  const art=document.querySelector('article')||document.body;
  const isUser=h=>/^\/[a-zA-Z0-9._]+\/$/.test(h||'') && !/\/(p|reel|reels|explore|stories)\//.test(h||'');
  const SKIP=/^(meta|about|blog|jobs|help|api|privacy|terms|locations|instagram|threads|contact|popular|uploads|directory)$/i;
  const out=[], seen=new Set();
  for(const a of art.querySelectorAll('a[href]')){
    const href=a.getAttribute('href')||''; if(!isUser(href)) continue;
    const uname=href.replace(/\//g,'');
    // 从用户名链接向上找含评论正文的最小容器（通常 2-4 层）
    let box=a.parentElement, text='';
    for(let i=0;i<4&&box;i++){
      const t=(box.innerText||'').trim();
      if(t.length>uname.length+3 && t.length<500){
        const idx=t.indexOf(uname);
        let body=(idx>=0? t.slice(idx+uname.length): t);
        // 去时间戳/Reply/likes 等 UI 行，取评论正文首段
        body=body.replace(/^[\s·•\n]+/,'').split(/\n/).find(l=>{
          const s=l.trim(); return s.length>1 && !/^(\d+\s*(天|周|小时|分钟|d|w|h|min|semanas?|días?|horas?)|回复|reply|responder|like|me gusta|verified|已验证|查看翻译|ver traducción)/i.test(s);
        }) || '';
        text=body.trim(); break;
      }
      box=box.parentElement;
    }
    if(text.length>2 && text.length<260 && !SKIP.test(uname) && !SKIP.test(text)){
      const key=uname+'|'+text.slice(0,24);
      if(!seen.has(key)){ seen.add(key); out.push({username:uname, text:text.slice(0,240)}); }
    }
    if(out.length>=80) break;
  }
  return out;
}"""


def deep_collect(pg, cand, ev_dir, n_posts=10):
    """③深采：对已浅扫的 cand（含 codes/handle）开 N 帖 → 只在推广帖找购买意图评论+截图
    + 帖子赞评算实算 ER。纯浏览器零 API。返回 (cand, evidence)；logged_out → (None, "logged_out")。"""
    from extensions.sop_v2 import comments as cmt_mod
    handle = cand.get("handle")
    ev = []
    ev_dir.mkdir(parents=True, exist_ok=True)
    codes = cand.get("codes") or []
    if not codes:  # 缓存/种子无网格 → 现取一次
        if not _goto(pg, f"https://www.instagram.com/{handle}/"):
            return cand, ev
        pg.wait_for_timeout(5000)
        g = pg.evaluate(_GRID_JS)
        if g.get("logged_out"):
            return None, "logged_out"
        for _ in range(3):
            if g.get("codes"):
                break
            pg.mouse.wheel(0, 1200)
            pg.wait_for_timeout(3500)
            g = pg.evaluate(_GRID_JS)
        codes = g.get("codes", [])
    _pause(2, 4)

    posts_meta, intent_posts, all_intent, promo_count = [], [], [], 0
    all_comments, seen_c = [], set()           # 累积评论样本（供 comments.analyze 算有效样本/信任分）
    for i, href in enumerate(codes[:n_posts]):
        purl = f"https://www.instagram.com{href}"
        if not _goto(pg, purl):
            continue
        pg.wait_for_timeout(3000)
        info = pg.evaluate(_DEEP_READ_JS)
        likes, comments, caption = _post_stats(info.get("caption", ""))   # 赞/评/干净caption(算实算ER)
        posts_meta.append({"code": href, "caption": caption, "is_video": info.get("video"),
                           "like_count": likes, "comment_count": comments})
        is_promo = cmt_mod.is_promotional(caption)
        if is_promo:
            promo_count += 1
        # 采评论的帖：推广帖 或 高评论帖（购买意图常出现在高互动帖，不只带货词帖）
        if not (is_promo or (comments or 0) >= 15):
            _pause(1.5, 3)
            continue
        _load_comments(pg, rounds=4)
        # 直接抽 {用户名,原话} 配对（不 OCR、不截图）
        paired = pg.evaluate(_COMMENTS_JS) or []
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
    cand["high_intent_snippets"] = [f"@{x['username']}（{x['grade_zh']}）: {x['text']}"
                                    for x in all_intent[:8]]       # 交付展示：@用户(级别): 原话
    cand["promo_intent_hits"] = len(all_intent)
    cand["promotional_post_count"] = promo_count
    cand["comments_read"] = True
    cand["comment_shots"] = []                        # 已弃截图（改结构化文本证据）
    # 评论信任分析（有效样本数/低质占比，供 routing comments_insufficient）——高意图数用上面的分级口径
    analysis = cmt_mod.analyze(all_comments)
    cand["comments_analyzed"] = analysis["comments_analyzed"]
    cand["valid_comments"] = analysis["valid_comments"]
    cand["low_quality_ratio"] = analysis["low_quality_ratio"]
    cand["high_intent_ratio"] = (round(cand["high_intent_count"] / analysis["valid_comments"] * 100, 1)
                                 if analysis["valid_comments"] else None)
    cand["sampled_posts"] = posts_meta
    cand["evidence_dir"] = str(ev_dir.relative_to(ROOT))
    # 内容信号（赞助/导购/成分词 + 实算 ER，从帖子赞评/caption 派生）
    cand["posts"] = [{"caption_text": p["caption"], "media_type": 2 if p["is_video"] else 1,
                      "like_count": p.get("like_count"), "comment_count": p.get("comment_count"),
                      "play_count": 0} for p in posts_meta]
    cand.update(content_mod.derive_content_signals(cand, _CFG))
    cand.pop("posts", None)
    caps = " ".join(p.get("caption", "") for p in posts_meta)
    cand["core_niche_key"] = content_mod.derive_niche(cand.get("biography"), cand.get("full_name"), caps)
    _cache_save(cand)   # 浅扫字段回写（stage3 另经 creator_cache.advance 写 stage_json）
    return cand, ev


def _cache_save(cand):
    try:
        from extensions.sop_v2 import creator_cache
        creator_cache.upsert(cand)
    except Exception:  # noqa: BLE001
        pass


def _resolve_storefront(cand, pg):
    """判 storefront（零 API）。收集全部 bio 链接（首链 + 多链接号弹层里的其余），
    任一直链 Amazon → confirmed_yes；聚合链穿透找 Amazon；聚合都没穿到 → confirmed_no；否则 unknown。"""
    ext = cand.get("external_url")
    links = [ext] if ext else []
    if cand.get("_bio_has_more"):        # "and N more"：点开弹层拿隐藏链接（Amazon 常藏这）
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
    if not all_links:
        # 没抓到外链 ≠ 一定没橱窗（抽取可能漏）→ unknown（交 Review），绝不 confirmed_no 误杀
        cand["storefront_status"] = "unknown"
        return
    # 1) 任意直链 Amazon
    for u in all_links:
        if any(a in u.lower() for a in AMAZON):
            cand["storefront_status"] = "confirmed_yes"
            cand["amazon_storefront_link"] = u
            return
    # 2) 聚合链逐个穿透找 Amazon
    penetrated = False
    for u in all_links:
        if any(g in u.lower() for g in AGG):
            penetrated = True
            if _goto(pg, u):
                pg.wait_for_timeout(4000)
                amz = pg.evaluate(r"""() => { const hs=[...document.querySelectorAll('a[href]')].map(a=>a.href);
                  const shop=hs.find(h=>/amazon\.[a-z.]+\/(shop|storefront)/i.test(h));
                  const any=hs.find(h=>/amazon\.|amzn\.to/i.test(h)); return shop||any||null; }""")
                if amz:
                    cand["storefront_status"] = "confirmed_yes"
                    cand["amazon_storefront_link"] = amz
                    return
    # 3) 有聚合链但都没穿到 Amazon → confirmed_no；只有非聚合外链 → unknown
    cand["storefront_status"] = "confirmed_no" if penetrated else "unknown"


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
