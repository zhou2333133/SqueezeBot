#!/bin/bash
# SqueezeBot 一键状态抓取
# 在 Linux 机器上运行: bash dump_bot_status.sh
HOST="http://127.0.0.1:8000"
OUTDIR="./bot_dump_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "抓取运行状态到 $OUTDIR ..."

# 1. Evolver 状态
curl -s "$HOST/api/evolver/status" > "$OUTDIR/evolver_status.json"
echo "  [1/9] Evolver 状态"

# 2. 健康检查
curl -s "$HOST/api/evolver/health" > "$OUTDIR/evolver_health.json"
echo "  [2/9] 健康检查"

# 3. 策略统计
curl -s "$HOST/api/strategy/stats?mode=all" > "$OUTDIR/strategy_stats.json"
echo "  [3/9] 策略统计"

# 4. 完整复盘包
curl -s "$HOST/api/scalp/analysis-pack.json?detail=full" > "$OUTDIR/analysis_pack_full.json"
echo "  [4/9] 完整复盘包"

# 5. 成交明细
curl -s "$HOST/api/scalp/trades" > "$OUTDIR/scalp_trades.json"
echo "  [5/9] 成交明细"

# 6. 策略成交明细
curl -s "$HOST/api/strategy/trades?limit=100&mode=all" > "$OUTDIR/strategy_trades.json"
echo "  [6/9] 策略成交"

# 7. Autopilot Guard
curl -s "$HOST/api/autopilot/status" > "$OUTDIR/autopilot_status.json"
echo "  [7/9] Guard 状态"

# 8. 当前配置关键参数
curl -s "$HOST/api/config" | python3 -c "
import sys, json
cfg = json.load(sys.stdin)
keys = ['SCALP_PAPER_TRADE','SCALP_MAX_POSITIONS','SCALP_POSITION_USDT','SCALP_RISK_PER_TRADE_USDT',
        'EVOLVER_ENABLED','EVOLVER_AUTO_APPLY','AUTOPILOT_GUARD_ENABLED','ORPHAN_SCAN_ENABLED',
        'POLICY_VERSION','SCALP_LEVERAGE','SCALP_ENABLED','SCALP_AUTO_TRADE']
out = {k: cfg.get(k) for k in keys}
for k,v in out.items():
    print(f'{k}={v}')
" > "$OUTDIR/key_configs.txt"
echo "  [8/9] 关键配置"

# 9. 最近日志尾巴
curl -s "$HOST/api/scalp/signals" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        print(f'Recent signals: {len(data)}')
        for s in data[-5:]:
            print(f'  {s.get(\"symbol\",\"\")} {s.get(\"direction\",\"\")} label={s.get(\"signal_label\",\"\")} tag={s.get(\"strategy_tag\",\"\")}')
    else:
        print(json.dumps(data, indent=2)[:2000])
except: print('no signals')
" > "$OUTDIR/recent_signals.txt"
echo "  [9/9] 最近信号"

echo ""
echo "完成！数据目录: $OUTDIR"
echo "文件列表:"
ls -la "$OUTDIR/"
echo ""
echo "打包发送: tar czf ${OUTDIR}.tar.gz $OUTDIR/"
