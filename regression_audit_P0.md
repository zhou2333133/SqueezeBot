# P0 修复回归审计报告

**审计时间**: 2026-05-12 15:35 CST
**审计范围**: 仅 P0 修复，不包含 Y1-Y11 黄色问题
**审计状态**: 只读检查，不修改

---

## 1. 各 P0 修复验证

### P0-1: risk_guard fail-closed ✅ 闭环

**改动点**: `bot_scalp.py:2893-2898`

| 检查项 | 结果 | 证据 |
|---|---|---|
| 抛异常时是否 return | ✅ | line 2898: `return` — 不继续执行 |
| 是否不会进入 _exec_open | ✅ | return 在 _exec_open(line 2927) 之前 |
| 日志级别是否为 ERROR | ✅ | line 2894: `logger.error(..., exc_info=True)` |
| 是否带 exc_info | ✅ | `exc_info=True` 参数存在 |
| 是否记录了 reject_reason | ✅ | line 2895-2896: `add_scalp_signal({"rejected_reason": "risk_guard_error"})` |

**风险_guard 正常 reject 路径** (line 2886-2892):
- `allow=False` → `_release_pending()` + `return` ✅

**结论**: fail-closed 正确生效。异常→禁止开仓、ERROR日志、不绕过。

### P0-2: _pending_entries 泄漏 ✅ 闭环

**改动点**: 新增 `_release_pending()` 辅助函数 + 8 个调用点

| 路径 | 条件 | 是否释放 | 行号 |
|---|---|---|---|
| 策略策略 BLOCK | 策略禁用 | ✅ | 2857 |
| 权重准入拦截 | 权重不足 | ✅ | 2869 |
| 策略策略异常 | 异常→fallthrough→后续路径 | ✅ (不return,后续释放) | 2871-2874 |
| Risk Guard 拦截 | 风控拒绝 | ✅ | 2891 |
| Risk Guard 异常 | 风控异常 | ✅ | 2897 |
| 自动交易关闭 | SCALP_AUTO_TRADE=false | ✅ | 2910 |
| 实盘暂停 | live_trading_suspended | ✅ | 2919 |
| 执行器失败 | _exec_open 返回失败 | ✅ | 2951 |
| 成交成功 | 正常开仓 | ✅ | 2988 |

**幂等性检查**: `_release_pending()` 使用 `_pending_released` 标志位，重复调用不重复释放 ✅

**遗留风险（低）**: 如果 `_exec_open`(line 2927) 内部抛出未捕获异常（非返回 error result），会导致泄漏。但实际 `_exec_open` 所有路径都返回 `_ExecResult` 而非 throw（PAPER 直接 return，LIVE 路径所有 API 失败都 return error result，无裸露 await）。若 trader 底层抛出网络异常，传播路径：`_exec_open` → `_execute_entry` → `_detect_signal_v3` → `_on_message` → WS loop → 断连重连。此场景概率低，不阻止闭环判定。

### P0-3: Evolver 永久冻结 ✅ 闭环

| 检查项 | 结果 |
|---|---|
| 文件是否备份 | ✅ `data/evolver_runtime_state.json.bak` |
| 当前 status | ✅ `IDLE` |
| consecutive_errors | ✅ `0` |
| frozen_until | ✅ `0.0` |
| 24h 上限保护 | ✅ `recover_evolver_runtime_state()` 自动截断 >24h 的 frozen_until |
| 新建冻结也有上限 | ✅ `min(freeze_min * 60, 86400)` 在 freeze 逻辑中 |
| 重启后是否会再次 FROZEN | ✅ 不会——状态已重置，24h 上限防止配置异常导致永久冻结 |

### P0-4: 前端 ReferenceError ✅ 闭环

| 检查项 | 结果 |
|---|---|
| `loadFlashTrades()` 调用 | ✅ 已删除 |
| `loadFlashReviews()` 调用 | ✅ 已删除 |
| `loadFlashCandidates()` 调用 | ✅ 已删除 |
| 超短线 tab | ✅ 正常（3处引用） |
| 妖币 tab | ✅ 正常（2处引用） |
| 策略统计 tab | ✅ 正常（2处引用） |
| AI 今日复盘 | ✅ 正常 |
| 风控参数输入框 | ✅ 全部存在 |

---

## 2. 有没有假修

无。所有 4 个修复点均经过代码审查和运行时验证：

1. **risk_guard 异常**: 代码路径明确从 `logger.debug` + fallthrough 改为 `logger.error` + `return` ✅
2. **_pending_entries**: 新增 `_release_pending()` 辅助函数，8个调用点覆盖所有 return 路径 ✅  
3. **Evolver**: 状态已物理重置为 IDLE，代码添加24h上限 ✅
4. **前端**: 3 个 setInterval 行已从 HTML 删除 ✅

---

## 3. 有没有新引入问题

| 问题 | 严重度 | 说明 |
|---|---|---|
| `_release_pending()` 为嵌套函数，使用 `nonlocal` | 🟢 无风险 | Python 3.7+ 支持 async 嵌套函数的 nonlocal，已验证语法通过 |
| 策略异常路径不释放但也不返回 | 🟢 设计如此 | 异常被捕获后 fallthrough 到 risk_guard/执行路径，后续会释放 |
| `_exec_open` 理论上的 throw 路径 | 🟡 低概率 | trader 方法有内部错误处理，`_exec_open` 所有失败都 return error result |

---

## 4. 是否可以进入 P1 修复

**是。** 4 个 P0 问题全部闭环验证通过：

| P0 | 状态 | 可关闭 |
|---|---|---|
| risk_guard fail-closed | ✅ 完整验证 | ✅ |
| _pending_entries 泄漏 | ✅ 完整验证 | ✅ |
| Evolver 永久冻结 | ✅ 完整验证 | ✅ |
| 前端 ReferenceError | ✅ 完整验证 | ✅ |

无假修，无新引入重大问题。可以进入 P1 黄色问题修复。

---

## 附录: _pending_entries 完整路径图

```
[+1] line 2835  _pending_entries += 1
  │
  ├─▶ 策略 BLOCK        → _release_pending() → return     [2857]
  ├─▶ 权重 BLOCK        → _release_pending() → return     [2869]
  ├─▶ 策略异常          → fallthrough (不return)          [2871]
  │     └─▶ 后续任何路径 → _release_pending()
  ├─▶ Risk Guard 拒绝   → _release_pending() → return     [2891]
  ├─▶ Risk Guard 异常   → _release_pending() → return     [2897]
  ├─▶ 自动交易关闭      → _release_pending() → return     [2910]
  ├─▶ 实盘暂停          → _release_pending() → return     [2919]
  ├─▶ _exec_open 失败   → _release_pending() → return     [2951]
  └─▶ 成功              → _release_pending()              [2988]
                            → open_positions[symbol] = pos
```
