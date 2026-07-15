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
    L = instaloader.Instaloader(quiet=True, request_timeout=30)
    if proxy:
        purl = f"http://{proxy['username']}:{proxy['password']}@{proxy['server'].split('//')[-1]}" \
            if proxy.get("username") else proxy["server"]
        L.context._session.proxies = {"http": purl, "https": purl}
    L.context._session.cookies.set("sessionid", acct["sessionid"], domain=".instagram.com")
    L.context._session.cookies.set("ds_user_id", acct["ds_user_id"], domain=".instagram.com")
    return L


BRAND_CATEGORIES = ("cosmetic", "skin care service", "product", "shopping", "retail",
                    "brand", "store", "company", "e-commerce", "wholesale")


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
    kw = dict(user_data_dir=str(pd), channel="chrome", headless=headless,
              viewport={"width": 1000, "height": 1300}, args=["--no-first-run", "--no-default-browser-check"])
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


def collect_candidate(L, pg, handle, ev_dir, n_posts=8):
    """采一个候选：profile(instaloader) + 品牌早筛 + N 帖评论截图 + Storefront。"""
    ev = []
    cand = {"handle": handle, "profile_url": f"https://www.instagram.com/{handle}/",
            "discovery_source": "Modash Discover / ai_search", "discovered_via": "modash_search",
            "captured_at": time.strftime("%Y-%m-%d %H:%M")}

    # 1) profile 字段走 instaloader（可靠：外链/商业号/类目/粉丝）
    pf = fetch_profile_il(L, handle)
    if pf is None:
        return None, "profile_fetch_failed"
    cand.update(pf)
    if pf.get("is_private"):
        return cand, ev  # 私密号：交给 gate 判 Exclude，不深采
    # 品牌号早筛：直接标记，跳过昂贵的帖子/评论采集
    if pf.get("brand_account_type") == "brand":
        cand["_skipped"] = "brand_account"
        # 仍从外链判 storefront（品牌号也可能有橱窗，供参考）
        _resolve_storefront(cand, pg)
        _cache_save(cand)
        return cand, ev

    # 2) 渲染 profile 页取帖子网格（评论截图用）
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
    prof = {"codes": g.get("codes", [])}
    _pause(2, 4)

    # 2/3) 开 N 个帖子：读评论文字判购买意图 → 有意义才截图，截不到就用链接
    from extensions.sop_v2 import comments as cmt_mod
    codes = prof.get("codes") or []
    posts_meta = []
    comment_shots = []       # 有购买意图且截图成功的证据
    intent_posts = []        # 有购买意图的帖子（链接 + 原话），截图失败也留
    all_snips = []
    for i, href in enumerate(codes[:n_posts]):
        purl = f"https://www.instagram.com{href}"
        if not _goto(pg, purl):
            continue
        pg.wait_for_timeout(3500)
        # 滚动评论区加载更多评论（提高命中真实购买意图；桌面版评论在右侧列表）
        for _ in range(3):
            pg.mouse.wheel(0, 1200)
            pg.wait_for_timeout(1200)
        info = pg.evaluate(r"""() => {
          const meta=(p)=>{const e=document.querySelector(`meta[property="${p}"]`);return e?e.content:null;};
          const art=document.querySelector('article')||document.body;
          return {caption: meta('og:description')||'', video: !!meta('og:video'),
                  text: (art.innerText||'').slice(0, 9000)}; }""")
        posts_meta.append({"code": href, "caption": info.get("caption", ""), "is_video": info.get("video")})
        snips = cmt_mod.find_intent_in_text(info.get("text", ""))
        if snips:
            all_snips.extend(snips)
            rec = {"post_url": purl, "snippets": snips[:2], "screenshot": None}
            # 有明确购买意图 → 截图（有意义的才截）；截不到就只留链接+原话
            shotpath = ev_dir / f"intent_{i+1:02d}.png"
            if _shot(pg, shotpath):
                rel = str(shotpath.relative_to(ROOT))
                comment_shots.append(rel)
                rec["screenshot"] = rel
                ev.append({"type": "intent_comment", "path": rel, "source_url": purl,
                           "captured_at": _now(), "snippets": snips[:2]})
            else:
                ev.append({"type": "intent_comment_link", "path": None, "source_url": purl,
                           "captured_at": _now(), "snippets": snips[:2], "note": "截图失败,用链接核验"})
            intent_posts.append(rec)
        _pause(2, 4)
    cand["intent_posts"] = intent_posts
    cand["high_intent_snippets"] = all_snips[:5]
    cand["high_intent_count"] = len(all_snips)
    cand["comments_read"] = True

    cand["comment_shots"] = comment_shots
    cand["sampled_posts"] = posts_meta
    # 内容信号（赞助/导购/成分词，从 caption 派生；赞评 ER 后续视觉读截图补）
    cand["posts"] = [{"caption_text": p["caption"], "media_type": 2 if p["is_video"] else 1,
                      "like_count": None, "comment_count": None, "play_count": 0} for p in posts_meta]
    cand.update(content_mod.derive_content_signals(cand, _CFG))
    cand.pop("posts", None)
    # 赛道从 bio + 全名 + caption 派生
    caps = " ".join(p.get("caption", "") for p in posts_meta)
    cand["core_niche_key"] = content_mod.derive_niche(cand.get("biography"), cand.get("full_name"), caps)

    # 4) Storefront（instaloader 已给外链）
    _resolve_storefront(cand, pg)
    _cache_save(cand)   # 完整浅扫数据回写缓存库（含 storefront + 精化赛道）
    return cand, ev


def _cache_save(cand):
    try:
        from extensions.sop_v2 import creator_cache
        creator_cache.upsert(cand)
    except Exception:  # noqa: BLE001
        pass


def _resolve_storefront(cand, pg):
    """按 instaloader 拿到的 external_url 判 storefront；聚合页用当前页穿透。"""
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
    ap.add_argument("--posts", type=int, default=4, help="每候选开几个帖子取赞评算 IG ER")
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
                L = make_iloader(acct, proxy)
                ctx = open_ctx(pw, acct, proxy)
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                cand, ev = collect_candidate(L, pg, h, ev_dir, args.posts)
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
