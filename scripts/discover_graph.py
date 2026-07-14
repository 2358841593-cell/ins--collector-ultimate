#!/usr/bin/env python3
"""复合发现引擎 —— 把三种架构合一，最大化发现范围：

  ① 现有 discover：账号池轮换 + 私有 API 爬取 + 漏斗/赛道评估（复用其函数）
  ② Modash 式「索引优先」：所有爬过的创作者落进持久索引(SQLite + bio FTS)，
     查询只查索引（瞬时、零账号消耗），跨 run 累积、越用越大
  ③ influencer-graph 式「社区图中心性」：用 IG 自家 suggested(相似账号) 等
     便宜的边建相似度图，跑 PageRank/eigenvector 找"社区中心"创作者

扩大发现范围的关键：
  · 多跳 snowball（hops≥2）：对口创作者→相似账号→再相似，呈指数扩散
  · 图引导优先级：优先从"对口/高中心度"节点扩展，预算不浪费在跨垂类分支
  · 索引去重 + 新鲜度：不重爬已知创作者，发现量跨 run 复利累积

用法：
  建库(扩散爬取，用账号池)：
    .venv/bin/python scripts/discover_graph.py ingest --hops 2 --budget 200 --wait-pool
  查询(瞬时，不碰账号)：
    .venv/bin/python scripts/discover_graph.py search --bio amazon --fit 对口 --min-followers 10000
  重算图中心性 / 看统计：
    .venv/bin/python scripts/discover_graph.py rank
    .venv/bin/python scripts/discover_graph.py stats
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "discovery.db"
sys.path.insert(0, str(ROOT / "scripts"))

from account_pool import AccountPool, PoolExhausted, load_pool_accounts  # noqa: E402
from discover import (  # noqa: E402
    as_plain, fetch_user_info, fetch_user_medias, load_seeds_toml,
    _analyze_content, _calc_engagement, _check_bio_links, _check_sponsorship,
    _detect_niche, _is_brand_like, _add_candidate,
)

FIT_RANK = {"对口": 3, "相关": 2, "未识别": 1, "跨垂类": 0}
_HAS_FTS = False  # bio 全文索引是否可用（_conn 时探测）


def _log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


# ─── 索引 + 图 schema ──────────────────────────────────────────────────────────

# creator_index = 基本库(quality_tier=1) + 高质量库(quality_tier=2)。
# 字段力求"全"——Excel 输出的每一项都有列，外加 data_json 存完整记录（含分解/评论样例
# 等嵌套结构），保证数据资产无损、可查询、可直接对外售卖/建站。
SCHEMA = """
CREATE TABLE IF NOT EXISTS creator_index(
  -- 身份
  handle TEXT PRIMARY KEY, pk TEXT, full_name TEXT, bio TEXT, profile_url TEXT, category TEXT,
  followers INT, following INT, media_count INT, is_verified INT, tier_label TEXT,
  -- Bio / Amazon 橱窗
  has_amazon TEXT, bio_link_type TEXT, bio_link_url TEXT,
  -- 赛道 / 内容
  niche TEXT, niche_secondary TEXT, fit TEXT, archetype TEXT,
  product_rec REAL, authority INT, authority_evidence TEXT,
  -- 赞助 / 互动
  sponsored_count INT, sponsored REAL, organic_posts INT,
  reels_count INT, reels_avg_likes REAL, reels_avg_comments REAL, reels_er REAL,
  static_count INT, static_avg_likes REAL, static_avg_comments REAL, static_er REAL,
  engagement REAL, meets_benchmark INT,
  -- 评论信任（ingest 不带评论时为空，后续 enrich 填）
  comments_analyzed INT, intent_ratio REAL, bot_ratio REAL, pod_ratio REAL,
  low_quality_ratio REAL, trust_level TEXT,
  -- 综合
  status TEXT, score REAL, centrality REAL DEFAULT 0,
  -- 发现/血缘
  hop INT, discovered_via TEXT, first_seen TEXT, last_crawled TEXT,
  -- 两层库 + 运营元数据（高质量库用；现仅建列，暂不启用晋升流程）
  quality_tier INT DEFAULT 1, verified INT DEFAULT 0, promoted_at TEXT, outreach TEXT,
  -- 完整记录（含 top_intent_comments / pod_samples / *_breakdown / reasons 等嵌套）
  data_json TEXT);
CREATE TABLE IF NOT EXISTS graph_edges(
  src TEXT, dst TEXT, kind TEXT, PRIMARY KEY(src, dst, kind));
CREATE INDEX IF NOT EXISTS idx_ci_fit ON creator_index(fit);
CREATE INDEX IF NOT EXISTS idx_ci_status ON creator_index(status);
CREATE INDEX IF NOT EXISTS idx_ci_tier ON creator_index(quality_tier);
CREATE INDEX IF NOT EXISTS idx_ci_amazon ON creator_index(has_amazon);
"""


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    # bio 全文索引（FTS5；不可用则降级为 LIKE）
    global _HAS_FTS
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS creator_fts "
                  "USING fts5(handle, bio, full_name)")
        c.execute("SELECT 1 FROM creator_fts LIMIT 1")
        _HAS_FTS = True
    except sqlite3.OperationalError:
        _HAS_FTS = False
    return c


def _is_fresh(row_last: str | None, ttl_days: int) -> bool:
    if not row_last:
        return False
    try:
        dt = datetime.fromisoformat(row_last)
        return (datetime.now(timezone.utc) - dt).days < ttl_days
    except Exception:
        return False


# ─── 评估一个创作者（复用 discover 的漏斗/赛道函数）──────────────────────────────

def evaluate(profile: dict, posts: list, cfg: dict) -> dict:
    cand = dict(profile)
    cand["posts"] = posts
    followers = profile.get("follower_count") or 0
    in_range = cfg["min_followers"] <= followers <= cfg["max_followers"]

    cand.update(_check_bio_links(cand))
    cand.update(_analyze_content(cand))
    cand.update(_detect_niche(cand))
    cand.update(_check_sponsorship(cand))
    cand.update(_calc_engagement(cand, cfg.get("engagement_benchmarks", {})))

    has_amazon = cand.get("has_amazon_storefront")
    brand_like = _is_brand_like(cand, cfg.get("brand_handles", []))
    er = max(cand.get("reels_engagement_rate") or 0, cand.get("static_engagement_rate") or 0)

    # 紧凑漏斗：硬门槛粉丝范围 + Amazon + 非品牌号
    if brand_like:
        status = "exclude"
    elif not in_range:
        status = "exclude"
    elif has_amazon is False:
        status = "exclude"           # 无 Amazon 橱窗（硬门槛）
    elif has_amazon is True and cand.get("campaign_fit") == "core":
        status = "include"
    else:
        status = "review"            # 有橱窗待验证 / 跨垂类 等

    # 评分（与 discover 同思路，× 赛道对口系数）
    score = (min(100, er * 15) * 0.30 + min(100, (cand.get("product_rec_ratio") or 0) * 150) * 0.25
             + min(100, cand.get("skincare_authority_score") or 0) * 0.20
             + max(0, 100 - (cand.get("sponsored_ratio") or 0) * 250) * 0.10
             + (100 if has_amazon is True else (60 if has_amazon == "unverified" else 0)) * 0.15)
    score *= {"core": 1.0, "related": 0.9, "off": 0.55}.get(cand.get("campaign_fit"), 0.85)

    cand["_eval"] = {"status": status, "score": round(score, 1), "engagement": round(er, 2),
                     "in_range": in_range}
    return cand


def upsert(c: sqlite3.Connection, cand: dict, hop: int, via: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    e = cand["_eval"]
    h = cand["handle"]
    g = cand.get
    exists = c.execute("SELECT first_seen,quality_tier,verified,promoted_at,outreach,centrality "
                       "FROM creator_index WHERE handle=?", (h,)).fetchone()
    first_seen = exists[0] if exists else now
    # 保留已有的两层库/运营元数据 + 图中心性（重爬不覆盖晋升状态/已算中心性）
    q_tier = exists[1] if exists else 1
    verified = exists[2] if exists else 0
    promoted_at = exists[3] if exists else None
    outreach = exists[4] if exists else None
    centrality = exists[5] if exists else 0
    # 完整记录入 data_json（去掉 posts 和内部 _eval）
    full = {k: v for k, v in cand.items() if k not in ("posts", "_eval")}
    full["handle"] = h
    full["profile_url"] = g("profile_url") or f"https://instagram.com/{h}"
    data_json = json.dumps(full, ensure_ascii=False, default=str)

    # 52 列，按 schema 顺序逐一对应（全 ? 占位，避免错位）
    vals = (
        h, g("pk"), g("full_name"), g("biography"), full["profile_url"], g("category"),
        g("follower_count"), g("following_count"), g("media_count"), 1 if g("is_verified") else 0, g("tier"),
        str(g("has_amazon_storefront")), g("bio_link_type"), g("bio_link_url"),
        g("niche_primary_label"), "/".join(g("niche_secondary") or []), g("campaign_fit_label"),
        g("creator_archetype"), g("product_rec_ratio"), g("skincare_authority_score"),
        ", ".join(g("authority_evidence") or []),
        g("sponsored_count"), g("sponsored_ratio"), g("organic_amazon_posts_count"),
        g("reels_count"), g("reels_avg_likes"), g("reels_avg_comments"), g("reels_engagement_rate"),
        g("static_count"), g("static_avg_likes"), g("static_avg_comments"), g("static_engagement_rate"),
        e["engagement"], 1 if g("meets_er_benchmark") else 0,
        g("comments_analyzed"), g("purchase_intent_ratio"), g("bot_comment_ratio"),
        g("engagement_pod_ratio"), g("low_quality_ratio"), g("trust_level"),
        e["status"], e["score"], centrality,
        hop, via, first_seen, now,
        q_tier, verified, promoted_at, outreach,
        data_json,
    )
    c.execute(f"INSERT OR REPLACE INTO creator_index VALUES ({','.join('?' * len(vals))})", vals)
    if _HAS_FTS:
        c.execute("DELETE FROM creator_fts WHERE handle=?", (h,))
        c.execute("INSERT INTO creator_fts(handle,bio,full_name) VALUES(?,?,?)",
                  (h, g("biography") or "", g("full_name") or ""))


def add_edges(c: sqlite3.Connection, src: str, dsts: list[str], kind: str) -> None:
    for d in dsts:
        if d and d != src:
            c.execute("INSERT OR IGNORE INTO graph_edges VALUES(?,?,?)", (src, d, kind))


# ─── 种子前沿 ──────────────────────────────────────────────────────────────────

def seed_frontier(pool: Any, seeds: dict, brand_set: set) -> list[tuple[str, int, str]]:
    front: dict[str, dict] = {}
    brands = seeds.get("brands", {}).get("handles", [])
    disc = seeds.get("discovery", {})

    # 品牌被 tag 的帖 → 作者
    for b in brands:
        try:
            u = pool.user_info_by_username_v1(b)
            pk = str(as_plain(u).get("pk") or "")
            tagged = pool.usertag_medias_v1(pk, amount=30)
            for m in tagged:
                au = (as_plain(m).get("user") or {})
                h = (au.get("username") or "").lower()
                if h and h not in brand_set:
                    _add_candidate(front, h, f"brand_tagged:{b}")
            _log(f"  种子·品牌 @{b}: 累计 {len(front)}")
        except Exception as exc:
            _log(f"  种子·品牌 @{b} 失败: {type(exc).__name__}")
        time.sleep(3)

    # 关键词搜索
    for q in disc.get("keyword_search_queries", []):
        try:
            for u in (pool.search_users(q) or []):
                h = (as_plain(u).get("username") or "").lower()
                if h and h not in brand_set:
                    _add_candidate(front, h, f"search:{q}")
        except Exception as exc:
            _log(f"  种子·搜索 '{q}' 失败: {type(exc).__name__}")
        time.sleep(3)

    # 显式 lookalike 种子
    for s in disc.get("lookalike_seeds", []):
        _add_candidate(front, s.lower().lstrip("@"), "lookalike_seed")

    _log(f"  种子前沿: {len(front)} 个")
    return [(h, 0, (c.get("discovery_sources") or ["seed"])[0]) for h, c in front.items()]


# ─── ingest：多跳 snowball 建索引+图 ─────────────────────────────────────────────

def cmd_ingest(args) -> None:
    seeds = load_seeds_toml()
    cfg = {
        "min_followers": seeds.get("filters", {}).get("min_followers", 10000),
        "max_followers": seeds.get("filters", {}).get("max_followers", 150000),
        "brand_handles": seeds.get("brands", {}).get("handles", []),
        "engagement_benchmarks": seeds.get("filters", {}).get("engagement_benchmarks", {}),
    }
    brand_set = {b.lower() for b in cfg["brand_handles"]}
    pool = AccountPool(load_pool_accounts(), rotate_every=args.rotate_every,
                       cooldown_minutes=args.cooldown, wait_on_exhaust=args.wait_pool)
    pool.warm_up()
    c = _conn()

    _log(f"═══ ingest: hops={args.hops} budget={args.budget} ttl={args.ttl}d ═══")
    seed_list = seed_frontier(pool, seeds, brand_set)

    # BFS 队列（带 hop + 来源 + 父 fit 优先级）
    queue: deque = deque((h, hop, via, 1) for h, hop, via in seed_list)
    crawled = 0
    while queue and crawled < args.budget:
        # 优先级：对口父节点的分支优先（把队列里高优先的提前）
        queue = deque(sorted(queue, key=lambda x: -x[3]))
        handle, hop, via, _prio = queue.popleft()
        handle = handle.lower().lstrip("@")
        row = c.execute("SELECT last_crawled,fit FROM creator_index WHERE handle=?", (handle,)).fetchone()
        if row and _is_fresh(row[0], args.ttl):
            # 已新鲜：不重爬，但仍可用其已存边继续扩散（图已有）
            continue
        try:
            profile = fetch_user_info(pool, handle)
            if profile.get("is_private"):
                continue
            posts = fetch_user_medias(pool, profile["pk"], args.candidate_posts)
            cand = evaluate(profile, posts, cfg)
            cand["handle"] = handle   # fetch_user_info 返回 username，统一用查询用的 handle 作主键
            upsert(c, cand, hop, via)
            crawled += 1
            fit = cand.get("campaign_fit", "")
            e = cand["_eval"]
            _log(f"  [{crawled}/{args.budget}] h{hop} @{handle:24s} "
                 f"{cand.get('niche_primary_label','?'):6s} {cand.get('campaign_fit_label','?'):5s} "
                 f"amazon={cand.get('has_amazon_storefront')} {e['status']} s={e['score']}")
            # 扩散：从对口/相关节点取相似账号作为下一跳（跨垂类不再扩散，省预算）
            if hop < args.hops and fit in ("core", "related"):
                try:
                    sug = pool.fbsearch_suggested_profiles(profile["pk"])
                    handles = [(as_plain(s).get("username") or "").lower() for s in (sug or [])]
                    handles = [h for h in handles if h and h not in brand_set]
                    add_edges(c, handle, handles, "suggested")
                    prio = 3 if fit == "core" else 2
                    for h2 in handles:
                        queue.append((h2, hop + 1, f"lookalike:{handle}", prio))
                    _log(f"      ↳ suggested {len(handles)} → 入队 hop{hop+1}")
                except Exception as exc:
                    _log(f"      suggested 失败: {type(exc).__name__}")
            if crawled % 10 == 0:
                c.commit()
            time.sleep(args.sleep)
        except PoolExhausted as exc:
            _log(f"⚠ 账号池耗尽，停止 ingest: {exc}")
            break
        except Exception as exc:
            _log(f"  @{handle} 失败: {type(exc).__name__}: {str(exc)[:50]}")
            continue

    c.commit()
    _rank(c)
    st = pool.stats()
    tot = c.execute("SELECT COUNT(*) FROM creator_index").fetchone()[0]
    edg = c.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    _log(f"═══ ingest 完成：本次爬 {crawled} | 索引总计 {tot} 创作者 | 图 {edg} 条边 ═══")
    _log(f"  账号池: {st['total_requests']} 请求 / {st['rotations']} 轮换 / 可用 {st['available']}/{st['total_accounts']}")
    c.close()


# ─── 图中心性 ──────────────────────────────────────────────────────────────────

def _rank(c: sqlite3.Connection) -> None:
    import networkx as nx
    edges = c.execute("SELECT src,dst FROM graph_edges").fetchall()
    if not edges:
        _log("  图为空，跳过中心性"); return
    G = nx.DiGraph()
    G.add_edges_from(edges)
    try:
        cen = nx.pagerank(G, alpha=0.85)
    except Exception:
        cen = nx.degree_centrality(G)
    mx = max(cen.values()) if cen else 1
    for node, val in cen.items():
        c.execute("UPDATE creator_index SET centrality=? WHERE handle=?",
                  (round(val / mx * 100, 2), node))
    c.commit()
    _log(f"  图中心性已更新：{G.number_of_nodes()} 节点 / {G.number_of_edges()} 边")


def cmd_rank(args) -> None:
    c = _conn(); _rank(c); c.close()


# ─── search：查索引 + 图中心性排序 ───────────────────────────────────────────────

def cmd_search(args) -> None:
    c = _conn()
    where, params = ["status IN ('include','review')"], []
    if args.min_followers:
        where.append("followers >= ?"); params.append(args.min_followers)
    if args.max_followers:
        where.append("followers <= ?"); params.append(args.max_followers)
    if args.fit:
        where.append("fit = ?"); params.append(args.fit)
    if args.amazon:
        where.append("has_amazon IN ('True','unverified')")
    handles_filter = ""
    if args.bio:
        if _HAS_FTS:
            hs = [r[0] for r in c.execute("SELECT handle FROM creator_fts WHERE creator_fts MATCH ?",
                                          (args.bio,)).fetchall()]
            handles_filter = " AND handle IN (%s)" % (",".join("?" * len(hs)) or "''")
            params_extra = hs
        else:
            where.append("bio LIKE ?"); params.append(f"%{args.bio}%"); params_extra = []
    else:
        params_extra = []
    q = (f"SELECT handle,full_name,followers,niche,fit,has_amazon,engagement,score,centrality,status "
         f"FROM creator_index WHERE {' AND '.join(where)}{handles_filter} "
         f"ORDER BY (centrality*{args.w_central} + score*{args.w_score}) DESC LIMIT {args.limit}")
    rows = c.execute(q, params + params_extra).fetchall()
    print(f"{'handle':24s}{'粉丝':>9s} {'赛道':7s}{'对口':5s}{'amazon':11s}{'ER%':>6s}{'评分':>6s}{'中心度':>7s} {'状态'}")
    for r in rows:
        print(f"@{r[0]:23s}{r[2] or 0:>9,} {r[3] or '':7s}{r[4] or '':5s}{str(r[5]):11s}"
              f"{r[6] or 0:>6}{r[7] or 0:>6}{r[8] or 0:>7} {r[9]}")
    print(f"\n{len(rows)} 个（查本地索引，零账号消耗）")
    c.close()


def cmd_stats(args) -> None:
    c = _conn()
    tot = c.execute("SELECT COUNT(*) FROM creator_index").fetchone()[0]
    edg = c.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    print(f"索引创作者: {tot} | 图边: {edg} | FTS: {'on' if _HAS_FTS else 'off(LIKE降级)'}")
    print("状态分布:", dict(c.execute("SELECT status,COUNT(*) FROM creator_index GROUP BY status").fetchall()))
    print("对口分布:", dict(c.execute("SELECT fit,COUNT(*) FROM creator_index WHERE status IN ('include','review') GROUP BY fit").fetchall()))
    print("按 hop:", dict(c.execute("SELECT hop,COUNT(*) FROM creator_index GROUP BY hop").fetchall()))
    print("\n中心度 Top 10（社区影响力）:")
    for r in c.execute("SELECT handle,niche,fit,followers,centrality,score FROM creator_index "
                       "WHERE status IN ('include','review') ORDER BY centrality DESC LIMIT 10"):
        print(f"  @{r[0]:24s}{r[1] or '':7s}{r[2] or '':5s}{r[3] or 0:>9,}  中心度{r[4]:>6}  评分{r[5]}")
    c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="复合发现引擎（索引+图+账号池）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="多跳爬取建索引+图（用账号池）")
    pi.add_argument("--hops", type=int, default=2)
    pi.add_argument("--budget", type=int, default=200, help="本次最多爬多少 profile")
    pi.add_argument("--ttl", type=int, default=14, help="新鲜度天数，内不重爬")
    pi.add_argument("--candidate-posts", type=int, default=12)
    pi.add_argument("--rotate-every", type=int, default=10)
    pi.add_argument("--cooldown", type=int, default=30)
    pi.add_argument("--sleep", type=int, default=4)
    pi.add_argument("--wait-pool", action="store_true", default=False)
    ps = sub.add_parser("search", help="查索引 + 中心性排序（不碰账号）")
    ps.add_argument("--bio", type=str, help="bio 关键词（FTS）")
    ps.add_argument("--fit", type=str, help="对口/相关/跨垂类")
    ps.add_argument("--amazon", action="store_true", help="仅有 Amazon 橱窗")
    ps.add_argument("--min-followers", type=int, default=0)
    ps.add_argument("--max-followers", type=int, default=0)
    ps.add_argument("--limit", type=int, default=30)
    ps.add_argument("--w-central", type=float, default=0.5)
    ps.add_argument("--w-score", type=float, default=0.5)
    sub.add_parser("rank", help="重算图中心性")
    sub.add_parser("stats", help="索引/图统计")
    args = ap.parse_args()
    {"ingest": cmd_ingest, "search": cmd_search, "rank": cmd_rank, "stats": cmd_stats}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
