# SqueezeBot 全量运行状态审计报告

**审计时间**: 2026-05-12 14:55 CST
**审计模式**: 只读，未修改任何代码或数据
**审计范围**: 启动/配置/下单链路/PAPER-LIVE/Evolver/数据账本/risk_guard/UI/废弃代码/日志

---

## 1. 总体结论

| 项目 | 状态 |
|---|---|
| 当前是否能启动 | ✅ 能启动，MarketHub/Web/FastAPI 均正常初始化 |
| 当前是否能模拟盘运行 | ✅ 可以，PAPER 模式下 _exec_open 直接返回虚拟成交 |
| 当前是否适合实盘 | ⚠️ **不推荐** — 见红色问题 |
| 最大风险 | **risk_guard 异常被静默跳过**：check_open_order 抛异常时交易继续执行不拦截；**_pending_entries 泄漏**：风控/策略拦截后计数器不减，逐步耗尽仓位额度；**Evolver 永久冻结**：连续99错误后冻结到2027年，不会自动恢复 |

---

## 2. 红色问题（严重 — 需优先修复）

### R1. risk_guard 异常不阻断交易
- **文件**: `bot_scalp.py:2883-2884`
- **问题**: `check_open_order()` 调用在 try/except 中，异常时仅 `logger.debug` 日志，**交易继续执行**
- **风险**: 如果 boundary_config.json 损坏或 I/O 错误，风控完全失效，所有订单畅通无阻
- **影响**: 实盘直接亏钱

### R2. _pending_entries 泄漏
- **文件**: `bot_scalp.py:2835`
- **问题**: `_pending_entries += 1` 在 risk_guard 和策略策略检查**之前**执行。如果被拦截（return），计数器不减
- **影响**: 每次被风控/策略拦截，永久减少 1 个仓位额度。假设 SCALP_MAX_POSITIONS=3，拦截3次后永不入场
- **证据**: 代码只有 3 个 decrement 点（line 2863 except、2971 成功创建后），而 return 路径有 5+ 个

### R3. Evolver 永久冻结，永远不会自动解冻
- **文件**: `data/evolver_runtime_state.json`
- **问题**: `status=FROZEN`, `consecutive_errors=99`, `frozen_until=1810101851`（2027年+）
- **影响**: Evolver 已死亡，不会自动恢复。需要手动解冻才能恢复自动进化
- **矛盾点**: `last_success=true` 但 `consecutive_errors=99`

### R4. 前端3个定时函数未定义，每分钟报 ReferenceError
- **文件**: `templates/index.html:2115-2117`
- **问题**: `setInterval` 调用 `loadFlashTrades()`、`loadFlashReviews()`、`loadFlashCandidates()`，但这些函数**从未定义**
- **影响**: 浏览器控制台每分钟 3 次 ReferenceError，未来一旦严格模式可能崩溃

---

## 3. 黄色问题（需关注，不影响启动但影响清晰度）

### Y1. LOCKED_PARAMS 不完整
- **config.py:172-176**: 只锁了 `SCALP_MAX_POSITIONS`, `SCALP_POSITION_USDT`, `SCALP_RISK_PER_TRADE_USDT`
- **未锁定（但也不在 PARAM_BOUNDS 中，所以 Evolver 实际上无法修改）**:
  - `SCALP_LEVERAGE` — 不在 PARAM_BOUNDS，Evolver 无法改，但概念上应锁定
  - `SCALP_PAPER_TRADE` — 也不在 PARAM_BOUNDS
- **boundary_config 参数**：`execution_mode`, `trading_enabled`, `allow_open`, `allow_close`, `max_open_positions`, `max_leverage`, `daily_max_loss_usdt`, `max_account_drawdown_pct` 全部不在 LOCKED_PARAMS 中
  - 但 risk_guard.py:55-59 有 `LOCKED_PARAMS_NAMES` 硬编码保护这 9 个边界参数
  - **双层防御有效**但需要审计确认一致性

### Y2. execution_router.route_order() 从未被调用
- **文件**: `execution_router.py:17`
- **问题**: `route_order()` 是设计中的统一入口，但 `bot_scalp.py` 直接调用 `_exec_open`，绕过了 route_order。execution_router 只被用作 `verify_execution()` 后验检查
- **影响**: execution_router 的 `_route_open` 内也调用了 `check_open_order`，但这段代码是死代码

### Y3. apply_param_updates() 是独立写路径
- **文件**: `strategy_evolver.py:375-397`
- **问题**: 这个函数直接 `config_manager.settings[key] = converted` + `_persist()`，绕过 risk_guard。虽然 `run_evolution_once()` 不使用它，但它仍然是公开导出的函数
- **风险**: 低（无人调用），但存在潜在绕过路径

### Y4. deprecated/proposal_validator.py 仍被生产代码引用
- **文件**: `strategy_evolver.py:537`, `evolver_runtime.py:239`
- **问题**: 这是 deprecated/ 下唯一仍在生产代码中 import 的文件。counterfactual validation 逻辑未迁移到别处
- **影响**: 如果删除 deprecated/，会直接 break Evolver

### Y5. autopilot_guard.py 与 risk_guard.py 重复定义 guard 事件函数
- **autopilot_guard.py** 写 `autopilot_guard_events.jsonl`，**risk_guard.py** 写 `guard_events.jsonl`
- 两个文件各有一套 `write_guard_event()` / `get_guard_events()`
- **风险**: 低，但事件分裂在两个文件，查询需要看两个地方

### Y6. 多个 silent failure（异常被吃但不报 ERROR）
- **bot_scalp.py**: 17+ 个 `except: logger.debug(...)` 模式 — 异常在 DEBUG 级别记录，生产环境看不到
- **risk_guard.py**: 5 个 `except: pass` — boundary_config 读取失败、事件文件损坏等全部沉默
- **影响**: 出问题时无法诊断

### Y7. 废弃前端状态变量
- **templates/index.html**: `cockpitPage`, `cockpitLogs`, `cockpitGuardEvents`, `cockpitLoading`, `cockpitError` 已定义但未渲染
- **影响**: 无，只是垃圾数据

### Y8. 12 个后端 API 端点未被前端调用
- `/api/strategy/trades`, `/api/evolver/performance`(且broken), `/api/evolver/shadow`, `/api/evolver/validations`, `/api/evolver/freeze`, `/api/evolver/unfreeze`, 5 个 autopilot 端点, `/api/yaobi/diagnostics`, `/api/yaobi/anomalies`

### Y9. /api/evolver/performance 有 import bug
- **web.py:1417**: 调用了 `get_recent_policy_performance()` 但该函数未 import
- **影响**: 访问此端点会 NameError（但前端没有调用它，所以不实际触发）

### Y10. Evolver 样本门槛不适合模拟盘冷启动
- 代码中硬编码默认值: `EVOLVER_MIN_TOTAL_TRADES=50`, `EVOLVER_MIN_TRADES_PER_STRATEGY=20`
- settings.json 实际值: `MIN_TOTAL_TRADES=15`, `MIN_TRADES_PER_STRATEGY=8`
- 代码默认值 vs 配置默认值不一致（50 vs 15）
- 模拟盘冷启动时如果没有 15 笔以上成交，Evolver 永不运行

### Y11. WARNING 日志每分钟刷一次
- `API Key 未配置` 每 30-60 秒一次，已积累 35 条
- **影响**: 日志文件膨胀，掩盖真正的警告

---

## 4. 绿色正常项

- ✅ **所有模块 import 正常** — 16 个核心模块全部通过
- ✅ **所有文件语法正确** — 6 个关键文件 `py_compile` 通过
- ✅ **成交数据仅一份主账本** — `strategy_stats.record_trade()` 是唯一写入 `strategy_trades.jsonl` 的路径
- ✅ **Evolver 只提案** — `run_evolution_once()` 通过 `risk_guard.apply_proposals()` 写入，不直接写
- ✅ **boundary_config.json 只由 risk_guard 写入** — Evolver/AI 从不接触 boundary_config
- ✅ **param_attribution 不写 config** — 只写自己的 `param_patches.jsonl`
- ✅ **evolver_runtime 不写 config** — 只写状态文件，零 config write
- ✅ **Evolver 不能修改开仓金额/最大持仓/杠杆** — 这些不在 PARAM_BOUNDS 中
- ✅ **PAPER/LIVE 共用同一套决策逻辑** — 差别仅在 `_exec_*` 内部 `is_paper` 分支
- ✅ **deprecated/single_coin_judge.py 和 multi_tf_gate.py 完全死代码** — 零引用
- ✅ **evolver_status.py 已清理** — 功能全部合并到 risk_guard.py
- ✅ **bot_scalp.py 无死 import** — 所有 import 均有引用
- ✅ **数据文件无损坏** — strategy_trades.jsonl(84行)、shadow_trades.jsonl(2008行) 全部有效 JSON
- ✅ **无死循环/递归风险** — 所有 loop 使用 `while self.running` + 退避

---

## 5. PAPER / LIVE 状态

| 检查项 | 结论 |
|---|---|
| 是否共用同一套信号逻辑 | ✅ 是 — `_detect_signal_v3()` 无 `is_paper` 分支 |
| 是否共用同一套 AI 判断 | ✅ 是 — `_yaobi_entry_guard()` / Surf AI 共用 |
| 是否共用同一套 risk_guard | ✅ 是 — `check_open_order()` 不区分 PAPER/LIVE |
| 是否共用同一套持仓统计 | ✅ 是 — `strategy_stats.record_trade()` 用 `execution_mode` 字段区分 |
| 是否共用同一套成交账本 | ✅ 是 — `strategy_trades.jsonl` 是唯一账本 |
| 是否共用同一套 Evolver 复盘 | ✅ 是 — Evolver 读取 `strategy_trades.jsonl`，含 `execution_mode` |
| 差别是否只在 executor | ✅ 是 — `_exec_open` / `_exec_close_qty` 内部 `if is_paper:` 分支 |
| 是否有数据分裂 | ❌ **轻微分裂** — `scalp_trade_history`（内存列表，signals.py）和 `strategy_trades.jsonl` 是两套数据，web 面板读内存，Evolver 读文件 |

---

## 6. risk_guard 接入状态

| 入口 | 是否接入 | 说明 |
|---|---|---|
| `_execute_entry` → `_exec_open` | ✅ 接入 | line 2869-2876 调用 `check_open_order()` |
| `_detect_signal_v3` → `_execute_entry` | ✅ 间接 | 通过 `_execute_entry` 接入 |
| `execution_router._route_open` | ✅ 代码存在 | 但从未被调用（死代码） |
| **手动开仓 API** (`/api/trade`) | ❓ 需检查 | 未在本次审计范围内 |
| 平仓路径 (`_emergency_close_position`) | ❌ 不适用 | 平仓不经过 risk_guard（设计如此） |
| **异常时绕过风险** | ❌ **危险** | line 2883-2884: 抛异常时不拦截 |

**是否存在绕过路径**: 
- 直接调用 `_exec_open()` 可以绕过 risk_guard（但 `_exec_open` 本身是 private 方法，只有 `_execute_entry` 调用它）
- `apply_param_updates()` 可以直接写配置（但无调用者）
- **结论**: 当前实际执行路径中，无有效绕过路径。但异常处理不完善。

---

## 7. Evolver 状态

| 检查项 | 结论 |
|---|---|
| 是否只提案 | ✅ 是 — `run_evolution_once()` 通过 `risk_guard.apply_proposals()` |
| 是否直接写 config | ✅ 否 — 但 `apply_param_updates()` 是遗留的写路径（无人调用）|
| 是否会改边界 | ❌ 不能 — boundary_config.json 只由 risk_guard 写入 |
| 是否有记忆 | ✅ 部分 — `learning_memory.json` 记录策略/币种/模式表现 |
| 是否有因果回溯 | ✅ 是 — `param_attribution.start_tracking()` + `feed_trade()` |
| 样本门槛是否合理 | ⚠️ 不一致 — 代码默认50，配置默认15。模拟盘冷启动需 ~15 笔才触发 |
| 当前状态 | 🔴 **FROZEN** — 连续99错误，冻结到2027年，永不自动恢复 |

---

## 8. 数据账本状态

| 项目 | 结论 |
|---|---|
| 当前唯一成交账本 | `data/strategy_trades.jsonl` — 由 `strategy_stats.record_trade()` 写入 |
| 是否有重复写入 | ❌ 无 — 每个 JSONL 文件只有一个写入者 |
| 内存列表 vs 文件 | ⚠️ `scalp_trade_history`（内存）+ `strategy_trades.jsonl`（文件）并存，web 面板读内存，Evolver 读文件 |
| 清空风险 | ⚠️ Web UI 「一键清空复盘」按钮清空内存 `scalp_trade_history`，但不影响 `strategy_trades.jsonl` |
| 启动后恢复能力 | ✅ settings.json / evolver_state.json / strategy_trades.jsonl 持久化，重启可恢复 |

---

## 9. UI 状态

| Tab/元素 | 状态 | 结论 |
|---|---|---|
| 超短线 (scalp) | ✅ 正常 | 主交易面板，保留 |
| 妖币 (yaobi) | ✅ 正常 | 扫描面板，保留 |
| 策略统计 (strategy) | ✅ 正常 | 含策略统计 + AI 今日复盘卡片 |
| AI 今日复盘 | ✅ 正常 | 紫色卡片，含7个分析部分 + 生成按钮 |
| Evolver 独立页 | ✅ **不存在** | 已移除，只有内联状态卡片 |
| 控制面板 (cockpit) | ✅ **不存在** | 已移除 |
| Evolver tab 按钮 | ⚠️ **存在但无内容** | line 105-108 有按钮，但无对应的 `x-show="mode==='evolver'"` div，点击显示空白 |
| 风控参数 | ✅ 已补全 | 单笔开仓/持仓/杠杆/止损/风险金额/日亏损/TP/候选/超时 + 4个开关 |
| 执行模式列 | ✅ 已修复 | 显示 PAPER/LIVE |
| 手动平仓按钮 | ✅ 已修复 | 按钮文字"手动平仓" |

**无效按钮**: 前端加载时调用 `loadFlashTrades/Reviews/Candidates` 但函数未定义 → 每分钟报错

---

## 10. 最小修复计划（按优先级）

### P0 — 必须修（影响风控或启动）

| # | 问题 | 涉及文件 | 风险 | 方案 |
|---|---|---|---|---|
| 1 | risk_guard 异常不阻断交易 | bot_scalp.py:2883-2884 | 🔴 实盘可能绕过风控 | 改为 `logger.error` + `return`，风险 guard 异常时必须安全失败 |
| 2 | _pending_entries 泄漏 | bot_scalp.py:2835 附近 | 🔴 仓位额度逐渐耗尽 | 在每个 return 路径前加 `self._pending_entries = max(0, self._pending_entries - 1)` |

### P1 — 建议修（不影响安全但影响可用性）

| # | 问题 | 涉及文件 | 风险 | 方案 |
|---|---|---|---|---|
| 3 | 前端未定义函数报错 | templates/index.html:2115-2117 | 🟡 浏览器控制台报错 | 删除 3 个 `setInterval` 调用或定义空函数 |
| 4 | Evolver 永久冻结 | evolver_runtime_state.json | 🟡 Evolver 不工作 | 解冻: 设置 `frozen=false`, `consecutive_errors=0` |
| 5 | apply_param_updates() 遗留写路径 | strategy_evolver.py:375-397 | 🟡 潜在绕过路径 | 加弃用标记或删除 |
| 6 | /api/evolver/performance import bug | web.py:1417 | 🟡 访问会崩溃 | 添加 import 或删除端点 |

### P2 — 清理（不影响运行）

| # | 问题 | 涉及文件 | 风险 | 方案 |
|---|---|---|---|---|
| 7 | 12 个未使用 API 端点 | web.py 各处 | 🟢 无 | 可删除 |
| 8 | deprecated/proposal_validator 仍在用 | strategy_evolver.py:537 | 🟢 无 | 移出 deprecated/ 或迁移逻辑 |
| 9 | autopilot_guard 与 risk_guard 重复事件 | 两个文件 | 🟢 无 | 统一事件写入 |
| 10 | 废弃前端变量 (cockpit*) | templates/index.html | 🟢 无 | 可删除 |

---

*报告结束 — 只读审计，未修改任何文件*
