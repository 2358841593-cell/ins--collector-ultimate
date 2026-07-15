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
AGG = ("linktr.ee", "beacons", "ltk", "liketoknow", "shopmy", "komi.io", "stan.store")
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


def load_accounts():
    """老号：username | pw | totp | cookie(含 sessionid+ds_user_id) | ..."""
    out = []
    for raw in (SECRETS / "accounts_raw.txt").read_text().splitlines():
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


def make_iloader(acct, proxy):
    """instaloader 会话（代理 + cookie），用于可靠取 profile 字段。"""
    import instaloader
    # max_connection_attempts=1：429 时快速失败，不做多分钟指数退避（"太慢"根因）
    L = instaloader.Instaloader(quiet=True, request_timeout=20, max_connection_attempts=1)
    if proxy:
        purl = f"http://{proxy['username']}:{proxy['password']}@{proxy['server'].split('//')[-1]}" \
            if proxy.get("username") else proxy["server"]
        L.context._session.proxies = {"http": purl, "https": purl}
    L.context._session.cookies.set("sessionid", acct["sessionid"], domain=".instagram.com")
    L.context._session.cookies.set("ds_user_id", acct["ds_user_id"], domain=".instagram.com")
    return L


BRAND_CATEGORIES = ("cosmetic", "skin care service", "product", "shopping", "retail",
                    "brand", "store", "company", "e-commerce", "wholesale")
# bio/名字里的店铺/品牌信号（浏览器浅扫时辅助判品牌号，类目拿不到时兜底）
BRAND_NAME_KW = ("shop", "store", "tienda", "boutique", "oficial", "official", "cosmetics",
                 "cosmetica", "cosmética", "skincare co", "beauty co", "brand", "marca",
                 "wholesale", "distribuidor", "laboratorio", "farmacia")


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
  // bio 外链：头部锚点里指向 l.instagram.com/?u= 或非 instagram 域名的
  let ext=null;
  for(const a of document.querySelectorAll('header a, section a, main a')){
    const h=a.getAttribute('href')||'';
    if(/l\.instagram\.com\/\?u=/.test(h)){ try{ext=decodeURIComponent(h.split('u=')[1].split('&')[0]);}catch(e){ext=h;} break; }
    if(/^https?:\/\//.test(h) && !/instagram\.com|threads\.net|facebook\.com/.test(h)){ ext=h; break; }
  }
  // 商业按钮信号（专业/商业号才有）
  const biz=/\b(Email|Correo|Contact|Contactar|Message|Shop|Tienda|View shop|Book now|Reservar|Call)\b/i.test(body.slice(0,1200));
  // 帖子网格 shortcode
  const codes=[...document.querySelectorAll('a')].map(a=>a.getAttribute('href'))
     .filter(h=>h&&/\/(p|reel)\//.test(h));
  const challenge=/verify you'?re a real person|请验证你是真人|confirm you'?re human|suspicious|unusual activity/i.test(body.slice(0,400));
  const loginForm=!!document.querySelector('input[name="username"],input[name="password"]');
  return {
    ogdesc: meta('og:description')||'', ogtitle: meta('og:title')||'',
    header: body.slice(0, 900), external_url: ext, biz_buttons: biz,
    codes: [...new Set(codes)].slice(0,12), challenge, login_form: loginForm,
    private: /This account is private|Esta cuenta es privada|cuenta privada|账号私密|This Account is Private/i.test(body)
  };
}"""


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
    # og 元标签服务端渲染，登出/挂登录 banner 也在；据此解析粉丝/名字
    og = d.get("ogdesc") or ""
    m = re.search(r"([\d.,]+\s*[KMkm]?)\s*(?:Followers|Seguidores|seguidores|Abonnés)", og, re.I)
    followers = _parse_count(m.group(1)) if m else None
    codes = d.get("codes") or []
    # 挑战页/纯登录墙且拿不到任何公开数据 → 判 wall（无法浅扫）
    if (d.get("challenge") or (d.get("login_form") and not followers and not codes)):
        return {"_wall": True}
    header = d.get("header") or ""
    hl = header.lower()
    is_business = bool(d.get("biz_buttons")) or any(k in hl for k in BRAND_CATEGORIES)
    name_blob = (d.get("ogtitle") or "") + " " + header
    is_brand = is_business and (any(k in hl for k in BRAND_CATEGORIES)
                                or any(k in name_blob.lower() for k in BRAND_NAME_KW))
    pf = {
        "handle": handle, "follower_count": followers,
        "full_name": (d.get("ogtitle") or "").split("(@")[0].strip() or None,
        "biography": header, "external_url": d.get("external_url"),
        "is_private": bool(d.get("private")), "is_business": is_business,
        "category": None, "brand_account_type": "brand" if is_brand else "personal",
        "_scan_source": "browser", "codes": codes,
    }
    pf["core_niche_key"] = content_mod.derive_niche(pf.get("biography"), pf.get("full_name"))
    return pf


def fetch_profile_il(L, handle, use_cache=True):
    """取 profile 浅扫字段：先查缓存库（命中新鲜即用，零 IG 请求），否则 instaloader 扫 + 回写库。"""
    from extensions.sop_v2 import creator_cache
    if use_cache:
        cached = creator_cache.get(handle, max_age_days=30)
        if cached:
            return cached
    import instaloader
    try:
        p = instaloader.Profile.from_username(L.context, handle)
    except Exception:  # noqa: BLE001
        return None
    cat = (p.business_category_name or "").lower()
    is_brand = bool(p.is_business_account) and any(k in cat for k in BRAND_CATEGORIES)
    pf = {
        "handle": handle,
        "full_name": p.full_name, "follower_count": p.followers, "media_count": p.mediacount,
        "biography": p.biography, "external_url": p.external_url or None,
        "is_verified": p.is_verified, "is_private": p.is_private,
        "is_business": bool(p.is_business_account), "category": p.business_category_name,
        "brand_account_type": "brand" if is_brand else "personal",
    }
    # 赛道也存（浅扫可判）
    pf["core_niche_key"] = content_mod.derive_niche(pf.get("biography"), pf.get("full_name"))
    try:
        creator_cache.upsert(pf)
    except Exception:  # noqa: BLE001
        pass
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

    # 2) 帖子网格 shortcode（浏览器浅扫已拿到；缓存命中则需重取一次）
    if not codes:
        if not _goto(pg, f"https://www.instagram.com/{handle}/"):
            return cand, ev
        pg.wait_for_timeout(5000)
        get_codes = r"""() => { const codes=[...document.querySelectorAll('a')].map(a=>a.getAttribute('href'))
            .filter(h=>h&&/\/(p|reel)\//.test(h)); return {codes:[...new Set(codes)].slice(0,12),
            logged_out:/创建新账户|Create new account/.test(document.body.innerText.slice(0,120))}; }"""
        g = pg.evaluate(get_codes)
        if g.get("logged_out"):
            return None, "logged_out"
        for _ in range(3):
            if g.get("codes"):
                break
            pg.mouse.wheel(0, 1200)
            pg.wait_for_timeout(3500)
            g = pg.evaluate(get_codes)
        codes = g.get("codes", [])
    prof = {"codes": codes}
    _pause(2, 4)

    # 2/3) 开 N 个帖子：读评论文字判购买意图 → 有意义才截图，截不到就用链接
    from extensions.sop_v2 import comments as cmt_mod
    codes = prof.get("codes") or []
    posts_meta = []
    comment_shots = []       # 有购买意图且截图成功的证据
    intent_posts = []        # 有购买意图的帖子（链接 + 原话），截图失败也留
    all_snips = []
    promo_count = 0          # 采样帖里明显带货/导购的帖数（带货型红人的核心信号）
    read_js = r"""() => {
      const meta=(p)=>{const e=document.querySelector(`meta[property="${p}"]`);return e?e.content:null;};
      const art=document.querySelector('article')||document.body;
      return {caption: meta('og:description')||'', video: !!meta('og:video'),
              text: (art.innerText||'').slice(0, 12000)}; }"""
    for i, href in enumerate(codes[:n_posts]):
        purl = f"https://www.instagram.com{href}"
        if not _goto(pg, purl):
            continue
        pg.wait_for_timeout(3000)
        info = pg.evaluate(read_js)
        likes, comments, caption = _post_stats(info.get("caption", ""))   # 赞/评/干净caption(算实算ER)
        posts_meta.append({"code": href, "caption": caption, "is_video": info.get("video"),
                           "like_count": likes, "comment_count": comments})
        # 只在"明显推广/导购帖"里找购买意图（用户要求：先找推广帖，再看意图）
        is_promo = cmt_mod.is_promotional(caption)
        if not is_promo:
            _pause(1.5, 3)
            continue
        promo_count += 1
        _load_comments(pg, rounds=4)                       # 展开更多评论
        text = pg.evaluate(read_js).get("text", "")
        snips = cmt_mod.find_intent_in_text(text, promo_context=True)   # 推广帖上下文放宽到"考虑购买"问句
        if snips:
            all_snips.extend(snips)
            rec = {"post_url": purl, "snippets": snips[:2], "screenshot": None, "promotional": True}
            _scroll_snippet_into_view(pg, snips[0])        # 把意图评论滚到中央再截，保证拍到
            pg.wait_for_timeout(600)
            shotpath = ev_dir / f"intent_{i+1:02d}.png"
            if _shot(pg, shotpath):
                rel = str(shotpath.relative_to(ROOT))
                comment_shots.append(rel)
                rec["screenshot"] = rel
                ev.append({"type": "intent_comment", "path": rel, "source_url": purl,
                           "captured_at": _now(), "snippets": snips[:2]})
            else:  # 截图失败不死磕，用帖子链接给客户自行核验
                ev.append({"type": "intent_comment_link", "path": None, "source_url": purl,
                           "captured_at": _now(), "snippets": snips[:2], "note": "截图失败,用链接核验"})
            intent_posts.append(rec)
        _pause(2, 4)
    cand["intent_posts"] = intent_posts
    cand["high_intent_snippets"] = all_snips[:5]
    cand["high_intent_count"] = len(all_snips)
    cand["promotional_post_count"] = promo_count
    cand["comments_read"] = True

    cand["comment_shots"] = comment_shots
    cand["sampled_posts"] = posts_meta
    # 内容信号（赞助/导购/成分词 + 实算 ER，从帖子赞评/caption 派生）
    cand["posts"] = [{"caption_text": p["caption"], "media_type": 2 if p["is_video"] else 1,
                      "like_count": p.get("like_count"), "comment_count": p.get("comment_count"),
                      "play_count": 0} for p in posts_meta]
    cand.update(content_mod.derive_content_signals(cand, _CFG))
    cand.pop("posts", None)
    # 赛道从 bio + 全名 + caption 派生（精化 storefront 早筛时的 bio-only 判断）
    caps = " ".join(p.get("caption", "") for p in posts_meta)
    cand["core_niche_key"] = content_mod.derive_niche(cand.get("biography"), cand.get("full_name"), caps)
    _cache_save(cand)   # 完整浅扫数据回写缓存库（storefront 已在早筛时 resolve）
    return cand, ev


def _cache_save(cand):
    try:
        from extensions.sop_v2 import creator_cache
        creator_cache.upsert(cand)
    except Exception:  # noqa: BLE001
        pass


def _resolve_storefront(cand, pg):
    """按浏览器浅扫拿到的 external_url 判 storefront；聚合页用当前页穿透（零 API）。"""
    ext = cand.get("external_url")
    cand["bio_links"] = [ext] if ext else []
    joined = (ext or "").lower()
    if not ext:
        cand["storefront_status"] = "confirmed_no"
        return
    if any(a in joined for a in AMAZON):
        cand["storefront_status"] = "confirmed_yes"
        cand["amazon_storefront_link"] = ext
        return
    if any(g in joined for g in AGG):
        cand["storefront_status"] = "unknown"
        if _goto(pg, ext):
            pg.wait_for_timeout(4000)
            amz = pg.evaluate(r"""() => { const hs=[...document.querySelectorAll('a[href]')].map(a=>a.href);
              const shop=hs.find(h=>/amazon\.[a-z.]+\/(shop|storefront)/i.test(h));
              const any=hs.find(h=>/amazon\.|amzn\.to/i.test(h)); return shop||any||null; }""")
            if amz:
                cand["storefront_status"] = "confirmed_yes"
                cand["amazon_storefront_link"] = amz
            else:
                cand["storefront_status"] = "confirmed_no"
    else:
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
