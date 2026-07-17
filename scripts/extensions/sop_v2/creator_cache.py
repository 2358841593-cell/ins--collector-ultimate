"""创作者浅扫缓存库 + 四阶段流水线状态机（数据资产化）。

把**浏览器浅扫**（fetch_profile_browser，零 API）拿到的 profile 字段（粉丝/外链/商业号/
类目/赛道/storefront）持久化到本机 SQLite，并承载流水线 status 逐级交接（PIPELINE_SPEC.md）。
命中新鲜数据就复用，不再重扫 IG——**越用越快、越省号、风险越低**（飞轮的落地）。

- 浅扫字段（cache_get 命中即用）：品牌号判定、赛道、粉丝档、外链/storefront —— 这些
  变化慢，30 天缓存足够，可承担"区分品牌号/是否符合要求"的初筛。
- 库不进公开仓库（data/ 已被 .gitignore 隔离）；只存创作者公开 profile 字段，无凭证。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[2].parent / "data" / "creator_cache.db"

SHALLOW_FIELDS = [
    "full_name", "follower_count", "media_count", "external_url", "is_business",
    "category", "brand_account_type", "core_niche_key", "storefront_status",
    "amazon_storefront_link", "is_private", "is_verified", "biography",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS creator_profiles (
    handle TEXT PRIMARY KEY,
    full_name TEXT, follower_count INTEGER, media_count INTEGER,
    external_url TEXT, is_business INTEGER, category TEXT, brand_account_type TEXT,
    core_niche_key TEXT, storefront_status TEXT, amazon_storefront_link TEXT,
    is_private INTEGER, is_verified INTEGER, biography TEXT,
    first_seen TEXT, last_scanned TEXT, times_seen INTEGER DEFAULT 1,
    data_json TEXT,
    tier INTEGER DEFAULT 0,            -- 0 浅扫 / 1 候选 / 2 金种子
    client_status TEXT,               -- approved / rejected / collaborated / null(pending)
    approved_at TEXT, rejected_reason TEXT, source_batch TEXT
);
"""

_MIGRATE = ["tier INTEGER DEFAULT 0", "client_status TEXT", "approved_at TEXT",
            "rejected_reason TEXT", "source_batch TEXT",
            # ── 四阶段流水线（PIPELINE_SPEC.md 冻结）：全部 nullable，无 DEFAULT ──
            "status TEXT", "stage_updated_at TEXT", "locked_at TEXT", "stage_error TEXT",
            "reject_reason TEXT", "discovery_batch TEXT", "seed_followers INTEGER",
            "modash_er REAL", "real_er REAL", "high_intent_count INTEGER",
            "final_pool TEXT", "evidence_dir TEXT", "stage_json TEXT", "client_note TEXT"]
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_brand ON creator_profiles(brand_account_type)",
    "CREATE INDEX IF NOT EXISTS idx_niche ON creator_profiles(core_niche_key)",
    "CREATE INDEX IF NOT EXISTS idx_storefront ON creator_profiles(storefront_status)",
    "CREATE INDEX IF NOT EXISTS idx_tier ON creator_profiles(tier)",
    "CREATE INDEX IF NOT EXISTS idx_cstatus ON creator_profiles(client_status)",
    "CREATE INDEX IF NOT EXISTS idx_status ON creator_profiles(status)",
    "CREATE INDEX IF NOT EXISTS idx_status_batch ON creator_profiles(status, discovery_batch)",
]


def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")        # 并发写不互锁
    c.execute("PRAGMA busy_timeout=5000")
    c.executescript(_SCHEMA)                    # 建表（含全列，新库直接就位）
    have = {r["name"] for r in c.execute("PRAGMA table_info(creator_profiles)")}
    for col in _MIGRATE:                        # 旧库迁移：缺列则补
        if col.split()[0] not in have:
            c.execute(f"ALTER TABLE creator_profiles ADD COLUMN {col}")
    for idx in _INDEXES:                        # 列齐后再建索引
        c.execute(idx)
    # 一次性回填（user_version 守卫，绝不每次 _conn 重跑）：迁移前的历史行置 decided 终态，
    # 不进新 Amazon 导购流水线；金种子 tier/client_status 正交，不碰。
    if c.execute("PRAGMA user_version").fetchone()[0] < 1:
        c.execute("UPDATE creator_profiles SET status='decided', "
                  "stage_updated_at=COALESCE(last_scanned, first_seen) WHERE status IS NULL")
        c.execute("PRAGMA user_version=1")
    return c


def upsert(cand: dict):
    """浅扫回写。ON CONFLICT 只更新 SHALLOW_FIELDS + last_scanned + times_seen+1，
    **绝不触碰** status/tier/client_status/stage_json（防清零阶段进度、防降级金种子）。
    None 值不覆盖已有非空（COALESCE）。"""
    h = (cand.get("handle") or "").lstrip("@")
    if not h:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    vals = {f: cand.get(f) for f in SHALLOW_FIELDS}
    for b in ("is_business", "is_private", "is_verified"):
        vals[b] = 1 if vals.get(b) else (0 if vals.get(b) is not None else None)
    cols = ["handle"] + SHALLOW_FIELDS + ["first_seen", "last_scanned", "times_seen", "data_json"]
    ins = [h] + [vals[f] for f in SHALLOW_FIELDS] + [now, now, 1, json.dumps(vals, ensure_ascii=False)]
    set_parts = [f"{f}=COALESCE(excluded.{f}, creator_profiles.{f})" for f in SHALLOW_FIELDS]
    set_parts += ["last_scanned=excluded.last_scanned",
                  "times_seen=creator_profiles.times_seen+1", "data_json=excluded.data_json"]
    with _conn() as c:
        c.execute(f"INSERT INTO creator_profiles ({','.join(cols)}) VALUES ({','.join('?'*len(cols))}) "
                  f"ON CONFLICT(handle) DO UPDATE SET {', '.join(set_parts)}", ins)


def get(handle: str, max_age_days: int = 30) -> dict | None:
    """命中且新鲜（last_scanned 在 max_age_days 内）则返回浅扫字段，否则 None。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        row = c.execute("SELECT * FROM creator_profiles WHERE handle=?", (h,)).fetchone()
    if not row:
        return None
    try:
        age = time.time() - time.mktime(time.strptime(row["last_scanned"], "%Y-%m-%dT%H:%M:%S"))
        if age > max_age_days * 86400:
            return None
    except (ValueError, TypeError):
        return None
    d = {f: row[f] for f in SHALLOW_FIELDS}
    for b in ("is_business", "is_private", "is_verified"):
        d[b] = bool(row[b]) if row[b] is not None else None
    d["_cache_hit"] = True
    return d


def ingest_batch(candidates_path: str) -> int:
    """把一次采集产出的候选浅扫字段批量写入缓存。"""
    cands = json.loads(Path(candidates_path).read_text())
    n = 0
    for c in cands:
        if c.get("collect_failed"):
            continue
        upsert(c)
        n += 1
    return n


# ── 四阶段流水线 API（PIPELINE_SPEC.md §5 冻结；其余模块只调这些，不写裸 SQL）──────
def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _to_int(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    mult = 1
    if s and s[-1] in "Kk":
        mult, s = 1000, s[:-1]
    elif s and s[-1] in "Mm":
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").strip())
    except ValueError:
        return None


def _ensure_row(c, h):
    c.execute("INSERT OR IGNORE INTO creator_profiles (handle, first_seen, last_scanned, times_seen) "
              "VALUES (?,?,?,1)", (h, _now_iso(), _now_iso()))


def should_ingest_seed(handle: str) -> bool:
    """discovery 去重：客户拒绝 / 机器淘汰 / 已在库 → 不再造 seed。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        r = c.execute("SELECT status, client_status FROM creator_profiles WHERE handle=?", (h,)).fetchone()
    if not r:
        return True
    return False  # 已存在（含 rejected/decided/在途）→ 不重复造 seed


def seed_handles(recs: list[dict], batch_id: str = "") -> dict:
    """① discovery：写种子 status=seed。recs=[{handle,followers,er}]。去重 + 多源加权。"""
    new = deduped = skipped = 0
    for rec in recs:
        h = (rec.get("handle") or "").lstrip("@")
        if not h:
            continue
        if not should_ingest_seed(h):
            with _conn() as c:
                r = c.execute("SELECT status FROM creator_profiles WHERE handle=?", (h,)).fetchone()
                if r and r["status"] == "rejected":
                    skipped += 1
                else:  # 已在库 → times_seen+1（多源加权），不改 status
                    c.execute("UPDATE creator_profiles SET times_seen=times_seen+1 WHERE handle=?", (h,))
                    deduped += 1
            continue
        fol, er = _to_int(rec.get("followers")), _to_float(rec.get("er"))
        cand = {"handle": h, "seed_followers": fol, "modash_er": er,
                "discovery_batch": batch_id, "discovered_via": "modash_search"}
        with _conn() as c:
            _ensure_row(c, h)
            c.execute("UPDATE creator_profiles SET status='seed', stage_updated_at=?, discovery_batch=?, "
                      "seed_followers=?, modash_er=?, stage_json=?, locked_at=NULL WHERE handle=?",
                      (_now_iso(), batch_id, fol, er, json.dumps(cand, ensure_ascii=False), h))
        new += 1
    return {"new_seeds": new, "deduped": deduped, "rejected_skipped": skipped}


def claim_queue(from_status: str, limit: int = 0, batch_id: str | None = None,
                stale_minutes: int = 30) -> list[dict]:
    """软锁认领：取 status=from_status 且（未锁 或 锁已陈旧=上次挂了）的行，置 locked_at=now，
    返回候选 dict（从 stage_json 还原）。断点续跑核心：已 advance 的 status 已变，不会被重取。"""
    out = []
    # 陈旧锁判定用 Python 本地时间算 cutoff（locked_at 也是本地 ISO），
    # 不用 SQLite 的 datetime('now')（那是 UTC，会和本地时区错位、断点续跑失效）。
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - int(stale_minutes) * 60))
    with _conn() as c:
        q = ("SELECT handle, stage_json FROM creator_profiles WHERE status=? "
             "AND (locked_at IS NULL OR locked_at < ?)")
        params = [from_status, cutoff]
        if batch_id:
            q += " AND discovery_batch=?"
            params.append(batch_id)
        # 确认有 Amazon 橱窗的(Include 候选)优先深采；再多源命中优先；再先发现先处理
        q += (" ORDER BY (storefront_status='confirmed_yes') DESC, "
              "times_seen DESC, stage_updated_at ASC")
        if limit:
            q += " LIMIT ?"
            params.append(limit)
        rows = c.execute(q, params).fetchall()
        for r in rows:
            c.execute("UPDATE creator_profiles SET locked_at=? WHERE handle=?", (_now_iso(), r["handle"]))
            cand = json.loads(r["stage_json"]) if r["stage_json"] else {}
            cand["handle"] = r["handle"]
            out.append(cand)
    return out


# 热列（claim_queue 优先级/查询/看板靠它，不能只留 stage_json）——advance/reject 共用
_HOT_COLS = ("seed_followers", "modash_er", "real_er", "high_intent_count",
             "final_pool", "evidence_dir", "storefront_status", "amazon_storefront_link",
             "follower_count", "brand_account_type", "core_niche_key")


def _stage_json_and_hot(cand: dict):
    """把 cand 序列化成 stage_json + 抽出热列，返回 (sets, params) 片段。"""
    sets = ["stage_json=?"]
    params = [json.dumps(cand, ensure_ascii=False, default=str)]
    for col in _HOT_COLS:
        if cand.get(col) is not None:
            sets.append(f"{col}=?")
            params.append(cand.get(col))
    return sets, params


def advance(handle: str, to_status: str, cand: dict | None = None):
    """成功推进：白名单 UPDATE stage_json（候选累积）+ 热列 + status + 清 locked_at/stage_error。
    不碰 tier(除单调升 1)/client_status/rejected_reason（正交）。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        _ensure_row(c, h)
        sets = ["status=?", "stage_updated_at=?", "locked_at=NULL", "stage_error=NULL"]
        params = [to_status, _now_iso()]
        if cand is not None:
            s2, p2 = _stage_json_and_hot(cand)
            sets += s2
            params += p2
        params.append(h)
        c.execute(f"UPDATE creator_profiles SET {','.join(sets)} WHERE handle=?", params)
        if to_status in ("qualified", "collected", "decided"):
            c.execute("UPDATE creator_profiles SET tier=MAX(COALESCE(tier,0),1) WHERE handle=?", (h,))


def reject(handle: str, reason: str, cand: dict | None = None):
    """机器淘汰（候选不合格）：status=rejected + reject_reason（区别于客户侧 rejected_reason）。
    cand 给定则回写 stage_json + 热列——被淘汰号也留住已抓浅扫数据（客户铁律：抓过有数据必体现，
    进 Exclude 池仍要展示画像+淘汰原因，不再 silently drop）。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        _ensure_row(c, h)
        sets = ["status='rejected'", "reject_reason=?", "stage_updated_at=?", "locked_at=NULL"]
        params = [reason, _now_iso()]
        if cand is not None:
            s2, p2 = _stage_json_and_hot(cand)
            sets += s2
            params += p2
        params.append(h)
        c.execute(f"UPDATE creator_profiles SET {','.join(sets)} WHERE handle=?", params)


def mark_error(handle: str, err: str):
    """瞬时失败（号问题）：写 stage_error + 清 locked_at，**status 不动**（可重试，绝不烧号）。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        c.execute("UPDATE creator_profiles SET stage_error=?, locked_at=NULL WHERE handle=?",
                  (str(err)[:120], h))


def export_candidates(status: str, batch_id: str | None = None) -> list[dict]:
    """把某 status 的候选从 stage_json 还原成 run_v2 可吃的 dict 列表；顺带 ig_er←real_er 显示映射。"""
    with _conn() as c:
        q = "SELECT handle, stage_json FROM creator_profiles WHERE status=?"
        params = [status]
        if batch_id:
            q += " AND discovery_batch=?"
            params.append(batch_id)
        rows = c.execute(q, params).fetchall()
    out = []
    for r in rows:
        cand = json.loads(r["stage_json"]) if r["stage_json"] else {}
        cand.setdefault("handle", r["handle"])
        if cand.get("real_er") is not None:
            cand.setdefault("ig_er", cand.get("real_er"))
        out.append(cand)
    return out


# 客户铁律：抓过就纳入——非 seed（浅扫过及以上）都算"有数据"，全进交付各归其池
_DATA_STATUSES = ("qualified", "collected", "decided", "rejected")


def export_all_with_data(batch_ids=None) -> list[dict]:
    """汇总导出所有"抓过有数据"的候选（非 seed：qualified/collected/decided/rejected），跨 batch 可选。
    每个 cand 塞入 _status/_reject_reason/_discovery_batch，供 stage4 归池 + 交付标注来源池与淘汰原因。
    stage_json 缺失的历史号（早 reject 未回写）用热列兜底最小 cand（handle + 已存字段 + 淘汰原因），
    至少以 handle+原因 出现在 Exclude 池——落实"抓过就要体现，不 silently drop"。"""
    cols = ("follower_count", "storefront_status", "amazon_storefront_link", "real_er",
            "brand_account_type", "core_niche_key", "full_name", "biography", "final_pool")
    with _conn() as c:
        # discovery_batch NOT NULL：排除 user_version<1 的历史终态回填（非本流水线"抓过"的候选）
        q = ("SELECT handle, status, reject_reason, discovery_batch, stage_json, "
             + ", ".join(cols) + " FROM creator_profiles WHERE discovery_batch IS NOT NULL AND status IN (%s)"
             % ",".join("?" * len(_DATA_STATUSES)))
        params = list(_DATA_STATUSES)
        if batch_ids:
            q += " AND discovery_batch IN (%s)" % ",".join("?" * len(batch_ids))
            params += list(batch_ids)
        rows = c.execute(q, params).fetchall()
    out = []
    for r in rows:
        cand = json.loads(r["stage_json"]) if r["stage_json"] else {}
        cand.setdefault("handle", r["handle"])
        for col in cols:                       # 热列兜底：stage_json 空的历史号至少给出已存浅扫字段
            if cand.get(col) is None and r[col] is not None:
                cand[col] = r[col]
        if cand.get("real_er") is not None:
            cand.setdefault("ig_er", cand.get("real_er"))
        cand["_status"] = r["status"]
        cand["_reject_reason"] = r["reject_reason"]
        cand["_discovery_batch"] = r["discovery_batch"]
        out.append(cand)
    return out


def status_dist(batch_id: str | None = None) -> dict:
    """看板：各 status 计数。"""
    with _conn() as c:
        q = "SELECT status, COUNT(*) n FROM creator_profiles"
        params = []
        if batch_id:
            q += " WHERE discovery_batch=?"
            params.append(batch_id)
        q += " GROUP BY status"
        rows = c.execute(q, params).fetchall()
    return {(r["status"] or "null"): r["n"] for r in rows}


def failed_items(batch_id: str | None = None) -> list[dict]:
    """看板：stage_error 非空 或 status=rejected 的 handle+原因。"""
    with _conn() as c:
        q = ("SELECT handle, status, reject_reason, stage_error FROM creator_profiles "
             "WHERE stage_error IS NOT NULL OR status='rejected'")
        params = []
        if batch_id:
            q += " AND discovery_batch=?"
            params.append(batch_id)
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def incomplete_items(batch_id: str | None = None) -> list[dict]:
    """采集完整性审计：找出"跑过但没真拿到东西"的号——代理抖动/限流/抽取失败导致的静默不完整。

    只报**确定异常**，不误报真·低互动小号（本就没评论的号不算失败）：
      - 网格有帖子 code 却 0 帖采到 → 帖子页导航全失败（抖动）
      - 帖子里有评论(comment_count≥5)却一条没抽到 → 评论抽取失败
      - 采到帖且有赞数却算不出 ER → ER 派生异常
      - stage_error 非空 → 已记录的失败（含 deep_all_posts_failed/logged_out 等）
    返回 [{handle, status, stage_error, reasons[]}]，供 audit_collect 报表 + --requeue 回头补采。"""
    with _conn() as c:
        # 括号必须有：AND 比 OR 结合更紧，漏了会让 batch 过滤对 status 条件失效（跨批次误报）
        q = ("SELECT handle, status, stage_error, stage_json FROM creator_profiles "
             "WHERE (status IN ('qualified','collected','decided') OR stage_error IS NOT NULL)")
        params = []
        if batch_id:
            q += " AND discovery_batch=?"
            params.append(batch_id)
        rows = c.execute(q, params).fetchall()
    out = []
    for r in rows:
        try:
            sj = json.loads(r["stage_json"]) if r["stage_json"] else {}
        except Exception:  # noqa: BLE001
            sj = {}
        reasons = []
        if r["stage_error"]:
            reasons.append(f"错误:{r['stage_error']}")
        if r["status"] in ("collected", "decided") and sj.get("comments_read"):
            posts = sj.get("sampled_posts") or []
            if sj.get("codes") and not posts:
                reasons.append(f"帖子全失败(网格 {len(sj['codes'])} 帖，0 采到)")
            elif posts:
                withc = sum(1 for p in posts if (p.get("comment_count") or 0) >= 5)
                if withc and not (sj.get("comments_analyzed") or 0):
                    reasons.append(f"评论抽取失败({withc} 帖有评论却抽 0)")
                if sj.get("real_er") is None and any(p.get("like_count") is not None for p in posts):
                    reasons.append("采到赞数却算不出 ER")
        if reasons:
            out.append({"handle": r["handle"], "status": r["status"],
                        "stage_error": r["stage_error"], "reasons": reasons})
    return out


# 重采时要清掉的深采派生字段（保留浅扫/Modash 数据，只重跑深采部分）
_DEEP_FIELDS = ("comments_read", "sampled_posts", "comments_analyzed", "valid_comments",
                "low_quality_ratio", "high_intent_ratio", "intent_posts", "intent_by_grade",
                "high_intent_count", "intent_total", "high_intent_snippets", "promo_intent_hits",
                "promotional_post_count", "real_er", "real_er_median", "real_er_window",
                "comment_shots", "evidence_dir")


def requeue_for_recollect(handles) -> int:
    """把不完整/失败的号退回 qualified 等待重采（"不完整再出来"）：清深采派生字段、
    保留浅扫+Modash 数据、清 locked_at 让 claim_queue 能重新认领。返回处理数。"""
    n = 0
    with _conn() as c:
        for h in handles:
            h = (h or "").lstrip("@")
            r = c.execute("SELECT stage_json FROM creator_profiles WHERE handle=?", (h,)).fetchone()
            if not r:
                continue
            try:
                sj = json.loads(r["stage_json"]) if r["stage_json"] else {}
            except Exception:  # noqa: BLE001
                sj = {}
            for k in _DEEP_FIELDS:
                sj.pop(k, None)
            sj.pop("final_pool", None)
            c.execute("UPDATE creator_profiles SET status='qualified', stage_json=?, "
                      "real_er=NULL, high_intent_count=NULL, final_pool=NULL, locked_at=NULL "
                      "WHERE handle=?", (json.dumps(sj, ensure_ascii=False), h))
            n += 1
    return n


def set_tier(handle: str, tier: int):
    """决策后把完整候选标为 tier1（候选库）。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        c.execute("UPDATE creator_profiles SET tier=MAX(tier,?) WHERE handle=?", (tier, h))


# ── 反馈回流（人工门控进库，SOP 阶段8）──────────────────────────────
def promote_golden(handle: str, batch_id: str = "", collaborated: bool = False):
    """客户 Herman Approval=Yes/已合作 → 进金种子库(tier=2)。"""
    h = (handle or "").lstrip("@")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    st = "collaborated" if collaborated else "approved"
    with _conn() as c:
        c.execute("""UPDATE creator_profiles SET tier=2, client_status=?, approved_at=?, source_batch=?
                     WHERE handle=?""", (st, now, batch_id, h))


def mark_rejected(handle: str, reason: str = "", batch_id: str = ""):
    """客户 Herman Approval=No → 负向库。不自动改门槛/config。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        c.execute("""UPDATE creator_profiles SET client_status='rejected', rejected_reason=?, source_batch=?
                     WHERE handle=?""", (reason, batch_id, h))


def set_client_note(handle: str, note: str):
    """存客户在交付表里填的原因备注（正交，不改 status/门槛）。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        _ensure_row(c, h)
        c.execute("UPDATE creator_profiles SET client_note=? WHERE handle=?", (note[:500], h))


def is_rejected(handle: str) -> bool:
    """负向库命中 → 发现阶段不再重现。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        r = c.execute("SELECT client_status FROM creator_profiles WHERE handle=?", (h,)).fetchone()
    return bool(r and r["client_status"] == "rejected")


def golden_seeds(limit: int = 50) -> list[str]:
    """金种子库(tier=2) handle 列表，作下一轮 Modash Lookalike 种子（SOP 阶段1）。"""
    with _conn() as c:
        rows = c.execute("SELECT handle FROM creator_profiles WHERE tier=2 ORDER BY approved_at DESC LIMIT ?",
                         (limit,)).fetchall()
    return [r["handle"] for r in rows]


def import_feedback(feedback: list[dict], batch_id: str = "") -> dict:
    """从客户填好的交付表回导 Herman Approval/Feedback → 触发③④进库。
    feedback = [{handle, approval:'Yes'/'No'/'', feedback:'...'}]。"""
    promoted = rejected = 0
    for f in feedback:
        h = (f.get("handle") or "").lstrip("@")
        if not h:
            continue
        ap = str(f.get("approval") or "").strip().lower()
        if ap in ("yes", "y", "approved", "批准", "已合作", "collaborated"):
            promote_golden(h, batch_id, collaborated=("合作" in ap or "collab" in ap))
            promoted += 1
        elif ap in ("no", "n", "rejected", "拒绝"):
            mark_rejected(h, f.get("feedback", ""), batch_id)
            rejected += 1
    return {"promoted_to_golden": promoted, "marked_rejected": rejected}


def stats() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM creator_profiles").fetchone()["n"]
        brands = c.execute("SELECT COUNT(*) n FROM creator_profiles WHERE brand_account_type='brand'").fetchone()["n"]
        storefronts = c.execute("SELECT COUNT(*) n FROM creator_profiles WHERE storefront_status='confirmed_yes'").fetchone()["n"]
        golden = c.execute("SELECT COUNT(*) n FROM creator_profiles WHERE tier=2").fetchone()["n"]
        rejected = c.execute("SELECT COUNT(*) n FROM creator_profiles WHERE client_status='rejected'").fetchone()["n"]
    return {"total": total, "brands": brands, "with_storefront": storefronts,
            "golden_seeds": golden, "rejected": rejected}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "stats", "get"])
    ap.add_argument("arg", nargs="?")
    a = ap.parse_args()
    if a.cmd == "ingest":
        print(f"写入 {ingest_batch(a.arg)} 个创作者 → {DB}")
    elif a.cmd == "stats":
        print(stats())
    elif a.cmd == "get":
        print(json.dumps(get(a.arg), ensure_ascii=False, indent=2))
