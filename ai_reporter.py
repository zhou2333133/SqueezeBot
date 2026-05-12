"""
AI 复盘日报 — 用 MiniMax 生成中文复盘报告。

职责：
  - 从 learning_memory + strategy_stats + param_attribution 聚合数据
  - 调用 MiniMax M2.7 生成结构化中文复盘
  - 持久化到 data/ai_reports.jsonl
  - 提供 latest / list 查询接口

架构原则：
  - 只读不写业务数据（报告本身写入 JSONL）
  - MiniMax 调用通过 scanner.ai_gateway._call_minimax
  - 报告是"洞察"而非"数据转储"——LLM 负责分析而非搬运
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from config import DATA_DIR, config_manager, MINIMAX_API_KEY
from persistence import append_jsonl, read_jsonl

logger = logging.getLogger(__name__)

REPORTS_FILE = os.path.join(DATA_DIR, "ai_reports.jsonl")

# ── 系统提示词 ──────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """你是一个专业的量化交易AI复盘助手。你以第一人称"我"的口吻写作（我是交易AI）。
根据提供的交易数据生成中文复盘日报。

日报应包含以下部分：
1. summary — 整体概览（今日总交易笔数、胜率、总盈亏、整体表现评价）
2. weak_analysis — 弱势币种分析（哪些币种表现差、可能原因、建议）
3. strong_analysis — 强势策略分析（哪些策略表现好、值得继续的方向）
4. pattern_insights — K线模式洞察（哪些模式赚钱/亏钱、市场状态总结）
5. param_changes — 参数修改总结（Evolver 最近修改了什么、效果如何）
6. risk_warning — 风险提示（潜在风险、需要关注的指标）
7. improvement — 改进建议（具体可操作的改进方向）

要求：
- 语言：中文
- 语气：专业、客观、第一人称
- 每部分 1-3 句话，总共不超过 400 字
- 只输出 JSON，不输出思考过程
- 如果没有数据，如实说明"暂无数据"
"""


# ══════════════════════════════════════════════════════════════════════════════
# 1. generate_report (async — FastAPI handler 可直接 await)
# ══════════════════════════════════════════════════════════════════════════════
async def generate_report() -> dict:
    """生成一篇完整的 AI 复盘日报（异步）。"""
    try:
        context = _build_context()
        if not _has_data(context):
            return _empty_report("暂无交易数据，无法生成复盘报告")

        report_text = await _call_llm(context)
        report = _parse_report(report_text, context)
        _persist_report(report)
        return report
    except Exception as e:
        logger.error("generate_report 失败: %s", e, exc_info=True)
        return _empty_report(f"生成失败: {e}")


# 同步版本（用于 CLI / 测试）
def generate_report_sync() -> dict:
    """生成复盘日报的同步封装。"""
    return asyncio.run(generate_report())


def _build_context() -> dict:
    """聚合所有数据源为 LLM 上下文。"""
    context = {}

    # learning_memory
    try:
        from learning_memory import get_memory_summary, get_weak_symbols, get_strong_strategies, get_problem_patterns
        context["memory"] = {
            "summary": get_memory_summary(),
            "weak_symbols": get_weak_symbols(),
            "strong_strategies": get_strong_strategies(),
            "problem_patterns": get_problem_patterns(),
        }
    except Exception as e:
        logger.debug("learning_memory error: %s", e)
        context["memory"] = {}

    # strategy_stats dashboard
    try:
        from strategy_stats import get_dashboard
        dashboard = get_dashboard(mode="all")
        context["dashboard"] = dashboard
    except Exception as e:
        logger.debug("strategy_stats error: %s", e)
        context["dashboard"] = {}

    # param_attribution
    try:
        from param_attribution import load_param_patches, get_active_tracking
        context["param_patches"] = load_param_patches()
        context["active_tracking"] = get_active_tracking()
    except Exception as e:
        logger.debug("param_attribution error: %s", e)
        context["param_patches"] = []
        context["active_tracking"] = []

    # evolver state
    try:
        from evolver_runtime import get_state
        state = get_state()
        context["evolver_state"] = {
            "runtime_status": state.get("runtime_status", "UNKNOWN"),
            "current_policy_version": state.get("current_policy_version", ""),
            "frozen": state.get("frozen", False),
            "rollback_count": state.get("rollback_count", 0),
        }
    except Exception as e:
        logger.debug("evolver_state error: %s", e)
        context["evolver_state"] = {}

    # config
    cfg = config_manager.settings
    context["config"] = {
        "execution_mode": cfg.get("EXECUTION_MODE", cfg.get("SCALP_PAPER_MODE", "PAPER")),
        "policy_version": cfg.get("POLICY_VERSION", "UNKNOWN"),
        "evolver_enabled": cfg.get("EVOLVER_ENABLED", False),
    }

    return context


def _has_data(context: dict) -> bool:
    """检查是否有足够数据生成报告。"""
    mem = context.get("memory", {})
    if mem.get("summary", {}).get("total_trades", 0) > 0:
        return True
    dash = context.get("dashboard", {})
    strategies = dash.get("strategies", {})
    total = sum(s.get("total", 0) for s in strategies.values())
    return total > 0


def _empty_report(reason: str) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_trades": 0,
        "summary": reason,
        "weak_analysis": "",
        "strong_analysis": "",
        "pattern_insights": "",
        "param_changes": "",
        "risk_warning": "",
        "improvement": "",
    }


async def _call_llm(context: dict) -> str:
    """调用 MiniMax 生成报告（异步）。"""
    mem = context.get("memory", {})
    dash = context.get("dashboard", {})
    patches = context.get("param_patches", [])
    tracking = context.get("active_tracking", [])
    evo = context.get("evolver_state", {})

    strategies = dash.get("strategies", {})
    strategy_lines = []
    for tag, s in strategies.items():
        strategy_lines.append(
            f"  {tag}: {s.get('total', 0)}笔 胜率{s.get('win_rate', 0):.0%} "
            f"总盈亏{s.get('total_pnl', 0):+.2f}USD"
        )

    patterns = mem.get("problem_patterns", [])
    pattern_lines = [f"  {p['pattern']}: {p['count']}次" for p in patterns[:5]]

    weak = mem.get("weak_symbols", [])
    weak_lines = [
        f"  {w['symbol']}: 胜率{w['win_rate']:.0%} "
        f"({w['trades']}笔, 盈亏{w['pnl']:+.2f}USD)"
        for w in weak[:5]
    ]

    strong = mem.get("strong_strategies", [])
    strong_lines = [
        f"  {s['strategy']}: 胜率{s['win_rate']:.0%} "
        f"({s['trades']}笔, 盈亏{s['pnl']:+.2f}USD)"
        for s in strong[:5]
    ]

    patch_lines = []
    for p in patches[-10:]:
        key = p.get("key", "")
        old = p.get("old", "")
        new = p.get("new", "")
        verdict = p.get("_verdict", "")
        status = p.get("status", "")
        line = f"  {key}: {old} -> {new} [{status}]"
        if verdict:
            line += f" 判定={verdict}"
        patch_lines.append(line)

    tracking_lines = []
    for t in tracking:
        tracking_lines.append(
            f"  {t['key']}: {t['trades_after']}笔/{_TRACK_WINDOW} "
            f"胜率{t['wins']/max(t['trades_after'],1):.0%}"
        )

    total_trades = sum(s.get("total", 0) for s in strategies.values())
    total_pnl = sum(s.get("total_pnl", 0) for s in strategies.values())
    total_wins = sum(s.get("wins", 0) for s in strategies.values())

    payload_lines = [
        f"交易概况：共{total_trades}笔，胜率{(total_wins/max(total_trades,1)):.0%}，总盈亏{total_pnl:+.2f}USD",
        f"运行模式：{context.get('config', {}).get('execution_mode', 'PAPER')}",
        f"Policy版本：{context.get('config', {}).get('policy_version', 'UNKNOWN')}",
        f"Evolver状态：{'运行中' if context.get('config', {}).get('evolver_enabled') else '已关闭'} "
        f"(运行时: {evo.get('runtime_status', 'UNKNOWN')})",
    ]

    if strategy_lines:
        payload_lines.append(f"\n策略表现：\n" + "\n".join(strategy_lines))
    if strong_lines:
        payload_lines.append(f"\n强势策略：\n" + "\n".join(strong_lines))
    if weak_lines:
        payload_lines.append(f"\n弱势币种：\n" + "\n".join(weak_lines))
    if patterns:
        payload_lines.append(f"\n高频K线模式：\n" + "\n".join(pattern_lines))
    if patch_lines:
        payload_lines.append(f"\n最近参数修改：\n" + "\n".join(patch_lines))
    if tracking_lines:
        payload_lines.append(f"\n因果追踪中：\n" + "\n".join(tracking_lines))

    payload = "\n".join(payload_lines)
    logger.info("生成 AI 复盘报告...")

    max_output = int(config_manager.settings.get("YAOBI_AI_MAX_OUTPUT_TOKENS", 2000) or 2000)
    try:
        import aiohttp
        from scanner.ai_gateway import _call_minimax

        async with aiohttp.ClientSession() as session:
            text, tokens = await _call_minimax(session, _SYSTEM_PROMPT, payload, max_output)
        logger.info("MiniMax 报告生成完成 (%d tokens)", tokens)
        return text
    except Exception as e:
        logger.error("MiniMax 调用失败: %s", e)
        raise


_TRACK_WINDOW = 15  # 对应 param_attribution._TRACK_WINDOW


# ══════════════════════════════════════════════════════════════════════════════
# 2. 解析 & 持久化
# ══════════════════════════════════════════════════════════════════════════════
def _parse_report(text: str, context: dict) -> dict:
    """解析 LLM 返回的 JSON，补全缺失字段。"""
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LLM 返回非标准 JSON，尝试提取: %.200s", text)
        # 尝试从文本中提取 JSON 块
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                report = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                report = {}
        else:
            report = {}

    # 计算实际交易数据
    dash = context.get("dashboard", {})
    strategies = dash.get("strategies", {})
    total_trades = sum(s.get("total", 0) for s in strategies.values())
    total_pnl = sum(s.get("total_pnl", 0) for s in strategies.values())

    # 保证必填字段
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "summary": report.get("summary", "") or "",
        "weak_analysis": report.get("weak_analysis", "") or "",
        "strong_analysis": report.get("strong_analysis", "") or "",
        "pattern_insights": report.get("pattern_insights", "") or "",
        "param_changes": report.get("param_changes", "") or "",
        "risk_warning": report.get("risk_warning", "") or "",
        "improvement": report.get("improvement", "") or "",
        "_raw_llm": text[:200],  # 调试用
    }
    return result


def _persist_report(report: dict) -> None:
    """追加到 ai_reports.jsonl。"""
    try:
        append_jsonl(REPORTS_FILE, report)
    except Exception as e:
        logger.warning("持久化报告失败: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 3. 查询接口
# ══════════════════════════════════════════════════════════════════════════════
def get_latest_report() -> dict | None:
    """返回最新的报告，没有则返回 None。"""
    reports = read_jsonl(REPORTS_FILE)
    if reports:
        report = reports[-1].copy()
        report.pop("_raw_llm", None)
        return report
    return None


def get_recent_reports(limit: int = 10) -> list[dict]:
    """返回最近 N 条报告。"""
    reports = read_jsonl(REPORTS_FILE)
    result = []
    for r in reports[-limit:]:
        entry = r.copy()
        entry.pop("_raw_llm", None)
        result.append(entry)
    return result
