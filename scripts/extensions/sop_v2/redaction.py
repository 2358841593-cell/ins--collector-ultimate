"""交付包脱敏扫描（P0-13，AUTH-05）。

扫描给定文本/文件是否含凭证模式（sessionid/密码/token/cookie/验证码）。
交付前钩子调用；命中即失败，阻止含密文件外发。
"""
from __future__ import annotations

import re
from pathlib import Path

# 凭证/敏感模式（宁可误报，不可漏放）
PATTERNS = {
    "sessionid": re.compile(r"sessionid\s*[=:]\s*\d+%?[0-9A-Za-z%]+", re.I),
    "ds_user_id_cookie": re.compile(r"ds_user_id\s*[=:]\s*\d{5,}", re.I),
    "password_kv": re.compile(r"(password|passwd|pwd)\s*[=:]\s*\S{6,}", re.I),
    "totp_secret": re.compile(r"\b[A-Z2-7]{16,32}\b"),   # base32 TOTP
    "bearer_token": re.compile(r"bearer\s+[A-Za-z0-9._\-]{20,}", re.I),
    "api_key": re.compile(r"(api[_-]?key|token|secret)\s*[=:]\s*[A-Za-z0-9._\-]{16,}", re.I),
    "csrftoken": re.compile(r"csrftoken\s*[=:]\s*\S+", re.I),
}


def scan_text(text: str) -> list[dict]:
    hits = []
    for name, pat in PATTERNS.items():
        for m in pat.finditer(text):
            hits.append({"pattern": name, "at": m.start(), "sample": _mask(m.group(0))})
    return hits


def _mask(s: str) -> str:
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:6] + "***" + s[-2:]


def scan_file(path: str | Path) -> list[dict]:
    p = Path(path)
    try:
        return scan_text(p.read_text(errors="ignore"))
    except (OSError, UnicodeError):
        return []


def scan_delivery(dir_path: str | Path, exts=(".json", ".html", ".csv", ".md", ".txt", ".log")) -> dict:
    d = Path(dir_path)
    findings = {}
    for f in d.rglob("*"):
        if f.is_file() and f.suffix.lower() in exts:
            h = scan_file(f)
            if h:
                findings[str(f)] = h
    return findings
