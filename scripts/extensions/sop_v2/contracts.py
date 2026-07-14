"""SOP V2 数据合同（P0-2）。

FieldEvidence / GateResult / ScoreItem / BatchManifest —— 决策全过程可追溯的最小结构。
缺失值一律 missing/pending/N/A/conflict，禁止填 0（区分"值为 0"与"没有值"）。
纯数据类，无外部依赖，可 JSON round-trip。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class FieldStatus(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    PENDING = "pending"
    NOT_APPLICABLE = "not_applicable"
    CONFLICT = "conflict"


class GateVerdict(str, Enum):
    PASS = "pass"
    EXCLUDE = "exclude"
    REVIEW = "review"


class Pool(str, Enum):
    INCLUDE_WITH_STOREFRONT = "Include-With-Storefront"
    INCLUDE_WITHOUT_STOREFRONT = "Include-Without-Storefront"
    PRIORITY_REVIEW = "Priority-Review"
    REVIEW = "Review"
    EXCLUDE = "Exclude"


@dataclass
class FieldEvidence:
    """一个标准化字段的完整证据链。"""
    field: str
    value: Any = None
    raw_value: Any = None
    source: str = ""            # modash | instagram | browser | manual | quote
    captured_at: str = ""
    evidence_ref: str = ""      # URL / 截图 / PDF 页 / 评论链接
    status: FieldStatus = FieldStatus.MISSING

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class GateResult:
    gate_id: str
    result: GateVerdict
    observed: Any = None
    threshold: Any = None
    reason_code: str = ""
    source: str = ""
    evidence_ref: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["result"] = self.result.value
        return d


@dataclass
class ScoreItem:
    module: str                 # A-F
    item: str
    earned: float | None = None
    available: float | None = None   # None → N/A（从分母移除）
    reason: str = ""
    evidence_ref: str = ""

    @property
    def is_na(self) -> bool:
        return self.available is None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchManifest:
    batch_id: str
    sop_version: str
    campaign_track: str          # paid | gifting
    config_sha256: str = ""
    source_sha256: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)   # 一个批次可跨多次采集 run
    params: dict = field(default_factory=dict)
    stage_counts: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _default(o):
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"not serializable: {type(o)}")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=_default)
