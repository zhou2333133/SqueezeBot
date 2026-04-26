# SqueezeBot — AI 通用索引

> 给接手的 AI 看的项目导航。读完本文件不需要再扫整个仓库就能知道：项目在哪、有什么、状态在哪、改什么文件能影响什么。
> 修改代码后请在文末 **WORKLOG** 追加一行（一句话即可）。
> 体积上限：保持本文件 < 400 行；过期内容直接删，不要堆叠。

## 1. 项目位置

- **根目录**：`C:\BOT\SqueezeBot\`
- **入口**：`run.py`（启动 MarketHub + 可选 ScalpBot + 可选 YaobiScanner + uvicorn `web:app`）
- **Web 面板**：`http://127.0.0.1:8000`（默认本机，token 可选）
- **Python 依赖**：`aiohttp / fastapi / jinja2 / python-multipart / python-dotenv / uvicorn / websockets`（见 `requirements.txt`）

## 2. 干什么的

加密合约**超短线机器人 + 闪崩做空机器人 + 妖币扫描器**。三条主线共用一套面板和数据源：
- **超短线（scalp）**：1 分钟级，两类信号 — Squeeze Hunter（爆仓反向）+ Trend Breakout（动能突破）+ 顺势回踩，下币安 U 本位合约，自带止损/分批止盈/移动止损/超时平仓。
- **闪崩（flash, V4AF）**：1H/4H 级，**纯做空妖币暴涨后**，三条件入场（24H涨幅 + 4H多头衰竭 + 1H lower high）+ 智能时间止损（每 8H 重评估，趋势没反转就续期）。**独立账户**（BINANCE_FLASH_API_KEY），与 scalp 隔离。
- **妖币扫描（yaobi）**：15 分钟级，从 GeckoTerminal/DEX Screener/币安广场抓热度 → Surf 新闻预过滤 → OKX 链上验证 → 币安合约 OI/资金费 → 打分 → AI 终审 → 写入 Obsidian + 推送到候选池。
- **关系**：妖币扫描器选币 → 候选池 → scalp 在 1m 维度等开仓点；flash 跨 1H/4H 等闪崩做空时机。

## 3. 顶层目录速查

```
C:\BOT\SqueezeBot\
├── run.py                  ← 启动入口（asyncio + uvicorn）
├── bot_state.py            ← 全局任务/实例引用（hub/scalp/yaobi）
├── config.py               ← 配置加载、settings.json 读写、密钥脱敏
├── log_manager.py          ← 日志 + 内存日志队列（面板订阅）
├── market_hub.py           ← 全局市场数据中心（Taker/LS/Basis/OKX-OI 共享缓存）
├── signals.py              ← 进程内共享状态：scalp_signals / scalp_positions / scalp_trade_history
├── bot_scalp.py            ← 超短线主体（≈2900 行，最大文件）
├── bot_flash.py            ← V4AF 闪崩做空 bot（独立账户，智能时间止损）
├── scalp_diagnostics.py    ← 交易诊断（entry_bad / direction_wrong / ...）+ 学习报告
├── trader.py               ← 币安下单封装（市价/止损/reduceOnly），支持 key 注入做多账户
├── watchlist.py            ← 人工关注/禁入池
├── web.py                  ← FastAPI 面板：所有 /api/* + /ws + 复盘包导出
├── data/                   ← settings.json + ai_knowledge/（AI 缓存、用量、反馈 jsonl）
├── logs/                   ← squeeze_bot.log
├── templates/index.html    ← 单页面板（Alpine.js + WS）
├── tests/                  ← pytest 单测
└── scanner/
    ├── candidates.py       ← Candidate dataclass + 候选池 candidates_map（80+ 字段）
    ├── yaobi_scanner.py    ← 妖币扫描主调度器（≈1800 行）
    ├── ai_gateway.py       ← OpenAI / Gemini / Anthropic / Surf 统一网关
    ├── knowledge_base.py   ← AI 缓存与知识库
    ├── obsidian.py         ← 输出 Markdown 到 Obsidian
    ├── scorer.py           ← 候选打分
    ├── provider_metrics.py ← 数据源调用计数
    └── sources/
        ├── binance_futures.py        ← Binance 合约 OI/资金费/散户LS
        ├── binance_klines.py         ← 1H/4H K 线共享缓存（V4AF 用）
        ├── binance_liquidations.py   ← 爆仓流
        ├── binance_square.py         ← 币安广场抓热度（需 cookie）
        ├── dexscreener.py            ← DEX Screener
        ├── geckoterminal.py          ← GeckoTerminal
        ├── okx_market.py             ← OKX 链上 + 大单
        ├── surf_api.py               ← Surf（新闻 + LLM）
        └── token_supply.py           ← 上币时间 + 启发式 vesting 分组（V4AF 选币）
```

## 4. 状态在哪（哪些是内存、哪些落盘）

| 类别 | 位置 | 持久化？ |
|---|---|---|
| 配置 settings | `data/settings.json` | 是 |
| 日志 | `logs/squeeze_bot.log` | 是 |
| AI 缓存/用量/反馈 | `data/ai_knowledge/{ai_cache,ai_usage,trade_feedback.jsonl}` | 是 |
| 关注池 | `data/watchlist.json`（如启用） | 是 |
| **超短线信号 / 持仓 / 成交历史** | `signals.py` 的模块全局列表，`MAX_SIGNALS=200`、`MAX_TRADES=500` | **否（重启即失）** |
| 候选池 | `scanner/candidates.py` 的 `candidates_map` dict | **否** |
| 妖币机会队列 | `candidates.py` 的 `opportunity_queue` | **否** |
| MarketHub 缓存 | `market_hub.py` 内存 dataclass | **否** |

> ⚠️ 重启会丢失所有未落盘状态。"复盘包"是从这些内存对象一次性聚合的 JSON。

## 5. 关键数据流

```
[scanner 各 source]
    ↓ upsert_candidate()
candidates_map ─────────────────────────────────────┐
    ↓ get_sorted_candidates / get_anomaly_candidates │
[yaobi_scanner] AI 终审 → set_opportunity_queue     │
    ↓                                                │
[bot_scalp] 1m 等开仓 → 开仓时 _entry_context_snapshot 拷贝 candidate_meta
    ↓ add_scalp_signal / add_scalp_trade           │
signals.py 全局列表  ─────────────────────────────────┤
                                                     ↓
                                          [web._build_scalp_analysis_pack]
                                          → /api/scalp/analysis-pack.json(.gz)
                                          → /api/scalp/analysis-pack.md(.gz)
```

## 6. 复盘包（给 AI 喂的 JSON）

- 入口：`web._build_scalp_analysis_pack(detail="slim"|"full")` → `web.py:934`
- 路由：
  - `GET /api/scalp/analysis-pack.json[?detail=full]`（默认 slim + gzip）
  - `GET /api/scalp/analysis-pack.md[?detail=full]`（默认 slim + gzip）
- **slim 模式（默认）**：体积约 50–100KB（gzip 后 < 20KB），保留交易历史、分组统计、学习报告、净值、字段说明；快照类（top_candidates / anomaly_candidates / candidate_meta）裁剪到 20 条 + 精简字段；信号样本 40 条；entry_context 仅保留决策相关 yaobi_* 字段。
- **full 模式**：原始全量，约 1MB+。仅在需要排查"为什么这个币没开仓"等深度问题时用。
- 客户端如果发了 `Accept-Encoding: gzip`，自动用 gzip 流；否则裸 JSON。

## 7. 给 AI 的"先看哪里"清单

| 想知道的事 | 看这里 |
|---|---|
| 当前/最近交易表现 | `/api/scalp/analysis-pack.json`（slim 即可）的 `策略报告` + `成交分组统计` + `学习报告` |
| 单笔交易为什么亏 | 同上 `成交明细[i].trade_diagnosis / entry_context` |
| 为什么某币没开仓 | `?detail=full` 的 `未开仓诊断.最近拦截时间线` + `候选等待原因` |
| 妖币扫描效果 | `?detail=full` 的 `妖币扫描.top_candidates / decision_cards` |
| 数据源是否正常 | `数据源诊断.provider_metrics` + `provider_metrics.py` |
| 配置/参数现值 | `策略参数`（已脱敏）或 `data/settings.json` |
| 历史日志 | `logs/squeeze_bot.log`（rotating） |

## 8. 改代码常见雷区

- `scalp_trade_history` 是模块全局，**不要在 web.py 里重新赋值**，要追加用 `add_scalp_trade()` ([signals.py:50](signals.py#L50))。
- `Candidate.to_dict()` 字段一改，前端 + obsidian + 复盘包都要跟着看 ([scanner/candidates.py:176](scanner/candidates.py#L176))。
- `_entry_context_snapshot` 字段被复盘和诊断都引用 ([bot_scalp.py:2356](bot_scalp.py#L2356))；删字段前 grep 一遍 `entry_context.get("xxx")`。
- `panel_security` 中间件对所有 `/api/*` 强制本机 + token，新加路由别忘了它会拦 ([web.py:122](web.py#L122))。
- 复盘包路由的 `Content-Disposition` 文件名前端写死了，改名要同步 [templates/index.html:835-837](templates/index.html#L835)。
- 测试入口：`pytest tests/`（无 conftest，直接跑即可）。复盘相关：`tests/test_scalp_report.py`、`tests/test_scalp_review_reset.py`。

## 9. 命令速查

```bash
# 启动
cd C:\BOT\SqueezeBot && python run.py

# 跑测试
cd C:\BOT\SqueezeBot && python -m pytest tests/ -q

# 拿 slim 复盘包（推荐给 AI）
curl -H "Accept-Encoding: gzip" http://127.0.0.1:8000/api/scalp/analysis-pack.json --output pack.json.gz

# 拿 full 复盘包（深度排查）
curl http://127.0.0.1:8000/api/scalp/analysis-pack.json?detail=full -o pack.json
```

---

## 10. V4AF 闪崩做空模块（bot_flash）— 接口契约

> 这块独立于 scalp，新会话改这块只读本节就够。

### 入口
- 启动开关：`FLASH_ENABLED` （默认 False；面板或 settings.json 设 True）
- 自启动：`run.py` 检查 `FLASH_ENABLED` → `FlashCrashBot()` → `bot.run()`
- 实例：`bot_state.flash_bot`，任务：`bot_state.flash_task`
- 主循环周期：`FLASH_SCAN_INTERVAL_SECONDS`（默认 60s）
- K 线刷新：独立后台任务，每 `FLASH_KLINE_REFRESH_SECONDS`（默认 300s）拉一次 1H+4H

### 账户隔离
- 用 `BINANCE_FLASH_API_KEY` / `BINANCE_FLASH_API_SECRET`（在 .env 设）
- **留空时回退主账户**（与 scalp 共用 BINANCE_API_KEY）— 一般要分开
- `trader.BinanceTrader(session, api_key=..., api_secret=..., label="flash")` 这个 label 只走日志，不影响下单

### 核心模块边界

| 模块 | 暴露接口 | 副作用 |
|---|---|---|
| `bot_flash.FlashCrashBot` | `run()` 主循环；`detect_entry(symbol, candidate)` 纯函数；`evaluate_continuation(pos)` 智能时间止损评估；`open_positions` dict；`snapshot_status()` | 写 `signals.flash_*` 全局列表；下单走 `trader` |
| `bot_flash.detect_4h_exhaustion(rows_4h)` | 纯函数，返回 `(bool, detail)` | 无 |
| `bot_flash.detect_1h_lower_high(rows_1h, lookback, min_drop_pct)` | 纯函数 | 无 |
| `scanner.sources.binance_klines.klines_1h / klines_4h` | `get(symbol, n)`、`peek(symbol)`、`closed_only(symbol, n)`、`update_symbols(list)`、`refresh_once(session)`、`run_loop(session, interval)` | 内存 dict 缓存，重启失数据 |
| `scanner.sources.token_supply.classify_vesting(...)` | 纯函数，返回 `(phase, eligible, ban_reason)` | 无 |
| `scanner.sources.token_supply.refresh_listing_dates(session)` | 拉 Binance exchangeInfo 1d 一次 | 写入 `_listing_cache` |
| `scanner.sources.token_supply.enrich_candidate(c, settings)` | in-place 给 candidate 注入 V4AF 字段 | 改 candidate dict |
| `signals.add_flash_signal/trade/review`、`set_flash_position` | 写全局 list / dict（MAX 200 / 500 / 500） | 内存 |

### 智能时间止损（这次的核心特性）

到 `FLASH_REVIEW_HOURS`（默认 8H）时调 `evaluate_continuation(pos)`，返回 `{"keep": bool, "reason": str, ...}`：

**继续持有的维持条件全部满足才返回 keep=True：**
1. `extension_count < FLASH_REVIEW_MAX_EXTENSIONS`（默认 2，最多续 2 次 = 总持仓 ≤ 8+4+4=16H）
2. 最新 1H 收盘价 < `peak_price`（趋势仍在 peak 下方）
3. 最近 4 根 1H 没有刷出 ≥ peak 的高点
4. 浮亏不超过 `FLASH_REVIEW_HOLD_LOSS_MAX_PCT`（默认 1.0%，亏损单不无限延期）

通过 → next_review 推迟 `FLASH_REVIEW_EXTEND_HOURS`（默认 4H）；不通过 → 平仓 `close_reason="smart_time_stop"`。

每次评估都写入 `signals.flash_review_log`，复盘包里看 `智能时间止损日志` 字段。

### Web API

| 路由 | 用途 |
|---|---|
| `GET /api/flash/status` | 运行状态 + 持仓数 + filter_stats |
| `GET /api/flash/positions` | 当前活跃持仓（含智能时间止损下次审核时间） |
| `GET /api/flash/trades?limit=50&full=false` | 历史成交（slim 默认） |
| `GET /api/flash/reviews?limit=50` | 智能时间止损评估历史 |
| `GET /api/flash/candidates` | 当前选币池（eligible / banned + 启发式 vesting 分组） |
| `GET /api/flash/analysis-pack.json[?detail=full]` | 复盘包（slim 默认 + gzip，与 scalp 同套压缩规则） |
| `DELETE /api/flash/paper/positions` | 清空 paper 持仓 |

### 测试入口

```bash
python -m unittest tests.test_token_supply tests.test_flash_signals -v
```

22 个测试：vesting 分类 6 个 + 4H 衰竭 4 个 + 1H lower high 4 个 + 智能时间止损 5 个 + 入场流程 3 个。

---

## 11. 参数现状

**真相源**：[config.py:163](config.py#L163) 的 `PROFILE_MIGRATION_DEFAULTS`（迁移强制覆盖）+ [config.py:354](config.py#L354) 的 `default_settings`（首次启动用）。两处必须同步改。
**版本**：`PROFILE_VERSION` 是迁移触发器；改了 defaults 必须 bump 它，否则旧 settings.json 不会被覆盖。
**生效**：用户在 UI 改的值会落 `data/settings.json`，覆盖 defaults；当前用户**没有** settings.json，跑的就是 defaults。

### 当前关键参数（仅复盘相关，2026-04-25 起）

| 参数 | 当前值 | 上次值 | 调整原因 | 调整日期 |
|---|---|---|---|---|
| `SCALP_TP1_RR` | 1.5 | 1.2 | 让 TP1 晚一点出，配合下面的 RATIO 多留仓位享受 runner | 2026-04-25 |
| `SCALP_TP1_RATIO` | 0.30 | 0.40 | TP3 8 笔 +63、exit_too_early/runner_missed 共留下 +40，少出 10% | 2026-04-25 |
| `SCALP_TIME_STOP_MINUTES` | 45 | 30 | 超时 2 笔吃 -46U；给回踩 setup 多 15 分钟发酵 | 2026-04-25 |
| `CONTINUATION_TAKER_MIN` | 0.58 | 0.55 | entry_bad 占亏损 75%，主动买侧门槛收紧 | 2026-04-25 |
| `CONTINUATION_MIN_PULLBACK_PCT` | 0.20 | 0.12 | 要求更深回踩，过滤毛刺 | 2026-04-25 |
| `CONTINUATION_MAX_EMA20_DEVIATION_PCT` | 4.00 | 3.00 | 4-25 收紧到 3.0 后实测开仓 0；4-26 部分回退（V4AF 接管已涨完候选） | 2026-04-26 |
| `BREAKOUT_MAX_EMA20_DEVIATION_PCT` | 3.0 | 2.0 | 同上 — 突破侧给 1% 呼吸空间 | 2026-04-26 |
| `SCALP_YAOBI_MIN_ANOMALY_SCORE` | 45 | 35 | 提高妖币背书门槛 | 2026-04-25 |

> 调参规则：每次改 defaults，**必须**(1) 同步改 `PROFILE_MIGRATION_DEFAULTS` 和 `default_settings`，(2) bump `PROFILE_VERSION`（用 `YYYYMMDDNN`），(3) 在本表追加/更新一行，(4) 看 `tests/test_config_migration.py` 是否有 assert 需要同步。

---

## WORKLOG

按时间倒序，新条目在最上面。每条一行：日期 — 改了什么 — 为什么。删除超过 1 个月或被覆盖的条目。

- **2026-04-26** — bot_scalp.py:974 `_ws_connect` 加 AsyncResolver 公共 DNS（1.1.1.1+8.8.8.8）。Why: 直连后系统 DNS 解析 fstream.binance.com 失败（"Temporary failure in name resolution"），因为系统 resolv.conf 指向 127.0.0.1（Clash DNS 端口）但 Clash 在直连模式下不解析 binance。aiodns 加入 requirements.txt。
- **2026-04-26** — bot_scalp.py:974 `_ws_connect` 改用独立 session 不走代理。Why: 用户 Linux 测试机的代理（Clash Verge / mihomo 新内核）对 fstream 多 stream + SUBSCRIBE 模式不稳，会吞 server push frame 导致 K 线 8 小时收不到 1 条，bot 永远暖不机。fapi/fstream 本地能直连（curl 200），WS 切直连后立刻恢复。REST 仍走代理（OKX/Surf/Gemini 仍可能需要代理）。
- **2026-04-26** — 部分回退 EMA20 偏离守卫（BREAKOUT 2.0→3.0, CONTINUATION 3.0→4.0）。理由：4-25 收紧后实测 21h 开仓 0，候选都是已涨 18-60% 的高偏离币；现在让 V4AF 接管这类（做空），scalp 同时给一点呼吸空间。`PROFILE_VERSION` 2026042505→2026042601。
- **2026-04-26** — V4AF 模块前端面板补完（templates/index.html 加 "💥 V4AF 闪崩" tab + /api/flash/start|stop 热启停）。
- **2026-04-25** — 新增 V4AF 闪崩做空模块（bot_flash + binance_klines + token_supply）。独立账户（BINANCE_FLASH_API_KEY），智能时间止损（8H+续期机制），slim+gzip 复盘包。22 个新测试。详见第 10 节接口契约。`PROFILE_VERSION` 2026042501→2026042502。
- **2026-04-25** — 调 7 个 scalp 策略参数（见上"参数现状"表），目标：收紧 entry_bad（占亏损 75%）+ 让赚的单子赚更久。`PROFILE_VERSION` 2026042407→2026042501。
- **2026-04-25** — 复盘包默认 slim + gzip 压缩；full 模式经 `?detail=full` 触发。1MB+ → ~20KB（gzip 后）。`web.py:934` 起。
- **2026-04-25** — 新增本索引文件。
