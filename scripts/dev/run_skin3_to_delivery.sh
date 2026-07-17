#!/bin/bash
# SKIN3 完整重跑收尾链：等深采跑完 → 补采失败的 → 决策+Modash补数 → 出交付表
# 用法：nohup bash scripts/dev/run_skin3_to_delivery.sh > reports/deliveries/chain_SKIN3.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/../.."          # 仓库根
ROOT="$PWD"
PY="$ROOT/.venv/bin/python3"
BATCH="SKIN3-20260717"
OUTDIR="$ROOT/reports/deliveries/$BATCH"
mkdir -p "$OUTDIR"
cd "$ROOT/scripts"
export PYTHONPATH=.

say() { echo ""; echo "═══ $* ═══"; date '+%F %T'; }

# ── 1) 等首轮深采跑完 ──
say "1/6 等首轮深采(PID ${STAGE3_PID:-none})跑完"
while [ -n "${STAGE3_PID:-}" ] && ps -p "$STAGE3_PID" >/dev/null 2>&1; do sleep 20; done
$PY -c "from extensions.sop_v2 import creator_cache as cc; print('状态:', cc.status_dist('$BATCH'))"

# ── 2) 监控：揪出抖动/不完整 ──
say "2/6 采集完整性监控"
$PY -m extensions.sop_v2.pipeline.audit_collect --batch-id "$BATCH"

# ── 3) 回头补采：退回 qualified 后重跑深采 ──
say "3/6 退回不完整项 → 补采"
$PY -m extensions.sop_v2.pipeline.audit_collect --batch-id "$BATCH" --requeue
$PY -m extensions.sop_v2.pipeline.stage3_collect --batch-id "$BATCH" --posts 10

# ── 4) 补采后再查一次（还不完整的如实记录，不再重试，避免死循环烧号）──
say "4/6 补采后复查（残留失败如实记录）"
$PY -m extensions.sop_v2.pipeline.audit_collect --batch-id "$BATCH"

# ── 5) 决策 + Modash 补数（原始报告缓存命中则免 credit）──
say "5/6 决策 + Modash 补数"
$PY -m extensions.sop_v2.pipeline.stage4_decide \
    --batch-id "$BATCH" --track paid \
    --out "$OUTDIR/decisions.json" \
    --modash-cdp --modash-cap 40 --no-xlsx

# ── 6) 出交付表 ──
say "6/6 生成 HTML 交付表"
$PY "$ROOT/scripts/export_v2_html.py" \
    --decisions "$OUTDIR/decisions.json" \
    --out "$OUTDIR/deliverable.html"

say "全链完成"
$PY -c "from extensions.sop_v2 import creator_cache as cc; print('最终状态:', cc.status_dist('$BATCH'))"
echo "交付表 → $OUTDIR/deliverable.html"
