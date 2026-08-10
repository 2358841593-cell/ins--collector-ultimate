"""国家/地区名称规范化。

项目内部沿用两位大写代码，并把英国统一记为 ``UK``（而不是 ISO 的 ``GB``）。
只转换明确可识别的值：两位字母代码直接规范化，已知全名走别名表；未知全名返回
``None``，避免用前两个字符猜测国家（例如 Ukraine 不能被误写成 UK）。
"""
from __future__ import annotations

import re
import unicodedata


def _name_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


_COUNTRY_NAMES = {
    # 当前投放目标国家/地区。
    "united states": "US",
    "united states of america": "US",
    "america": "US",
    "canada": "CA",
    "united kingdom": "UK",
    "great britain": "UK",
    "britain": "UK",
    "germany": "DE",
    "italy": "IT",
    "france": "FR",
    "spain": "ES",
    "netherlands": "NL",
    "the netherlands": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "sweden": "SE",
    # Modash 历史数据与客户本轮反馈中已出现的非目标国家/地区。
    "australia": "AU",
    "argentina": "AR",
    "brazil": "BR",
    "mexico": "MX",
    "india": "IN",
    "indonesia": "ID",
    "portugal": "PT",
    "ireland": "IE",
    "austria": "AT",
    "poland": "PL",
    "turkey": "TR",
    "türkiye": "TR",
    "philippines": "PH",
    "united arab emirates": "AE",
    "uae": "AE",
    "russia": "RU",
    "russian federation": "RU",
    "ukraine": "UA",
    "south korea": "KR",
    "korea south": "KR",
    "republic of korea": "KR",
    "korea republic of": "KR",
    "syria": "SY",
    "syrian arab republic": "SY",
    "iran": "IR",
    "islamic republic of iran": "IR",
    "iran islamic republic of": "IR",
}

_ALIASES = {_name_key(name): code for name, code in _COUNTRY_NAMES.items()}
_CODE_ALIASES = {"GB": "UK"}


def normalize_country_code(value: object) -> str | None:
    """Return the project's canonical two-letter code, or ``None`` if unknown.

    A two-letter ASCII value is already an explicit country code, so it is
    accepted without guessing. Longer values must match a known country name.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    code = text.upper()
    if len(code) == 2 and code.isascii() and code.isalpha():
        return _CODE_ALIASES.get(code, code)
    return _ALIASES.get(_name_key(text))
