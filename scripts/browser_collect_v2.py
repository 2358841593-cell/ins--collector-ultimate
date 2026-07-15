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


def collect_candidate(pg, handle, ev_dir, sample_posts=3):
    """采一个候选：profile + 帖子 + 评论区截图。返回候选 dict + 证据列表。"""
    ev = []
    cand = {"handle": handle, "profile_url": f"https://www.instagram.com/{handle}/",
            "discovery_source": "Modash Discover / ai_search", "discovered_via": "modash_search",
            "captured_at": time.strftime("%Y-%m-%d %H:%M")}

    # 1) profile 页
    pg.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(6000)
    get_prof = r"""() => {
      const meta=(p)=>{const e=document.querySelector(`meta[property="${p}"]`);return e?e.content:null;};
      const codes=[...document.querySelectorAll('a')].map(a=>a.getAttribute('href'))
        .filter(h=>h&&/\/(p|reel)\//.test(h));
      return {title:meta('og:title'), desc:meta('og:description'), codes:[...new Set(codes)].slice(0,12),
              logged_out: /创建新账户|Create new account/.test(document.body.innerText.slice(0,120))};
    }"""
    prof = pg.evaluate(get_prof)
    if prof.get("logged_out"):
        return None, "logged_out"
    # 帖子网格没加载 → 滚动 + 再等 + 重试（最多 3 次）
    for _ in range(3):
        if prof.get("codes"):
            break
        pg.mouse.wheel(0, 1200)
        pg.wait_for_timeout(3500)
        prof = pg.evaluate(get_prof)
    if not prof.get("codes"):
        return None, "no_post_grid"
    # 解析 og
    title = prof.get("title") or ""
    m = re.match(r"(.+?)\s*\(@", title)
    cand["full_name"] = m.group(1).strip() if m else None
    desc = prof.get("desc") or ""
    fm = re.search(r"([\d.,]+\s*[KMkm万]?)\s*(?:Followers|位?粉丝)", desc)
    cand["follower_count"] = _to_int(fm.group(1)) if fm else None
    profshot = ev_dir / "profile.png"
    pg.screenshot(path=str(profshot))
    ev.append({"type": "profile", "path": str(profshot.relative_to(ROOT)),
               "source_url": cand["profile_url"], "captured_at": _now()})
    _pause()

    # 2/3) 采样帖子 → 评论区截图
    codes = prof.get("codes") or []
    posts_meta = []
    comment_shots = []
    for i, href in enumerate(codes[:sample_posts]):
        pg.goto(f"https://www.instagram.com{href}", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(7000)
        pmeta = pg.evaluate(r"""() => {
          const meta=(p)=>{const e=document.querySelector(`meta[property="${p}"]`);return e?e.content:null;};
          const cm=(document.querySelector('svg[aria-label*="Comment"],svg[aria-label*="评论"]')||{});
          return {caption: meta('og:description')||'', video: !!meta('og:video'),
                  media: meta('og:video')||meta('og:image')||''};
        }""")
        shot = ev_dir / f"comments_{i+1:02d}.png"
        pg.screenshot(path=str(shot))
        comment_shots.append(str(shot.relative_to(ROOT)))
        ev.append({"type": "comment_area", "path": str(shot.relative_to(ROOT)),
                   "source_url": f"https://www.instagram.com{href}", "captured_at": _now(),
                   "note": "供视觉读购买意图评论"})
        posts_meta.append({"code": href, "caption": pmeta.get("caption", ""),
                           "is_video": pmeta.get("video"), "media_url": pmeta.get("media", "")})
        _pause()

    cand["comment_shots"] = comment_shots
    cand["sampled_posts"] = posts_meta
    # 内容信号（从 caption 派生；赞助/导购/成分等）
    cand["posts"] = [{"caption_text": p["caption"], "media_type": 2 if p["is_video"] else 1,
                      "like_count": None, "comment_count": None, "play_count": 0} for p in posts_meta]
    cand.update(content_mod.derive_content_signals(cand, _CFG))
    cand.pop("posts", None)
    return cand, ev


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
    ap.add_argument("--sample-posts", type=int, default=3)
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
                cand, ev = collect_candidate(pg, h, ev_dir, args.sample_posts)
                if cand is None:
                    print(f"     ✗ {ev}", flush=True)
                    cands.append({"handle": h, "collect_failed": True, "note": ev, "campaign_track": None})
                else:
                    cand["general_er"] = _erf(rec.get("er"))
                    cand["storefront_status"] = None  # 由后续穿透补
                    for e in ev:
                        e["handle"] = h
                        evidence_index.append(e)
                    cands.append(cand)
                    print(f"     ✓ {len(cand.get('comment_shots',[]))} 张评论截图", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"     ✗ {type(e).__name__}: {str(e)[:60]}", flush=True)
                cands.append({"handle": h, "collect_failed": True, "campaign_track": None})
            finally:
                if ctx:
                    ctx.close()
            _pause(6, 12)  # 候选间更长停顿

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
