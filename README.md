# SqueezeBot — 加密合约超短线交易机器人

中文安装与使用指南。

## 系统要求

- Python 3.11+
- Windows / Linux
- Binance U 本位合约 API（可只用模拟盘）

## 快速安装

```bash
# 1. 克隆仓库
git clone https://github.com/zhou2333133/SqueezeBot.git
cd SqueezeBot

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
# 复制 .env.example 为 .env，填入你的 API Key
cp .env.example .env
```

## 环境变量配置

编辑 `.env` 文件，以下为必填项：

```ini
# Binance 合约 API（必填，即使模拟盘也需要）
BINANCE_API_KEY="你的API_KEY"
BINANCE_API_SECRET="你的API_SECRET"

# MiniMax AI（用于 AI 复盘报告，可选但建议配置）
MINIMAX_API_KEY="你的MINIMAX_KEY"

# Web 面板访问
PANEL_HOST="127.0.0.1"
PANEL_PORT="8000"
```

完整配置项见 `.env.example`。

## 启动

```bash
python run.py
```

启动后打开 http://127.0.0.1:8000

首次使用建议先在 UI 中开启「模拟开仓」模式。

## 页面说明

| Tab | 功能 |
|---|---|
| ⚡ 超短线 | 1 分钟级信号交易面板。显示当前持仓、信号、历史成交、策略参数 |
| 🔍 妖币扫描 | 15 分钟级候选币扫描。从链上/合约数据发现机会 |
| 📊 策略统计 | 按策略标签聚合成交统计 + AI 今日复盘卡片 |

### AI 今日复盘

策略统计页面底部的复盘卡片，分两块：

1. **LLM 生成报告** — 点击「生成复盘」按钮，调用 MiniMax M2.7 生成中文分析
2. **结构化数据** — 自动展示最近成交、规则选择器拦截统计、风控拦截记录、Evolver 状态

## 架构概要

```
信号检测 → market_state 市场状态 → rule_selector 规则选择
  → risk_guard 硬风控 → execution_router 执行器
  → 成交记录 → learning_memory 学习记忆 → Evolver 参数进化
```

- **market_state**: 纯规则判断市场阶段，不拦截交易
- **rule_selector**: 软判断，按历史表现允许/warn/block，不影响订单参数
- **risk_guard**: 硬风控（金额/仓位/杠杆/亏损），执行前最后一道闸
- **execution_router**: 统一 PAPER/LIVE 执行，不夹带策略判断
- **Evolver**: 根据历史表现提 proposal，经 risk_guard 审核后写配置

## 模拟盘 vs 实盘

- 模拟盘（PAPER）：不实际下单，虚拟成交，适合测试策略
- 实盘（LIVE）：真实下单交易

切换方式：修改 `config/boundary_config.json` 中的 `execution_mode` 字段。

**建议流程**：先在模拟盘运行积累足够成交 → 确认策略正常 → 再切换实盘。

## 数据文件

所有数据存储在 `data/` 目录下：

| 文件 | 说明 |
|---|---|
| `strategy_trades.jsonl` | 唯一成交账本 |
| `learning_memory.json` | 按候选来源×币种×市场状态×规则聚合的历史表现 |
| `rule_selector_events.jsonl` | 规则选择器决策日志 |
| `guard_events.jsonl` | 风控事件日志 |
| `param_patches.jsonl` | Evolver 参数修改记录 |

## 常见问题

**Q: 启动后 Web 页面打不开？**
检查端口是否被占用，或修改 `.env` 中的 `PANEL_PORT`。

**Q: 模拟盘没有成交？**
检查网络是否能连接 `fapi.binance.com:443` 和 `fstream.binance.com:443`。
如果 API 连续失败，风控熔断器会自动暂停开仓。

**Q: 保存参数报错？**
安装 `python-multipart`：
```bash
pip install python-multipart
```
