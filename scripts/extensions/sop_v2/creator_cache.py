"""创作者浅扫缓存库（数据资产化）。

把 instaloader 浅扫拿到的 profile 字段（粉丝/外链/商业号/类目/赛道/storefront）
持久化到本机 SQLite。之后先查库、命中新鲜数据就复用，不再重扫 IG——**越用越快、
越省号、风险越低**（TWO_TIER_DESIGN 飞轮的落地）。

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
            "rejected_reason TEXT", "source_batch TEXT"]
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_brand ON creator_profiles(brand_account_type)",
    "CREATE INDEX IF NOT EXISTS idx_niche ON creator_profiles(core_niche_key)",
    "CREATE INDEX IF NOT EXISTS idx_storefront ON creator_profiles(storefront_status)",
    "CREATE INDEX IF NOT EXISTS idx_tier ON creator_profiles(tier)",
    "CREATE INDEX IF NOT EXISTS idx_cstatus ON creator_profiles(client_status)",
]


def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)                    # 建表（含全列，新库直接就位）
    have = {r["name"] for r in c.execute("PRAGMA table_info(creator_profiles)")}
    for col in _MIGRATE:                        # 旧库迁移：缺列则补
        if col.split()[0] not in have:
            c.execute(f"ALTER TABLE creator_profiles ADD COLUMN {col}")
    for idx in _INDEXES:                        # 列齐后再建索引
        c.execute(idx)
    return c


def upsert(cand: dict):
    """写入/更新一个创作者的浅扫数据。"""
    h = (cand.get("handle") or "").lstrip("@")
    if not h:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    vals = {f: cand.get(f) for f in SHALLOW_FIELDS}
    for b in ("is_business", "is_private", "is_verified"):
        vals[b] = 1 if vals.get(b) else (0 if vals.get(b) is not None else None)
    with _conn() as c:
        row = c.execute("SELECT times_seen, first_seen FROM creator_profiles WHERE handle=?", (h,)).fetchone()
        first_seen = row["first_seen"] if row else now
        times = (row["times_seen"] + 1) if row else 1
        cols = ["handle"] + SHALLOW_FIELDS + ["first_seen", "last_scanned", "times_seen", "data_json"]
        placeholders = ",".join("?" * len(cols))
        data = [h] + [vals[f] for f in SHALLOW_FIELDS] + [first_seen, now, times,
                                                          json.dumps(vals, ensure_ascii=False)]
        c.execute(f"INSERT OR REPLACE INTO creator_profiles ({','.join(cols)}) VALUES ({placeholders})", data)


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
