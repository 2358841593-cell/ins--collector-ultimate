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
import hashlib
import sqlite3
import time
import uuid
from datetime import datetime
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
CREATE TABLE IF NOT EXISTS client_feedback_imports (
    file_sha256 TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    decision_count INTEGER NOT NULL,
    approved_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    adopted_existing INTEGER NOT NULL DEFAULT 0
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


def _new_lock_token():
    """时间前缀供 stale 比较，随机后缀供 compare-and-swap 区分 owner。"""
    return (
        datetime.now().astimezone().isoformat(timespec="microseconds")
        + "-"
        + uuid.uuid4().hex[:8]
    )


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
                stale_minutes: int | None = None) -> list[dict]:
    """软锁认领：取 status=from_status 且（未锁 或 锁已陈旧=上次挂了）的行，置 locked_at=now，
    返回候选 dict（从 stage_json 还原）。``stale_minutes=None`` 时只认领未锁行；
    只有显式 resume 才允许接管陈旧锁。选择和加锁在 BEGIN IMMEDIATE 中原子完成。"""
    out = []
    # 陈旧锁判定用 Python 本地时间算 cutoff（locked_at 也是本地 ISO），
    # 不用 SQLite 的 datetime('now')（那是 UTC，会和本地时区错位、断点续跑失效）。
    cutoff = (
        time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - int(stale_minutes) * 60),
        )
        if stale_minutes is not None
        else None
    )
    with _conn() as c:
        # _conn 可能刚完成 schema/index 维护；先落盘，再拿写锁，避免两个 worker
        # 都 SELECT 到同一批后互相覆盖 locked_at。
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        lock_clause = (
            "(locked_at IS NULL OR locked_at < ?)"
            if cutoff is not None
            else "locked_at IS NULL"
        )
        q = (
            "SELECT handle, stage_json FROM creator_profiles WHERE status=? "
            f"AND {lock_clause}"
        )
        params = [from_status]
        if cutoff is not None:
            params.append(cutoff)
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
        claimed_at = _new_lock_token()
        for r in rows:
            where_lock = (
                "(locked_at IS NULL OR locked_at < ?)"
                if cutoff is not None
                else "locked_at IS NULL"
            )
            update_params = [claimed_at, r["handle"], from_status]
            if cutoff is not None:
                update_params.append(cutoff)
            changed = c.execute(
                "UPDATE creator_profiles SET locked_at=? "
                "WHERE handle=? AND status=? "
                f"AND {where_lock}",
                update_params,
            ).rowcount
            if changed != 1:
                continue
            cand = json.loads(r["stage_json"]) if r["stage_json"] else {}
            cand["handle"] = r["handle"]
            # 仅供 worker 内存中的长队列续租；_base 在业务处理前会 pop，绝不入 stage_json。
            cand["_queue_lock_token"] = claimed_at
            cand["_queue_from_status"] = from_status
            out.append(cand)
    return out


def refresh_queue_locks(cands: list[dict]) -> tuple[list[dict], list[dict]]:
    """续租长队列的剩余软锁，并丢弃已被其他 worker 接管的项。

    每个候选必须携带 ``claim_queue`` 注入的内存 token；更新使用 compare-and-swap，
    不会替别的 worker 续租。返回 ``(kept, lost)``，并原地更新 kept token。
    """
    if not cands:
        return [], []
    kept, lost = [], []
    renewed_at = _new_lock_token()
    with _conn() as c:
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        for cand in cands:
            handle = (cand.get("handle") or "").lstrip("@")
            status = cand.get("_queue_from_status")
            token = cand.get("_queue_lock_token")
            if not handle or not status or not token:
                lost.append(cand)
                continue
            changed = c.execute(
                "UPDATE creator_profiles SET locked_at=? "
                "WHERE handle=? AND status=? AND locked_at=?",
                (renewed_at, handle, status, token),
            ).rowcount
            if changed == 1:
                cand["_queue_lock_token"] = renewed_at
                kept.append(cand)
            else:
                lost.append(cand)
    return kept, lost


def release_queue_locks(cands: list[dict]) -> int:
    """只释放仍由这些内存 token 所有的锁；用于 Ctrl-C/启动失败清理。"""
    released = 0
    if not cands:
        return released
    with _conn() as c:
        for cand in cands:
            handle = (cand.get("handle") or "").lstrip("@")
            token = cand.get("_queue_lock_token")
            if not handle or not token:
                continue
            released += c.execute(
                "UPDATE creator_profiles SET locked_at=NULL "
                "WHERE handle=? AND locked_at=?",
                (handle, token),
            ).rowcount
    return released


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
        sets = ["status='rejected'", "reject_reason=?", "stage_updated_at=?",
                "locked_at=NULL", "stage_error=NULL"]
        params = [reason, _now_iso()]
        if cand is not None:
            s2, p2 = _stage_json_and_hot(cand)
            sets += s2
            params += p2
        params.append(h)
        c.execute(f"UPDATE creator_profiles SET {','.join(sets)} WHERE handle=?", params)


def requeue_machine_rejections(handles, expected_reason: str, batch_ids=None) -> dict:
    """因业务规则纠偏，把精确的机器淘汰集合事务性退回 ``seed`` 重判。

    仅允许处理 ``status=rejected`` 且 reject_reason 与调用方声明完全一致的记录；客户已作
    最终 approved/rejected/collaborated 的记录禁止改动。保留 stage_json 的已采 profile
    数据，但清理旧决策/错误/锁。任一目标不满足前置条件则整批失败，不做部分更新。
    """
    hs = sorted({(h or "").strip().lstrip("@").lower() for h in handles if (h or "").strip()})
    if not hs:
        return {"requeued": 0, "handle_set_sha256": hashlib.sha256(b"").hexdigest()}
    allowed_batches = set(batch_ids or [])
    with _conn() as c:
        rows = c.execute(
            "SELECT handle,status,reject_reason,client_status,discovery_batch,stage_json "
            f"FROM creator_profiles WHERE lower(handle) IN ({','.join('?' * len(hs))})",
            hs,
        ).fetchall()
        by_handle = {r["handle"].lower(): r for r in rows}
        missing = [h for h in hs if h not in by_handle]
        invalid = []
        for h in hs:
            r = by_handle.get(h)
            if not r:
                continue
            if r["status"] != "rejected" or r["reject_reason"] != expected_reason:
                invalid.append(f"{h}:status={r['status']},reason={r['reject_reason']}")
            elif allowed_batches and r["discovery_batch"] not in allowed_batches:
                invalid.append(f"{h}:batch={r['discovery_batch']}")
            elif r["client_status"] in ("approved", "rejected", "collaborated"):
                invalid.append(f"{h}:client_status={r['client_status']}")
        if missing or invalid:
            raise ValueError(
                "semantic requeue precondition failed; "
                f"missing={missing[:5]} invalid={invalid[:5]}"
            )
        now = _now_iso()
        for h in hs:
            r = by_handle[h]
            try:
                sj = json.loads(r["stage_json"]) if r["stage_json"] else {}
            except Exception:  # noqa: BLE001
                sj = {}
            for key in ("final_pool", "_status", "_reject_reason"):
                sj.pop(key, None)
            c.execute(
                "UPDATE creator_profiles SET status='seed', reject_reason=NULL, stage_error=NULL, "
                "locked_at=NULL, final_pool=NULL, stage_updated_at=?, stage_json=? WHERE lower(handle)=?",
                (now, json.dumps(sj, ensure_ascii=False, default=str), h),
            )
    digest = hashlib.sha256("\n".join(hs).encode()).hexdigest()
    return {"requeued": len(hs), "handle_set_sha256": digest}


def mark_error(
    handle: str,
    err: str,
    cand: dict | None = None,
    *,
    expected_status: str | None = None,
    lock_token: str | None = None,
) -> bool:
    """记录可重试错误，并可原子保存本次部分证据。

    ``status`` 始终不变。worker 传入认领时的 status/token 后，更新使用 CAS，不能
    覆盖已被其他 worker 接管的行；客户已终判的资产也不会被本次采集覆盖。若 CAS
    失败但锁仍属于本 worker，只释放自己的锁，避免留下孤儿锁。
    """
    h = (handle or "").lstrip("@")
    with _conn() as c:
        sets = ["stage_error=?", "locked_at=NULL"]
        params = [str(err)[:120]]
        if cand is not None:
            persisted = dict(cand)
            # claim 元数据只存在于 worker 内存，绝不能写入业务证据。
            persisted.pop("_queue_lock_token", None)
            persisted.pop("_queue_from_status", None)
            persisted.setdefault("handle", h)
            s2, p2 = _stage_json_and_hot(persisted)
            sets += s2
            params += p2

        where = [
            "handle=?",
            "(client_status IS NULL OR client_status NOT IN "
            "('approved','rejected','collaborated'))",
        ]
        params.append(h)
        if expected_status is not None:
            where.append("status=?")
            params.append(expected_status)
        if lock_token is not None:
            where.append("locked_at=?")
            params.append(lock_token)
        changed = c.execute(
            f"UPDATE creator_profiles SET {','.join(sets)} "
            f"WHERE {' AND '.join(where)}",
            params,
        ).rowcount
        if changed != 1 and lock_token is not None:
            # 终判或状态并发变化时不写证据/错误，但仍可安全释放自己持有的锁。
            c.execute(
                "UPDATE creator_profiles SET locked_at=NULL "
                "WHERE handle=? AND locked_at=?",
                (h, lock_token),
            )
        return changed == 1


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
    每个 cand 塞入 _status/_stage_error/_reject_reason/_discovery_batch，供 stage4
    按当前流水线状态与错误判断归池，并标注来源池与淘汰原因。
    stage_json 缺失的历史号（早 reject 未回写）用热列兜底最小 cand（handle + 已存字段 + 淘汰原因），
    至少以 handle+原因 出现在 Exclude 池——落实"抓过就要体现，不 silently drop"。"""
    cols = ("follower_count", "storefront_status", "amazon_storefront_link", "real_er",
            "brand_account_type", "core_niche_key", "full_name", "biography", "final_pool")
    with _conn() as c:
        # discovery_batch NOT NULL：排除 user_version<1 的历史终态回填（非本流水线"抓过"的候选）
        q = ("SELECT handle, status, stage_error, reject_reason, discovery_batch, stage_json, "
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
        cand["_stage_error"] = r["stage_error"]
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


def strict_deep_reasons(
    cand: dict,
    *,
    status: str | None = None,
    target_posts: int = 10,
    require_full_deep: bool = False,
) -> list[str]:
    """Return strict, evidence-based deep-collection failures.

    A genuine low-comment account is complete when every sampled post exposes an
    observed ``comment_count`` and all of those counts are zero.  Zero extracted
    comments is normally a technical failure.  A bounded exception is recorded
    explicitly in ``comment_unavailable_posts`` only after the same URL fails in
    two rounds while reporting 1–2 comments; it counts as reviewed, never as zero.

    Machine-rejected candidates remain exempt by default for backward
    compatibility.  ``require_full_deep`` makes the same contract apply to them.
    """
    status = status or cand.get("_status") or cand.get("status")
    if status == "rejected" and not require_full_deep:
        return []

    target = max(1, int(target_posts))
    reasons = []
    contract_fields = (
        "deep_target_posts",
        "deep_available_posts",
        "deep_successful_posts",
        "deep_failed_posts",
        "deep_metric_missing_posts",
        "comment_attempted_posts",
        "comment_completed_posts",
        "comment_failed_posts",
        "deep_collection_status",
    )
    has_explicit_contract = all(field in cand for field in contract_fields)
    if has_explicit_contract:
        def _count_or_len(value):
            if isinstance(value, (list, tuple, set, dict)):
                return len(value)
            return int(value)

        try:
            recorded_target = max(
                1, _count_or_len(cand.get("deep_target_posts"))
            )
            available = max(
                0, _count_or_len(cand.get("deep_available_posts"))
            )
            successful = max(
                0, _count_or_len(cand.get("deep_successful_posts"))
            )
            failed = max(
                0, _count_or_len(cand.get("deep_failed_posts"))
            )
            metric_missing = max(
                0, _count_or_len(cand.get("deep_metric_missing_posts"))
            )
            comment_completed = max(
                0, _count_or_len(cand.get("comment_completed_posts"))
            )
            comment_failed = max(
                0, _count_or_len(cand.get("comment_failed_posts"))
            )
        except (TypeError, ValueError):
            reasons.append("深采完整性元数据无效")
        else:
            required_target = max(target, recorded_target)
            expected = min(required_target, available)
            if cand.get("deep_collection_status") != "complete":
                reasons.append(
                    "深采状态未完成"
                    f"(status={cand.get('deep_collection_status') or 'missing'})"
                )
            if successful < expected:
                reasons.append(
                    f"帖子覆盖不足({successful}/{expected}；目标 {required_target})"
                )
            if failed:
                reasons.append(f"帖子读取失败({failed} 帖)")
            if metric_missing:
                reasons.append(f"帖子互动指标缺失({metric_missing} 帖)")
            if comment_failed:
                reasons.append(f"评论读取失败({comment_failed} 帖)")
            if (
                cand.get("deep_collection_status") == "complete"
                and comment_completed < successful
            ):
                reasons.append(
                    f"评论完成计数不一致({comment_completed}/{successful})"
                )
    else:
        # 旧记录稍后用结构化证据回推；证据足够时允许继续使用，避免为了补 contract
        # 字段无差别重刷全部历史账号。
        pass

    if cand.get("comments_read") is not True:
        reasons.append("深采未完成(comments_read 未确认)")

    raw_posts = cand.get("sampled_posts")
    posts = [post for post in (raw_posts or []) if isinstance(post, dict)]
    if not has_explicit_contract:
        try:
            analyzed_count = int(cand.get("comments_analyzed") or 0)
            valid_count = int(cand.get("valid_comments") or 0)
        except (TypeError, ValueError):
            analyzed_count = valid_count = 0
        has_observed_metric = any(
            post.get("like_count") is not None
            or post.get("comment_count") is not None
            for post in posts
        )
        legacy_evidence_complete = (
            len(posts) >= target
            and analyzed_count > 0
            and valid_count >= 20
            and cand.get("real_er") is not None
            and has_observed_metric
        )
        if not legacy_evidence_complete:
            reasons.append("旧版深采证据不足(需复采建立完整性契约)")
            if analyzed_count > 0 and valid_count < 20:
                reasons.append(f"旧版有效评论不足({valid_count}/20)")
            if cand.get("real_er") is None:
                reasons.append("旧版实算 ER 缺失")
    if not has_explicit_contract and len(posts) < target:
        reasons.append(f"帖子覆盖不足({len(posts)}/{target})")
    if not posts:
        return reasons

    observed_likes = [
        post.get("like_count") for post in posts
        if post.get("like_count") is not None
    ]
    observed_comments = [
        post.get("comment_count") for post in posts
        if post.get("comment_count") is not None
    ]
    if not observed_likes and not observed_comments:
        reasons.append("帖子互动指标全缺失(like/comment 均为 null)")

    analyzed = cand.get("comments_analyzed")
    if not has_explicit_contract and not (analyzed or 0):
        all_comments_observed = len(observed_comments) == len(posts)
        genuine_zero_comments = (
            all_comments_observed
            and all(float(value) == 0 for value in observed_comments)
        )
        if not genuine_zero_comments:
            if any(float(value) > 0 for value in observed_comments):
                reasons.append(
                    f"评论抽取失败(帖子可见评论但抽取 0；"
                    f"{sum(float(value) > 0 for value in observed_comments)} 帖有评论)"
                )
            else:
                reasons.append("评论为 0 且无法证明账号真实低互动")

    if cand.get("real_er") is None and observed_likes:
        reasons.append("采到赞数却算不出 ER")
    return list(dict.fromkeys(reasons))


def incomplete_items(
    batch_id: str | None = None,
    *,
    strict: bool = False,
    target_posts: int = 10,
    require_full_deep: bool = False,
) -> list[dict]:
    """采集完整性审计：找出"跑过但没真拿到东西"的号——代理抖动/限流/抽取失败导致的静默不完整。

    只报**确定异常**，不误报真·低互动小号（本就没评论的号不算失败）：
      - 网格有帖子 code 却 0 帖采到 → 帖子页导航全失败（抖动）
      - 帖子里有评论(comment_count≥5)却一条没抽到 → 评论抽取失败
      - 采到帖且有赞数却算不出 ER → ER 派生异常
      - stage_error 非空 → 已记录的失败（含 deep_all_posts_failed/logged_out 等）
    返回 [{handle, status, stage_error, reasons[]}]，供 audit_collect 报表 + --requeue 回头补采。"""
    with _conn() as c:
        # 括号必须有：AND 比 OR 结合更紧，漏了会让 batch 过滤对 status 条件失效（跨批次误报）
        statuses = ["qualified", "collected", "decided"]
        if require_full_deep:
            statuses.append("rejected")
        q = (
            "SELECT handle, status, stage_error, stage_json FROM creator_profiles "
            f"WHERE (status IN ({','.join('?' * len(statuses))}) "
            "OR stage_error IS NOT NULL)"
        )
        params = list(statuses)
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
        if strict or require_full_deep:
            reasons.extend(
                strict_deep_reasons(
                    sj,
                    status=r["status"],
                    target_posts=target_posts,
                    require_full_deep=require_full_deep,
                )
            )
            reasons = list(dict.fromkeys(reasons))
        if reasons:
            out.append({"handle": r["handle"], "status": r["status"],
                        "stage_error": r["stage_error"], "reasons": reasons})
    return out


# 重采时要清掉的深采派生字段（保留浅扫/Modash 数据，只重跑深采部分）
_DEEP_FIELDS = ("comments_read", "sampled_posts", "comments_analyzed", "valid_comments",
                "low_quality_ratio", "high_intent_ratio", "intent_posts", "intent_by_grade",
                "high_intent_count", "intent_total", "high_intent_snippets", "promo_intent_hits",
                "promotional_post_count", "real_er", "real_er_median", "real_er_window",
                "comment_shots", "evidence_dir", "comment_sample", "deep_target_posts",
                "deep_available_posts", "deep_successful_posts", "deep_failed_posts",
                "deep_metric_missing_posts", "comment_attempted_posts",
                "comment_completed_posts", "comment_failed_posts",
                "comment_unavailable_posts", "deep_collection_status")


def requeue_for_recollect(handles) -> int:
    """把不完整/失败的号退回 qualified 等待重采（"不完整再出来"）：清深采派生字段、
    保留浅扫+Modash 数据、清 locked_at 让 claim_queue 能重新认领。显式 requeue
    同时清掉 comment_failed 的轮次历史与 comment_unavailable 终态，从首轮重新计数。
    返回处理数。"""
    n = 0
    normalized = [
        str(handle or "").strip().lstrip("@")
        for handle in handles
        if str(handle or "").strip()
    ]
    with _conn() as c:
        if normalized:
            rows = c.execute(
                "SELECT handle,client_status FROM creator_profiles "
                f"WHERE handle IN ({','.join('?' * len(normalized))})",
                normalized,
            ).fetchall()
            client_final = sorted(
                row["handle"]
                for row in rows
                if row["client_status"] in ("approved", "rejected", "collaborated")
            )
            if client_final:
                raise ValueError(
                    f"客户终判资产禁止退回深采：{client_final[:10]}"
                )
        for h in normalized:
            r = c.execute(
                "SELECT stage_json FROM creator_profiles WHERE handle=?", (h,)
            ).fetchone()
            if not r:
                continue
            try:
                sj = json.loads(r["stage_json"]) if r["stage_json"] else {}
            except Exception:  # noqa: BLE001
                sj = {}
            for k in _DEEP_FIELDS:
                sj.pop(k, None)
            sj.pop("final_pool", None)
            c.execute(
                "UPDATE creator_profiles SET status='qualified', stage_json=?, "
                "real_er=NULL, high_intent_count=NULL, final_pool=NULL, "
                "reject_reason=NULL, locked_at=NULL WHERE handle=?",
                (json.dumps(sj, ensure_ascii=False), h),
            )
            n += 1
    return n


def validate_recollect_scope(handles) -> set[str]:
    """Validate an exact deep-recollection allowlist before any state change.

    Every handle must exist and customer-final assets are forbidden.  The
    normalized set is returned for callers to use as an exact intersection.
    """
    normalized = {
        str(handle or "").strip().lstrip("@").lower()
        for handle in handles
        if str(handle or "").strip()
    }
    if not normalized:
        raise ValueError("重采白名单为空")
    with _conn() as c:
        rows = c.execute(
            "SELECT lower(handle) AS handle,client_status FROM creator_profiles "
            f"WHERE lower(handle) IN ({','.join('?' * len(normalized))})",
            sorted(normalized),
        ).fetchall()
    by_handle = {row["handle"]: row for row in rows}
    missing = sorted(normalized - set(by_handle))
    client_final = sorted(
        handle
        for handle, row in by_handle.items()
        if row["client_status"] in ("approved", "rejected", "collaborated")
    )
    if missing or client_final:
        raise ValueError(
            "重采白名单校验失败；"
            f"missing={missing[:10]} client_final={client_final[:10]}"
        )
    return normalized


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
        c.execute("""UPDATE creator_profiles SET tier=2, client_status=?, approved_at=?,
                     rejected_reason=NULL, source_batch=?
                     WHERE handle=?""", (st, now, batch_id, h))


def mark_rejected(handle: str, reason: str = "", batch_id: str = ""):
    """客户 Herman Approval=No → 负向库。不自动改门槛/config。"""
    h = (handle or "").lstrip("@")
    with _conn() as c:
        c.execute("""UPDATE creator_profiles SET client_status='rejected', rejected_reason=?,
                     approved_at=NULL, tier=CASE WHEN tier=2 THEN 1 ELSE tier END, source_batch=?
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
    c = _conn()
    try:
        rows = c.execute(
            "SELECT handle FROM creator_profiles "
            "WHERE tier=2 AND client_status IN ('approved','collaborated') "
            "ORDER BY approved_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        c.close()
    return [r["handle"] for r in rows]


class ClientDecisionImportError(ValueError):
    """客户决策不能被安全、原子地导入。"""


def _valid_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _client_note_after_transition(existing, label: str | None, reason: str) -> str | None:
    """Replace stale decision-tag notes while preserving unrelated operator notes."""
    if label and reason:
        return f"[{label}] {reason}"[:500]
    if isinstance(existing, str) and existing.startswith(
        ("[合适] ", "[不合适] ", "[待定] ")
    ):
        return None
    return existing


def apply_client_decisions(
    decisions: list[dict],
    *,
    batch_id: str,
    file_sha256: str,
    source_sha256: str,
    adopt_existing: bool = False,
    allow_status_change: bool = False,
    dry_run: bool = False,
) -> dict:
    """严格校验后在单个事务中落客户反馈，并用原文件 SHA-256 保证幂等。

    ``decisions`` 必须已经由交付文件校验器归一化为
    ``{handle, action: approved|rejected|pending, reason}``。本层仍会防御性检查，
    并在同一事务中核对账号存在、``discovery_batch``、历史客户状态、写业务字段和
    ledger，避免逐行成功后半途失败。

    ``adopt_existing`` 只用于接管旧导入器已经写过、但没有 ledger 的批次：它要求
    现有业务状态与文件逐条一致，只修复批准/拒绝互斥字段并登记 ledger，不刷新
    ``approved_at``。

    已有客户终判与新文件冲突时默认整批中止。只有客户明确改判时，调用方才可显式
    传 ``allow_status_change=True``；它不能与 ``adopt_existing`` 同时使用。
    """
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ClientDecisionImportError("batch_id 不能为空")
    batch_id = batch_id.strip()
    if not _valid_sha256(file_sha256) or not _valid_sha256(source_sha256):
        raise ClientDecisionImportError("file/source SHA-256 格式无效")
    if not isinstance(decisions, list) or not decisions:
        raise ClientDecisionImportError("没有可导入的客户决策")
    if adopt_existing and allow_status_change:
        raise ClientDecisionImportError(
            "adopt_existing 与 allow_status_change 不能同时启用"
        )

    normalized = []
    seen = set()
    counts = {"approved": 0, "rejected": 0, "pending": 0}
    for index, item in enumerate(decisions, 1):
        if not isinstance(item, dict):
            raise ClientDecisionImportError(f"第 {index} 条决策不是对象")
        raw_handle = item.get("handle")
        if not isinstance(raw_handle, str):
            raise ClientDecisionImportError(f"第 {index} 条 handle 不是字符串")
        handle = raw_handle.strip().lstrip("@").strip()
        key = handle.lower()
        if not handle:
            raise ClientDecisionImportError(f"第 {index} 条 handle 为空")
        if key in seen:
            raise ClientDecisionImportError(f"反馈中存在重复账号：@{handle}")
        seen.add(key)
        action = item.get("action")
        if action not in counts:
            raise ClientDecisionImportError(f"@{handle} 的 action 无效")
        origin_batch = item.get("origin_batch", batch_id)
        if not isinstance(origin_batch, str) or not origin_batch.strip():
            raise ClientDecisionImportError(f"@{handle} 的 origin_batch 无效")
        origin_batch = origin_batch.strip()
        reason = item.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise ClientDecisionImportError(f"@{handle} 的 reason 不是字符串")
        normalized.append({"handle": handle, "key": key, "action": action,
                           "reason": reason.strip(), "origin_batch": origin_batch})
        counts[action] += 1

    c = _conn()
    try:
        # _conn 可能刚完成旧库迁移；先提交迁移，再显式开启本次唯一业务事务。
        c.commit()
        c.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")

        ledger = c.execute(
            "SELECT * FROM client_feedback_imports WHERE file_sha256=?",
            (file_sha256,),
        ).fetchone()
        if ledger:
            if ledger["batch_id"] != batch_id:
                raise ClientDecisionImportError("同一文件 SHA 的 ledger 批次不一致")
            if ledger["source_sha256"] != source_sha256:
                raise ClientDecisionImportError("同一文件 SHA 的交付基线 SHA 不一致")
            result = {
                "batch_id": ledger["batch_id"],
                "decision_count": ledger["decision_count"],
                "approved": ledger["approved_count"],
                "rejected": ledger["rejected_count"],
                "pending": ledger["pending_count"],
                "already_imported": True,
                "adopted_existing": bool(ledger["adopted_existing"]),
                "dry_run": dry_run,
            }
            c.rollback()
            return result

        rows_by_key = {}
        for item in normalized:
            rows = c.execute(
                """SELECT handle, discovery_batch, tier, client_status, approved_at,
                          rejected_reason, source_batch, client_note
                   FROM creator_profiles WHERE handle=? COLLATE NOCASE""",
                (item["handle"],),
            ).fetchall()
            if not rows:
                raise ClientDecisionImportError(f"数据库不存在账号：@{item['handle']}")
            if len(rows) != 1:
                raise ClientDecisionImportError(f"数据库存在大小写重复账号：@{item['handle']}")
            row = rows[0]
            if row["discovery_batch"] != item["origin_batch"]:
                raise ClientDecisionImportError(
                    f"@{item['handle']} 的 discovery_batch 不是来源批次 "
                    f"{item['origin_batch']}"
                )
            rows_by_key[item["key"]] = row

        if adopt_existing:
            inconsistent = []
            for item in normalized:
                row = rows_by_key[item["key"]]
                action, reason = item["action"], item["reason"]
                if action == "approved":
                    ok = (
                        row["client_status"] == "approved"
                        and row["source_batch"] == batch_id
                        and row["tier"] == 2
                        and bool(row["approved_at"])
                        and (
                            not reason
                            or row["client_note"] == f"[合适] {reason}"[:500]
                        )
                    )
                elif action == "rejected":
                    expected = reason or "客户判定不合适"
                    ok = (
                        row["client_status"] == "rejected"
                        and row["source_batch"] == batch_id
                        and row["rejected_reason"] == expected
                    )
                else:
                    ok = (
                        row["client_status"] is None
                        and (
                            not reason
                            or row["client_note"] == f"[待定] {reason}"[:500]
                        )
                    )
                if not ok:
                    inconsistent.append(item["handle"])
            if inconsistent:
                shown = "、".join(f"@{h}" for h in inconsistent[:5])
                raise ClientDecisionImportError(
                    f"--adopt-existing 核对失败，现有状态与反馈不一致：{shown}"
                )
        else:
            # 没有 ledger 却已经呈现相同业务状态，通常表示旧脚本写过。默认中止，
            # 防止把一次历史写入误认作本次新导入。
            already_present = []
            status_conflicts = []
            batch_has_ledger = bool(
                c.execute(
                    "SELECT 1 FROM client_feedback_imports WHERE batch_id=? LIMIT 1",
                    (batch_id,),
                ).fetchone()
            )
            for item in normalized:
                row = rows_by_key[item["key"]]
                current = row["client_status"]
                wanted = item["action"]
                if (
                    (
                        wanted == "approved"
                        and current in ("approved", "collaborated")
                    )
                    or (wanted == "rejected" and current == "rejected")
                    or (
                        wanted == "pending"
                        and item["reason"]
                        and current is None
                        and row["client_note"] == f"[待定] {item['reason']}"[:500]
                    )
                ):
                    already_present.append(item["handle"])
                elif current is not None:
                    status_conflicts.append(item["handle"])
            if status_conflicts and not allow_status_change:
                shown = "、".join(f"@{h}" for h in status_conflicts[:5])
                raise ClientDecisionImportError(
                    "客户终判与数据库已有状态冲突；如客户明确改判，需显式使用 "
                    f"--allow-status-change：{shown}"
                )
            if already_present and not batch_has_ledger:
                shown = "、".join(f"@{h}" for h in already_present[:5])
                raise ClientDecisionImportError(
                    "发现无 ledger 但已有相同客户状态；请核实后使用 "
                    f"--adopt-existing：{shown}"
                )

        if dry_run:
            c.rollback()
            return {
                "batch_id": batch_id,
                "decision_count": len(normalized),
                **counts,
                "already_imported": False,
                "adopted_existing": adopt_existing,
                "dry_run": True,
            }

        now = _now_iso()
        if adopt_existing:
            for item in normalized:
                handle = rows_by_key[item["key"]]["handle"]
                if item["action"] == "approved":
                    c.execute(
                        "UPDATE creator_profiles SET rejected_reason=NULL WHERE handle=?",
                        (handle,),
                    )
                elif item["action"] == "rejected":
                    c.execute(
                        """UPDATE creator_profiles SET approved_at=NULL,
                           tier=CASE WHEN tier=2 THEN 1 ELSE tier END WHERE handle=?""",
                        (handle,),
                    )
        else:
            for item in normalized:
                row = rows_by_key[item["key"]]
                handle = row["handle"]
                action, reason = item["action"], item["reason"]
                if action == "approved":
                    if row["client_status"] in ("approved", "collaborated"):
                        # 新一轮/增量 JSON 常会重复带上已选行。保留首次批准时间和
                        # collaborated 的更强状态，只登记本轮来源并清互斥字段。
                        c.execute(
                            """UPDATE creator_profiles SET tier=2,
                               rejected_reason=NULL, source_batch=? WHERE handle=?""",
                            (batch_id, handle),
                        )
                    else:
                        note = _client_note_after_transition(
                            row["client_note"], "合适", reason
                        )
                        c.execute(
                            """UPDATE creator_profiles SET tier=2, client_status='approved',
                               approved_at=?, rejected_reason=NULL, source_batch=?,
                               client_note=?
                               WHERE handle=?""",
                            (now, batch_id, note, handle),
                        )
                    if reason and row["client_status"] in ("approved", "collaborated"):
                        c.execute(
                            "UPDATE creator_profiles SET client_note=? WHERE handle=?",
                            (f"[合适] {reason}"[:500], handle),
                        )
                elif action == "rejected":
                    if row["client_status"] == "rejected":
                        c.execute(
                            """UPDATE creator_profiles SET rejected_reason=?,
                               approved_at=NULL,
                               tier=CASE WHEN tier=2 THEN 1 ELSE tier END,
                               source_batch=? WHERE handle=?""",
                            (reason or "客户判定不合适", batch_id, handle),
                        )
                    else:
                        note = _client_note_after_transition(
                            row["client_note"], None, reason
                        )
                        c.execute(
                            """UPDATE creator_profiles SET client_status='rejected',
                               rejected_reason=?, approved_at=NULL,
                               tier=CASE WHEN tier=2 THEN 1 ELSE tier END,
                               source_batch=?, client_note=? WHERE handle=?""",
                            (reason or "客户判定不合适", batch_id, note, handle),
                        )
                elif row["client_status"] is not None:
                    # 只有显式 --allow-status-change 才可能走到这里：客户把旧终判
                    # 改回待定，必须真正清掉终判字段，不能只写一条待定备注。
                    c.execute(
                        """UPDATE creator_profiles SET client_status=NULL,
                           approved_at=NULL, rejected_reason=NULL,
                           tier=CASE WHEN tier=2 THEN 1 ELSE tier END,
                           source_batch=?, client_note=? WHERE handle=?""",
                        (
                            batch_id,
                            _client_note_after_transition(
                                row["client_note"], "待定", reason
                            ),
                            handle,
                        ),
                    )
                elif reason:
                    c.execute(
                        "UPDATE creator_profiles SET client_note=? WHERE handle=?",
                        (f"[待定] {reason}"[:500], handle),
                    )

        c.execute(
            """INSERT INTO client_feedback_imports
               (file_sha256, batch_id, source_sha256, imported_at, decision_count,
                approved_count, rejected_count, pending_count, adopted_existing)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                file_sha256,
                batch_id,
                source_sha256,
                now,
                len(normalized),
                counts["approved"],
                counts["rejected"],
                counts["pending"],
                int(adopt_existing),
            ),
        )
        c.commit()
        return {
            "batch_id": batch_id,
            "decision_count": len(normalized),
            **counts,
            "already_imported": False,
            "adopted_existing": adopt_existing,
            "dry_run": False,
        }
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


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
