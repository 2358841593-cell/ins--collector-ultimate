"""Modash 补数（stage4 用）：把 Modash Profile Report 的假粉/受众/国家/ER 写回候选。

解除 routing 的 modash_core_missing（否则 Include 池恒空，PIPELINE_SPEC 坑B）。
两条通道：
  A. CSV 导入（推荐，稳且对齐客户既有 Modash 导出流程，零额外抓取）：
     客户在 Modash 对 shortlist 导出 Profile Report CSV → --modash-csv <path>。
  B. CDP 实时读（占位，需已登录 Chrome；未接入 DOM/网络抽取，避免消耗额度与误抓）。
补不到的候选诚实留 None → 下游按 modash_core_missing 落 Review（不伪造数据）。

映射到 gates/routing 读的字段名：fake_pct / creator_country / top_audience_country / general_er
（+ 受众 target_countries_audience_pct / top_language_pct 供 E 模块与 audience 分流）。
"""
from __future__ import annotations

import csv


def _find_col(col_map, candidates):
    for c in candidates:
        if c.lower() in col_map:
            return col_map[c.lower()]
    return None


def _f(v):
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def _country(v):
    return str(v).strip().upper()[:2] if v and str(v).strip() else None


def parse_modash_csv(path: str) -> dict:
    """解析 Modash Profile Report CSV → {handle: {gate 字段}}。列名宽松匹配。"""
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return out
        cm = {name.lower().strip(): name for name in reader.fieldnames}
        k_user = _find_col(cm, ["username", "handle", "instagram handle", "profile", "account"])
        k_fake = _find_col(cm, ["fake followers", "fake followers %", "suspicious followers", "fake %"])
        k_cred = _find_col(cm, ["credibility", "audience credibility", "credibility score"])
        k_er = _find_col(cm, ["engagement rate", "engagementrate", "er", "avg engagement rate"])
        k_cloc = _find_col(cm, ["creator location", "location", "creator country", "country"])
        k_aud = _find_col(cm, ["top audience country", "audience country", "audience geo", "top geo"])
        k_lang = _find_col(cm, ["top audience language", "audience language", "language %", "top language"])
        k_tgt = _find_col(cm, ["target country %", "target audience %", "audience in target"])
        if not k_user:
            return out
        for row in reader:
            h = (row.get(k_user) or "").strip().lstrip("@").lower()
            if not h:
                continue
            d: dict = {}
            # 假粉：优先直接列；否则 100*(1 - credibility)
            fake = _f(row.get(k_fake)) if k_fake else None
            if fake is None and k_cred:
                cred = _f(row.get(k_cred))
                if cred is not None:
                    fake = round((1 - cred) * 100, 1) if cred <= 1 else round(100 - cred, 1)
            if fake is not None:
                d["fake_pct"] = fake
            if k_er:
                d["general_er"] = _f(row.get(k_er))
            if k_cloc:
                d["creator_country"] = _country(row.get(k_cloc))
            if k_aud:
                d["top_audience_country"] = _country(row.get(k_aud))
            if k_lang:
                d["top_language_pct"] = _f(row.get(k_lang))
            if k_tgt:
                d["target_countries_audience_pct"] = _f(row.get(k_tgt))
            if d:
                out[h] = d
    return out


def enrich(cands: list[dict], csv_path: str) -> dict:
    """把 CSV 补数写回候选（按 handle 匹配）。None 不覆盖已有。返回统计。"""
    table = parse_modash_csv(csv_path)
    matched = 0
    for c in cands:
        h = (c.get("handle") or "").lstrip("@").lower()
        row = table.get(h)
        if not row:
            continue
        matched += 1
        for k, v in row.items():
            if v is not None and c.get(k) is None:
                c[k] = v
    return {"csv_rows": len(table), "matched": matched, "total": len(cands)}


# 人工核验回填（Raw Skin/VO/报价/SHEIN·Temu）——SOP §B4/B5 人工步骤，解除 raw_skin_or_vo_unverified。
# CSV 列：handle, raw_skin_grade(A/B/C), has_vo(1/0/yes/no), paid_cpm, shein_temu(1/0)
def enrich_manual(cands: list[dict], csv_path: str) -> dict:
    table: dict[str, dict] = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cm = {n.lower().strip(): n for n in (reader.fieldnames or [])}
        ku = _find_col(cm, ["handle", "username"])
        kr = _find_col(cm, ["raw_skin_grade", "raw skin", "raw skin grade"])
        kv = _find_col(cm, ["has_vo", "vo", "voice over", "voiceover"])
        kc = _find_col(cm, ["paid_cpm", "cpm", "quote cpm"])
        ks = _find_col(cm, ["shein_temu", "shein/temu", "shein temu partnership"])
        if not ku:
            return {"error": "no handle column"}
        for row in reader:
            h = (row.get(ku) or "").strip().lstrip("@").lower()
            if not h:
                continue
            d: dict = {}
            if kr and (row.get(kr) or "").strip():
                d["raw_skin_grade"] = row[kr].strip().upper()[:1]
            if kv and (row.get(kv) or "").strip():
                d["has_vo"] = str(row[kv]).strip().lower() in ("1", "yes", "y", "true", "是")
            if kc:
                d["paid_cpm"] = _f(row.get(kc))
            if ks and (row.get(ks) or "").strip():
                d["shein_temu_partnership"] = str(row[ks]).strip().lower() in ("1", "yes", "y", "true", "是")
            if d:
                table[h] = d
    matched = 0
    for c in cands:
        row = table.get((c.get("handle") or "").lstrip("@").lower())
        if not row:
            continue
        matched += 1
        c.update(row)   # 人工核验优先级最高，直接覆盖
    return {"csv_rows": len(table), "matched": matched, "total": len(cands)}
