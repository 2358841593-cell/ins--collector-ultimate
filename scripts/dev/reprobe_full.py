#!/usr/bin/env python3
"""二次体检：对首轮判死的号，用【完整 cookie 串 + 设备身份(ig_did/mid)】重注入再打私有 API。
首轮只注了 sessionid + 随机设备，可能把好号误判为 login_required/challenge。这里排除假阴性。
读 data/session/cookie_report.json 的非活号，cookie 从 21 号备份里取。
"""
from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS = ROOT / ".secrets"
SESSION_DIR = ROOT / "data" / "session"
REPORT = SESSION_DIR / "cookie_report.json"
GAP = 9


def cookie_dict(cookie: str) -> dict:
    d = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def load_all_accounts() -> dict:
    # 取最新的 21 号全量备份
    baks = sorted(glob.glob(str(SECRETS / "account_pool.json.bak-*")))
    src = baks[-1] if baks else str(SECRETS / "account_pool.json")
    accs = json.loads(Path(src).read_text(encoding="utf-8"))["accounts"]
    return {a["username"]: a for a in accs}


def reprobe(acc: dict) -> dict:
    from instagrapi import Client

    user = acc["username"]
    cookie = acc.get("cookie_string", "")
    cd = cookie_dict(cookie)
    sid = cd.get("sessionid", "")
    if not sid:
        return {"username": user, "alive": False, "detail": "无 sessionid"}
    uid = sid.split("%3A")[0]

    cl = Client()
    # 完整 cookie 注入 + 设备身份对齐
    cl.settings["cookies"] = cd
    if cd.get("ig_did"):
        try:
            cl.uuid = cd["ig_did"].strip("{}").lower()
        except Exception:
            pass
    if cd.get("mid"):
        try:
            cl.mid = cd["mid"]
        except Exception:
            pass
    cl.init()
    cl.authorization_data = {"ds_user_id": uid, "sessionid": sid, "should_use_header_over_cookies": True}
    # 把 web cookie 也塞进 private/public jar
    for k, v in cd.items():
        for jar in (getattr(cl, "private", None), getattr(cl, "public", None)):
            try:
                jar.cookies.set(k, v)
            except Exception:
                pass
    try:
        u = cl.user_info_v1(int(uid))
        sf = SESSION_DIR / f"instagrapi-{user}.json"
        try:
            cl.dump_settings(str(sf)); os.chmod(sf, 0o600)
        except Exception:
            pass
        return {"username": user, "alive": True, "detail": f"@{u.username} pk={u.pk}"}
    except Exception as exc:
        n = type(exc).__name__
        low = (n + str(exc)).lower()
        bucket = "CHALLENGE" if ("challenge" in low or n.startswith("Challenge")) else (
            "DEAD_COOKIE" if "login_required" in low else "OTHER:" + n)
        return {"username": user, "alive": False, "detail": f"{bucket} | {n}: {str(exc)[:55]}"}


def main() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    failed = [r["username"] for r in report if not r["alive"]]
    allacc = load_all_accounts()
    targets = [allacc[u] for u in failed if u in allacc and allacc[u].get("cookie_string")]
    print(f"二次体检 {len(targets)} 个首轮判死号（完整 cookie + 设备身份重注入，间隔 {GAP}s）\n", flush=True)
    flipped = []
    results = []
    for i, acc in enumerate(targets):
        print(f"[{i+1:2d}/{len(targets)}] {acc['username']:20s} ... ", end="", flush=True)
        r = reprobe(acc)
        results.append(r)
        print(("✅ 复活 " if r["alive"] else "❌ ") + r["detail"], flush=True)
        if r["alive"]:
            flipped.append(r["username"])
        if i < len(targets) - 1:
            time.sleep(GAP)
    (SESSION_DIR / "reprobe_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n──────── 汇总 ────────")
    print(f"  本轮复活: {len(flipped)}  {', '.join(flipped) or '—'}")
    print(f"  仍判死: {len(targets)-len(flipped)}")


if __name__ == "__main__":
    main()
