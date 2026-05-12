# execution_router 接入回归审计报告

**审计时间**: 2026-05-12 16:20 CST
**审计范围**: execution_router 接入后回归检查
**审计状态**: 只读，未修改代码

---

## 1. 开仓链路回归 ✅ 闭环

### 所有开仓入口

```
_detect_signal_v3
  └→ _execute_entry (唯一入口, 6种信号共用)
       └→ check_open_order()     (bot_scalp.py:2878)  ← risk_guard 检查
       └→ _exec_open()           (bot_scalp.py:2907)  ← 唯一开仓执行
            └→ execution_router.execute()  (execution_router.py:306)
                 ├→ PAPER: 返回虚拟成交 dict
                 └→ LIVE: trader.* API 调用
```

| 检查项 | 结果 |
|---|---|
| 开仓是否只有 `_exec_open` 一个入口 | ✅ 是 — 仅 line 2907 |
| `_exec_open` 是否调用 `execution_router.execute()` | ✅ 是 — line 306 |
| `_exec_open` 内部是否有 `is_paper` 分支 | ❌ AST 验证: 无 |
| PAPER/LIVE 差别是否在 execution_router 内部 | ✅ 是 — execute() 内部 `if is_paper:` |
| execution_router 返回统一结构 | ✅ 12 字段统一 dict |

### 返回结构一致性

| 字段 | PAPER | LIVE(成功) | LIVE(失败) |
|---|---|---|---|
| success | True | True | False |
| mode | "PAPER" | "LIVE" | "LIVE" |
| qty | quantity | filled_qty | 0 |
| price | entry_price | avgPrice | 0 |
| order_id | None | orderId | None |
| error | "" | "" | "reason" |
| raw_response | None | trade_resp | None |

---

## 2. 风控顺序 ✅ 正确

| 检查项 | 结果 | 行号 |
|---|---|---|
| `check_open_order()` 在 `execute()` 之前 | ✅ | 2878 check → 2907 _exec_open → 306 execute |
| risk_guard 异常时不会进入 execution_router | ✅ | 2893-2898: `except → _release_pending() → return` |
| risk_guard reject 时不会进入 execution_router | ✅ | 2886-2892: `allow=False → return` |
| execution_router 异常时 _pending_entries 释放 | ✅ | 2905-2992: `try/finally → _release_pending()` |
| 风控 reject 信号记录 | ✅ | `add_scalp_signal({rejected_reason})` |

**风控-执行顺序链**:
```
_pending_entries += 1     (line 2852)
  ↓
strategy_policy check      → BLOCK → _release_pending() + return
  ↓  
check_open_order()         → BLOCK → _release_pending() + return
  ↓                        → 异常 → _release_pending() + return (fail-closed)
_exec_open                 
  └→ execution_router.execute()
     └→ trader API          → 成功 → _release_pending() in finally
                            → 异常 → _release_pending() in finally
```

---

## 3. 平仓路径审计

| 路径 | 函数 | 调用方式 | 是否走 execution_router | 建议 |
|---|---|---|---|---|
| TP1 平仓 | `_check_tp_sl` → `_close_remaining` | `_exec_close_qty()` | ❌ 直接调用 trader | 本轮不改 |
| TP2 平仓 | `_check_tp_sl` → `_close_remaining` | `_exec_close_qty()` | ❌ 直接调用 trader | 本轮不改 |
| TP1→SL 替换 | `_check_tp_sl` → `_exec_replace_sl()` | `trader.cancel_order + place_stop_loss` | ❌ | 本轮不改 |
| SL 止损 | `_check_tp_sl` (waterfall) | `_exec_close_qty()` | ❌ | 本轮不改 |
| 时间止损 | `_check_tp_sl` → `_close_remaining` | `_exec_close_qty()` | ❌ | 本轮不改 |
| 手动平仓 | `manual_close_position` | `_exec_close_qty()` | ❌ | 后续可接入 |
| 紧急平仓(新闻) | `_emergency_close_position` | `_exec_close_qty()` | ❌ | **保持现状** — 新闻紧急平仓需绕过正常风控 |
| 保护失败重试 | `_sync_live_position_once` | `trader.place_reduce_only_market_order()` | ❌ | **保持现状** — 保护路径必须直接操作交易所 |
| 孤儿仓自动保护 | `_orphan_scan_once` | `trader.place_stop_loss_order()` | ❌ | **保持现状** — 孤儿仓保护需绕过正常风控 |
| 启动恢复对账 | `_restore_positions` | `trader.get_position()` | ❌ 只读 | **保持现状** |

**结论**: 所有平仓路径目前都不走 execution_router。其中:
- **可后续接入**: TP/SL 正常平仓、手动平仓（通过 `_exec_close_qty` 包装层）
- **应保持现状**: 紧急平仓(新闻)、保护失败重试、孤儿仓、启动恢复 — 这些路径的目的是绕过正常交易逻辑直接操作交易所，不应该加额外路由层

---

## 4. LIVE 安全检查

| 检查项 | 结果 | 行号 |
|---|---|---|
| trader 未初始化 → 安全失败 | ✅ `_error_result("LIVE", ..., "trader_uninitialized")` | execution_router.py:71 |
| 下单失败 → 不会创建 ScalpPosition | ✅ `_execute_entry` 检查 `result.success` 后 return | bot_scalp.py:2926 |
| IOC 无成交 → error 明确 | ✅ `"ioc_no_fill"`, `qty=0`, `success=False` | execution_router.py:106-118 |
| 部分成交 → filled_qty=实际值 | ✅ `filled_qty = float(trade_resp.get("executedQty", 0))` | execution_router.py:100 |
| 保护失败+紧急撤出成功 → 返回明确 error | ✅ `"stop_loss_order_failed_emergency_closed"`, `protection_failed=True` | execution_router.py:126-138 |
| 保护失败+紧急撤出失败 → suspend | ✅ `_suspend_live_trading()` 由 `_exec_open` 调用 | bot_scalp.py:308-309 |
| 杠杆设置失败 → 安全返回 | ✅ `"leverage_set_failed"` | execution_router.py:86 |

---

## 5. PAPER 一致性检查

| 检查项 | 结果 |
|---|---|
| PAPER 返回的 qty/price/mode/success 字段 | ✅ 与 LIVE 兼容 (mode=PAPER) |
| PAPER 成交后走相同持仓创建路径 | ✅ ScalpPosition 创建在 _execute_entry, 不区分 PAPER/LIVE |
| PAPER 走相同成交记录 | ✅ `_record_scalp_trade` → `strategy_stats.record_trade()` |
| PAPER 走相同 Evolver 统计 | ✅ `strategy_trades.jsonl` 包含 `execution_mode` 字段区分 |
| PAPER 无单独账本 | ✅ 无 PAPER-only 账本 |
| PAPER 无单独状态 | ✅ `open_positions` 共用 dict，`pos.paper` 仅标记字段 |

---

## 6. 数据安全

| 文件 | 状态 |
|---|---|
| `strategy_trades.jsonl` | ✅ 84行，未修改 |
| `boundary_config.json` | ✅ execution_mode=PAPER, order_amt=100, max_pos=3 |
| `settings.json` | ✅ SCALP_POSITION_USDT=100, MAX_POSITIONS=3, LEVERAGE=10 |
| 所有 data/ 文件 | ✅ 未删除、未修改 |

---

## 7. 总体结论

### execution_router 是否真正闭环
**是。** 所有开仓路径：
```
6种信号 → _execute_entry → _exec_open → execution_router.execute() → PAPER/LIVE
```
`_exec_open` 已无直接 `is_paper` 分支，PAPER/LIVE 差异完全集中在 `execution_router.execute()` 内部。

### 是否还有绕过 router 的开仓路径
**无。** 唯一 `_exec_open` 调用点强制执行路由。`execution_router._route_open()`（旧同步代码）未被调用，但无安全影响 — 它是死代码而非绕过。

### 平仓路径当前状态
所有平仓路径仍直接调用 `_exec_close_qty` / `trader.*`，不走 execution_router。其中：
- **可后续接入**: TP/SL、手动平仓（`_exec_close_qty` 已有 is_paper 分支）
- **建议保持**: 紧急平仓、保护重试、孤儿仓、启动对账（需直接操作交易所）

### 是否发现新问题
无新的 P0/P1 问题。以下为已知遗留项（不在本轮范围）：
- `_exec_close_qty` 仍有 `is_paper` 分支（close 路径）
- `execution_router._route_open()` 是死代码（旧 route_order 兼容层）
- 启动恢复/孤儿仓直接调用 trader（设计如此，不经过风控）

### 是否可以进入 UI 简化阶段
**可以。** execution_router 接入验证完成，4 个 P0 修复已闭环，无回归。
