"""
策略策略层：读取策略启停和权重，应用到候选信号。

职责：
  - 读取 STRATEGY_ENABLED / STRATEGY_WEIGHTS
  - 判断策略是否允许执行
  - 返回权重用于决策参考
  - 不影响每笔开仓金额和最大持仓数量

架构原则：
  - PAPER 和 LIVE 共用同一套 strategy_policy
  - execution_backend 不能参与判断
"""
import logging
from typing import Any

from config import config_manager

logger = logging.getLogger(__name__)

# 策略标签映射（中文→英文key）
_TAG_TO_KEY = {
    "启动型": "STARTUP",
    "OI爆发": "OI_EXPLOSION",
    "静默建仓": "QUIET_ACCUM",
    "突破前夜": "PRE_BREAKOUT",
    "早期启动": "EARLY_START",
}

_KEY_TO_TAG = {v: k for k, v in _TAG_TO_KEY.items()}

_ALL_KEYS = ["STARTUP", "OI_EXPLOSION", "QUIET_ACCUM", "PRE_BREAKOUT", "EARLY_START", "UNKNOWN"]


def normalize_strategy_tag(strategy_tag: str) -> str:
    """规范化策略标签：中文→英文 key。"""
    if not strategy_tag:
        return "UNKNOWN"
    # 已经是英文
    if strategy_tag.upper() in _ALL_KEYS:
        return strategy_tag.upper()
    # 中文→英文
    return _TAG_TO_KEY.get(strategy_tag, "UNKNOWN")


def denormalize_strategy_tag(eng_key: str) -> str:
    """英文 key → 中文标签（用于配置/展示）。"""
    return _KEY_TO_TAG.get(eng_key, eng_key)


def _cfg_key(eng_key: str, prefix: str) -> str:
    """生成配置 key，如 STRATEGY_ENABLED.STARTUP"""
    return f"{prefix}.{eng_key}"


def get_strategy_enabled(strategy_tag: str, cfg: dict | None = None) -> bool:
    """判断策略是否启用。"""
    if cfg is None:
        cfg = config_manager.settings
    eng_key = normalize_strategy_tag(strategy_tag)
    key = f"STRATEGY_ENABLED.{eng_key}"
    val = cfg.get(key)
    if val is not None:
        return bool(val)
    # fallback: 旧版平铺 key
    fallback = f"STRATEGY_ENABLED_{eng_key}"
    if fallback in cfg:
        return bool(cfg[fallback])
    return True  # 默认启用


def get_strategy_weight(strategy_tag: str, cfg: dict | None = None) -> float:
    """获取策略权重。1.0 = 正常，<1.0 = 降权，>1.0 = 升权。"""
    if cfg is None:
        cfg = config_manager.settings
    eng_key = normalize_strategy_tag(strategy_tag)
    key = f"STRATEGY_WEIGHTS.{eng_key}"
    val = cfg.get(key)
    if val is not None:
        return max(0.0, float(val))
    # fallback: 旧版平铺 key
    fallback = f"STRATEGY_WEIGHT_{eng_key}"
    if fallback in cfg:
        return max(0.0, float(cfg[fallback]))
    return 1.0


def default_strategy_policy() -> dict:
    """返回默认策略策略。"""
    return {
        "strategy_tag": "",
        "enabled": True,
        "weight": 1.0,
        "action": "ALLOW",
        "reason": "",
    }


def apply_strategy_policy(
    strategy_tag: str,
    signal: dict | None = None,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """应用策略策略到信号。返回 policy_result dict。"""
    if cfg is None:
        cfg = config_manager.settings
    eng_key = normalize_strategy_tag(strategy_tag)
    enabled = get_strategy_enabled(strategy_tag, cfg)
    weight = get_strategy_weight(strategy_tag, cfg)

    result: dict[str, Any] = {
        "strategy_tag": eng_key,
        "enabled": enabled,
        "weight": weight,
    }

    if not enabled:
        result["action"] = "BLOCK"
        result["reason"] = "STRATEGY_DISABLED_BY_EVOLVER"
        return result

    result["action"] = "ALLOW"
    result["reason"] = ""

    if signal is not None and weight != 1.0:
        # 将权重写入 signal 以供后续参考（不改仓位）
        signal["strategy_weight"] = weight

    return result


def get_enabled_strategies(cfg: dict | None = None) -> list[str]:
    """返回当前启用的策略列表。"""
    if cfg is None:
        cfg = config_manager.settings
    enabled = []
    for eng_key in _ALL_KEYS:
        if get_strategy_enabled(denormalize_strategy_tag(eng_key), cfg):
            enabled.append(eng_key)
    return enabled


def is_last_strategy(eng_key: str, cfg: dict | None = None) -> bool:
    """检查是否只剩这一个启用策略。"""
    if cfg is None:
        cfg = config_manager.settings
    enabled = get_enabled_strategies(cfg)
    return len(enabled) == 1 and eng_key in enabled


def set_config_value(cfg: dict, dotted_key: str, value: Any) -> None:
    """支持 dotted key 写入配置。如 STRATEGY_WEIGHTS.STARTUP=0.7。"""
    cfg[dotted_key] = value


# ══════════════════════════════════════════════════════════════════════════════
# 权重准入逻辑
# ══════════════════════════════════════════════════════════════════════════════

def apply_strategy_weight_to_signal(signal: dict, strategy_tag: str, cfg: dict | None = None) -> dict:
    """让 STRATEGY_WEIGHTS 真实影响信号准入。
    
    逻辑：
      - weight = 0 → BLOCK
      - weight < 1.0 → 降低通过率（提高准入要求）
      - weight > 1.0 → 提高通过率（降低准入要求）
      - weight = 1.0 → 正常
    """
    if cfg is None:
        cfg = config_manager.settings
    eng_key = normalize_strategy_tag(strategy_tag)
    weight = get_strategy_weight(strategy_tag, cfg)
    enabled = get_strategy_enabled(strategy_tag, cfg)

    result = {
        "raw_score": None,
        "weight": weight,
        "weighted_score": None,
        "required_score": None,
        "action": "ALLOW",
        "reason": "",
    }

    if not enabled:
        result["action"] = "BLOCK"
        result["reason"] = "STRATEGY_DISABLED_BY_EVOLVER"
        return result

    if weight <= 0:
        result["action"] = "BLOCK"
        result["reason"] = "STRATEGY_WEIGHT_ZERO"
        return result

    # 尝试从 signal/intent 中找到可用分数
    raw_score = _extract_raw_score(signal)
    result["raw_score"] = raw_score

    if raw_score is not None:
        weighted_score = round(raw_score * weight, 2)
        result["weighted_score"] = weighted_score
        # 基础要求分 = 50（无其他分数时保守基线）
        base_required = 50
        # 权重越低，required 越高
        result["required_score"] = base_required

        if weighted_score < base_required:
            result["action"] = "BLOCK"
            result["reason"] = f"WEIGHTED_SCORE_BELOW_REQUIRED (w={weight}, ws={weighted_score} < r={base_required})"
            return result
    else:
        # 无分数：用 weight 做准入概率采样
        # weight >= 1.0 → 全部 ALLOW
        # weight < 1.0 → 概率拒绝
        if weight < 1.0:
            # 暂时只记录，不强制 BLOCK（未来可改为概率采样）
            result["reason"] = f"NO_SCORE_WEIGHT_RECORDED_ONLY (w={weight})"
        else:
            result["reason"] = "NO_SCORE_WEIGHT_NEUTRAL"

    return result


def _extract_raw_score(signal: dict) -> float | None:
    """从 signal 中提取最接近的分数。"""
    candidates = [
        signal.get("score"),
        signal.get("confidence"),
        signal.get("tier_score"),
        signal.get("signal_score"),
        signal.get("opportunity_score"),
        signal.get("strategy_confidence"),
    ]
    for c in candidates:
        try:
            v = float(c) if c is not None else None
            if v is not None and 0 <= v <= 100:
                return v
        except (TypeError, ValueError):
            continue
    return None
