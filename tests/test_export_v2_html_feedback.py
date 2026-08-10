"""客户交付页的结构化反馈 UI/JSON 合同。"""
from __future__ import annotations

import sys
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import export_v2_html  # noqa: E402
from extensions.sop_v2 import feedback_taxonomy  # noqa: E402


def _delivery() -> dict:
    return {
        "manifest": {"batch_id": "FEEDBACK-V2", "campaign_track": "paid"},
        "generated_at": "2026-08-10T00:00:00Z",
        "candidates": [
            {
                "handle": "sample_creator",
                "final_pool": "Review",
                "ai_vetting_score": 7.2,
            }
        ],
    }


def test_feedback_ui_is_generated_from_canonical_taxonomy():
    rendered = export_v2_html.build_html(_delivery())

    for reason in feedback_taxonomy.REASON_DEFINITIONS:
        assert rendered.count(f'value="{reason.code}"') == 1
        assert reason.label_zh in rendered
    assert (
        f'window.__FEEDBACK_TAXONOMY_VERSION__="{feedback_taxonomy.TAXONOMY_VERSION}"'
        in rendered
    )
    assert 'value="account">仅此账号' in rendered
    assert 'value="policy_signal">希望后续统一参考' in rendered
    assert "不会自动变成全局规则" in rendered
    assert 'value="campaign">仅当前活动不合适（未来可重评）' in rendered
    assert 'value="temporary">暂时不合适（后续复核）' in rendered
    assert 'value="global">永久排除（所有后续轮次）' in rendered
    assert "只有“永久排除”会进入永久负向库" in rendered


def test_feedback_export_keeps_legacy_fields_and_adds_v2_contract():
    rendered = export_v2_html.build_html(_delivery())

    # 旧导入端继续能读取这五个字段；新端额外获得结构化原因与作用范围。
    assert "handle:dec.dataset.h, verdict:v, reason:r" in rendered
    assert "pool:dec.dataset.pool, score:dec.dataset.score" in rendered
    assert "reason_tags:tags, feedback_scope:feedbackScope(dec)" in rendered
    assert "rejection_scope:v === '不合适' ? rejectionScope(dec) : null" in rendered
    assert "feedback_schema_version:2, taxonomy_version:TAXONOMY_VERSION" in rendered


def test_rejection_reason_is_optional_and_old_local_state_can_restore():
    rendered = export_v2_html.build_html(_delivery())

    assert "原因不是必填项" in rendered
    assert "if(v || r || tags.length)" in rendered
    assert "if(!out.length){ alert('还没做任何选择'); return; }" in rendered
    assert "if(d.reason) dec.querySelector('.dr').value = d.reason;" in rendered
    assert "if(Array.isArray(d.reason_tags))" in rendered
    assert "d.feedback_scope === 'policy_signal' ? 'policy_signal' : 'account'" in rendered
    assert "? d.rejection_scope : 'campaign'" in rendered


def test_decision_cell_escapes_candidate_metadata():
    rendered = export_v2_html._decide_cell(  # noqa: SLF001
        {
            "handle": 'unsafe" onmouseover="boom',
            "final_pool": '<script>alert("x")</script>',
            "ai_vetting_score": '7" data-evil="1',
        }
    )

    assert 'onmouseover="boom' not in rendered
    assert "<script>alert" not in rendered
    assert 'data-evil="1' not in rendered
    assert "&quot;" in rendered


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_browser_script_exports_optional_reason_and_restores_legacy_state():
    """用最小 DOM 跑真实交互脚本，不依赖浏览器或第三方 JS 包。"""
    interact_js = export_v2_html._INTERACT_JS.removeprefix("<script>").removesuffix(  # noqa: SLF001
        "</script>"
    )
    harness = r"""
class ClassList {
  constructor(names){ this.names = new Set(names || []); }
  add(name){ this.names.add(name); }
  remove(name){ this.names.delete(name); }
}
function control(classes){
  return {classList:new ClassList(classes), value:'', checked:false,
          closest:function(){ return dec; }};
}
var yes = control(['db','yes']);
var no = control(['db','no']);
var maybe = control(['db','maybe']);
var freeText = control(['dr']);
var scope = control(['fs']); scope.value = 'account';
var rejectionScope = control(['rs']); rejectionScope.value = 'campaign';
var tag = control(['rt']); tag.value = 'quality_video_low';
var dec = {
  dataset:{h:'sample_creator',pool:'Review',score:'7.2'},
  querySelectorAll:function(selector){
    if(selector === '.db') return [yes,no,maybe];
    if(selector === '.rt') return [tag];
    if(selector === '.rt:checked') return tag.checked ? [tag] : [];
    return [];
  },
  querySelector:function(selector){
    if(selector === '.dr') return freeText;
    if(selector === '.fs') return scope;
    if(selector === '.rs') return rejectionScope;
    if(selector === '.db.yes') return yes;
    if(selector === '.db.no') return no;
    if(selector === '.db.maybe') return maybe;
    return null;
  }
};
var stat = {textContent:''};
var storage = new Map();
storage.set('dec_FEEDBACK-V2_sample_creator', JSON.stringify({verdict:'待定',reason:'legacy note'}));
globalThis.window = globalThis;
window.__BATCH__ = 'FEEDBACK-V2';
window.__FEEDBACK_TAXONOMY_VERSION__ = '1.0.0';
globalThis.localStorage = {
  getItem:function(key){ return storage.has(key) ? storage.get(key) : null; },
  setItem:function(key,value){ storage.set(key,value); },
  removeItem:function(key){ storage.delete(key); }
};
globalThis.document = {
  querySelectorAll:function(selector){ return selector === '.dec' ? [dec] : []; },
  getElementById:function(){ return stat; },
  createElement:function(){ return {href:'',download:'',click:function(){}}; }
};
var alerts = [];
globalThis.alert = function(message){ alerts.push(message); };
globalThis.confirm = function(){ return true; };
var lastBlob = '';
globalThis.Blob = class { constructor(parts){ lastBlob = parts[0]; } };
globalThis.URL = {createObjectURL:function(){ return 'blob:test'; },revokeObjectURL:function(){}};
""" + interact_js + r"""
var restored = {verdict:dec.dataset.verdict,reason:freeText.value,scope:scope.value,
                rejectionScope:rejectionScope.value};
window.clearDecisions();
window.mark(no, '不合适');
window.exportDecisions();
var noReason = JSON.parse(lastBlob);
tag.checked = true; window.saveD(tag);
scope.value = 'policy_signal'; window.saveD(scope);
rejectionScope.value = 'temporary'; window.saveD(rejectionScope);
window.exportDecisions();
var structured = JSON.parse(lastBlob);
console.log(JSON.stringify({restored:restored,noReason:noReason,structured:structured,alerts:alerts}));
"""
    completed = subprocess.run(  # noqa: S603
        [shutil.which("node"), "-e", harness],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["restored"] == {
        "verdict": "待定",
        "reason": "legacy note",
        "scope": "account",
        "rejectionScope": "campaign",
    }
    no_reason = result["noReason"]
    assert no_reason["feedback_schema_version"] == 2
    assert no_reason["taxonomy_version"] == feedback_taxonomy.TAXONOMY_VERSION
    assert no_reason["decisions"][0] == {
        "handle": "sample_creator",
        "verdict": "不合适",
        "reason": "",
        "pool": "Review",
        "score": "7.2",
        "reason_tags": [],
        "feedback_scope": "account",
        "rejection_scope": "campaign",
    }
    assert result["structured"]["decisions"][0]["reason_tags"] == [
        "quality_video_low"
    ]
    assert result["structured"]["decisions"][0]["feedback_scope"] == "policy_signal"
    assert result["structured"]["decisions"][0]["rejection_scope"] == "temporary"
    assert result["alerts"] == []
