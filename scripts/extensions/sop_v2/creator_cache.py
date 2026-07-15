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
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_brand ON creator_profiles(brand_account_type);
CREATE INDEX IF NOT EXISTS idx_niche ON creator_profiles(core_niche_key);
CREATE INDEX IF NOT EXISTS idx_storefront ON creator_profiles(storefront_status);
"""


def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
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


def stats() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM creator_profiles").fetchone()["n"]
        brands = c.execute("SELECT COUNT(*) n FROM creator_profiles WHERE brand_account_type='brand'").fetchone()["n"]
        storefronts = c.execute("SELECT COUNT(*) n FROM creator_profiles WHERE storefront_status='confirmed_yes'").fetchone()["n"]
    return {"total": total, "brands": brands, "with_storefront": storefronts}


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
