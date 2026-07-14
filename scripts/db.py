#!/usr/bin/env python3
"""本地 SQLite 数据库：每日发现内容入库，随时查询/复核/跨 run 去重。

表：
  runs        每次运行的汇总
  candidates  每次运行的候选明细（run_id + handle，含完整 data_json）
  creators    每个创作者的最新快照 + 累计出现次数（跨 run 去重视图）
  pod_accounts 互赞团/水军账号库（镜像 pod_accounts.json）

用法：
  入库：   .venv/bin/python scripts/db.py ingest-latest
           .venv/bin/python scripts/db.py ingest data/runs/discovery-XXXX.json
  查创作者：.venv/bin/python scripts/db.py creator skincarewithtash
  列表：   .venv/bin/python scripts/db.py list --status review --fit core --min-score 30
  统计：   .venv/bin/python scripts/db.py stats
  查水军：  .venv/bin/python scripts/db.py pod mikkinucifora_
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "data" / "runs"
DB_PATH = ROOT / "data" / "discovery.db"
POD_FILE = ROOT / "data" / "pod_accounts.json"

sys.path.insert(0, str(ROOT / "scripts"))
from export_xlsx import verdict, amazon_state, load_verifications  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, ingested_at TEXT, timestamp TEXT,
  total INT, include INT, review INT, exclude INT, source TEXT);
CREATE TABLE IF NOT EXISTS candidates(
  run_id TEXT, handle TEXT, status TEXT, verdict TEXT, verdict_reason TEXT,
  niche TEXT, fit TEXT, followers INT, tier TEXT, amazon TEXT, archetype TEXT,
  product_rec REAL, authority INT, sponsored REAL, reels_er REAL, static_er REAL,
  trust TEXT, intent REAL, pod REAL, score REAL,
  profile_url TEXT, bio_url TEXT, data_json TEXT,
  PRIMARY KEY(run_id, handle));
CREATE TABLE IF NOT EXISTS creators(
  handle TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, times_seen INT,
  last_run TEXT, last_status TEXT, last_verdict TEXT, last_score REAL,
  last_niche TEXT, last_fit TEXT, last_followers INT, last_amazon TEXT);
CREATE TABLE IF NOT EXISTS pod_accounts(
  username TEXT PRIMARY KEY, hits INT, creators TEXT, sample TEXT,
  reasons TEXT, first_seen TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS idx_cand_handle ON candidates(handle);
CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);
"""


def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    return c


def ingest(json_path: str) -> None:
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    run_id = data.get("run_id", os.path.basename(json_path))
    verifs = load_verifications(run_id)
    now = datetime.now(timezone.utc).isoformat()
    s = data.get("summary", {})
    db = conn()
    db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?)",
               (run_id, now, data.get("timestamp", ""), s.get("total_candidates"),
                s.get("include"), s.get("review"), s.get("exclude"), os.path.basename(json_path)))
    n = 0
    for c in data.get("candidates", []):
        h = c["handle"]
        vrec = verifs.get(h, {})
        vt, vw = verdict(c, vrec)
        db.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (run_id, h, c.get("status"), vt, vw,
                    c.get("niche_primary_label"), c.get("campaign_fit_label"),
                    c.get("follower_count"), c.get("tier"), amazon_state(c, vrec),
                    c.get("creator_archetype"), c.get("product_rec_ratio"),
                    c.get("skincare_authority_score"), c.get("sponsored_ratio"),
                    c.get("reels_engagement_rate"), c.get("static_engagement_rate"),
                    c.get("trust_level"), c.get("purchase_intent_ratio"),
                    c.get("engagement_pod_ratio"), c.get("discovery_score"),
                    c.get("profile_url") or f"https://instagram.com/{h}", c.get("bio_link_url"),
                    json.dumps(c, ensure_ascii=False)))
        n += 1
    # 重建 creators 视图（跨 run 去重 + 最新快照）
    rows = db.execute("""
      SELECT handle, COUNT(DISTINCT run_id) AS seen,
             MIN(run_id) AS first_run, MAX(run_id) AS last_run
      FROM candidates GROUP BY handle""").fetchall()
    for handle, seen, first_run, last_run in rows:
        latest = db.execute("""SELECT run_id,status,verdict,score,niche,fit,followers,amazon
            FROM candidates WHERE handle=? ORDER BY run_id DESC LIMIT 1""", (handle,)).fetchone()
        db.execute("INSERT OR REPLACE INTO creators VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (handle, first_run, last_run, seen, latest[0], latest[1], latest[2],
                    latest[3], latest[4], latest[5], latest[6], latest[7]))
    # 同步水军库
    if POD_FILE.exists():
        pods = json.loads(POD_FILE.read_text(encoding="utf-8")).get("accounts", {})
        for u, info in pods.items():
            db.execute("INSERT OR REPLACE INTO pod_accounts VALUES(?,?,?,?,?,?,?)",
                       (u, info.get("hits"), ",".join(info.get("creators", [])),
                        info.get("sample"), ",".join(info.get("reasons", [])),
                        info.get("first_seen"), info.get("last_seen")))
    db.commit()
    db.close()
    print(f"✓ 入库 {run_id}: {n} 候选 · creators 视图已更新 · DB={DB_PATH}")


def cmd_creator(handle):
    db = conn()
    handle = handle.lstrip("@").lower()
    cr = db.execute("SELECT * FROM creators WHERE handle=?", (handle,)).fetchone()
    if not cr:
        print(f"未找到 @{handle}")
        return
    cols = [d[0] for d in db.execute("SELECT * FROM creators LIMIT 0").description]
    print(f"=== @{handle} ===")
    for k, v in zip(cols, cr):
        print(f"  {k}: {v}")
    print("\n历史出现:")
    for row in db.execute("""SELECT run_id,status,verdict,score,fit,followers
        FROM candidates WHERE handle=? ORDER BY run_id DESC""", (handle,)):
        print(f"  {row[0]} | {row[1]:8s} | {row[2]} | score={row[3]} | {row[4]} | {row[5]} 粉")
    db.close()


def cmd_list(args):
    db = conn()
    q = "SELECT handle,status,verdict,niche,fit,followers,score,amazon FROM candidates WHERE 1=1"
    p = []
    # 默认查最新 run
    last = db.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    if not args.all_runs and last:
        q += " AND run_id=?"; p.append(last)
    if args.status:
        q += " AND status=?"; p.append(args.status)
    if args.fit:
        q += " AND fit=?"; p.append(args.fit)
    if args.min_score:
        q += " AND score>=?"; p.append(args.min_score)
    q += " ORDER BY score DESC"
    rows = db.execute(q, p).fetchall()
    print(f"{'handle':24s}{'status':9s}{'verdict':11s}{'niche':8s}{'fit':7s}{'fans':>8s}{'score':>7s}  amazon")
    for r in rows:
        print(f"@{r[0]:23s}{r[1]:9s}{r[2] or '':11s}{r[3] or '':8s}{r[4] or '':7s}{r[5] or 0:>8}{r[6] or 0:>7}  {r[7]}")
    print(f"\n{len(rows)} 行" + (f"（run {last}）" if not args.all_runs else "（全部 run）"))
    db.close()


def cmd_stats():
    db = conn()
    print("=== 运行历史 ===")
    for r in db.execute("SELECT run_id,total,include,review,exclude FROM runs ORDER BY run_id DESC"):
        print(f"  {r[0]} | 候选{r[1]} | include {r[2]} / review {r[3]} / exclude {r[4]}")
    nc = db.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
    np = db.execute("SELECT COUNT(*) FROM pod_accounts").fetchone()[0]
    print(f"\n累计独立创作者: {nc} · 水军库: {np}")
    print("对口度分布(最新run):")
    last = db.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    for r in db.execute("SELECT fit,COUNT(*) FROM candidates WHERE run_id=? GROUP BY fit", (last,)):
        print(f"  {r[0]}: {r[1]}")
    db.close()


def cmd_pod(username):
    db = conn()
    r = db.execute("SELECT * FROM pod_accounts WHERE username=?", (username.lstrip("@").lower(),)).fetchone()
    if r:
        cols = [d[0] for d in db.execute("SELECT * FROM pod_accounts LIMIT 0").description]
        print("⚠ 在水军库中：")
        for k, v in zip(cols, r):
            print(f"  {k}: {v}")
    else:
        print(f"@{username} 不在水军库")
    db.close()


def main():
    ap = argparse.ArgumentParser(description="本地发现数据库")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ingest-latest")
    pi = sub.add_parser("ingest"); pi.add_argument("path")
    pc = sub.add_parser("creator"); pc.add_argument("handle")
    pl = sub.add_parser("list")
    pl.add_argument("--status"); pl.add_argument("--fit"); pl.add_argument("--min-score", type=float)
    pl.add_argument("--all-runs", action="store_true")
    sub.add_parser("stats")
    pp = sub.add_parser("pod"); pp.add_argument("username")
    args = ap.parse_args()

    if args.cmd == "ingest-latest":
        files = sorted(glob.glob(str(RUNS_DIR / "discovery-*.json")), key=os.path.getmtime)
        if not files:
            raise SystemExit("无 discovery-*.json")
        ingest(files[-1])
    elif args.cmd == "ingest":
        ingest(args.path)
    elif args.cmd == "creator":
        cmd_creator(args.handle)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "stats":
        cmd_stats()
    elif args.cmd == "pod":
        cmd_pod(args.username)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
