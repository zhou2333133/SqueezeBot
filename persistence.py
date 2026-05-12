"""
轻量持久化工具：安全 JSON / JSONL 写入。

无外部依赖。所有写入失败只 warning，不 raise。
"""
import json
import logging
import os
import time
import tempfile

logger = logging.getLogger(__name__)


def ensure_parent_dir(path: str) -> None:
    """确保父目录存在。"""
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning("创建目录失败 %s: %s", parent, e)


def atomic_write_json(path: str, data, *, encoding: str = "utf-8") -> bool:
    """原子写入 JSON：tmp + rename，避免写半截损坏。"""
    clear_jsonl_cache(path)
    ensure_parent_dir(path)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning("JSON 写入失败 %s: %s", path, e)
        return False


def safe_read_json(path: str, default=None) -> object:
    """安全读取 JSON，损坏时返回 default。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("JSON 读取失败 %s: %s", path, e)
        return default


def append_jsonl(path: str, record: dict) -> bool:
    """追加 JSONL 行。无文件锁，单生产者场景安全。"""
    clear_jsonl_cache(path)
    ensure_parent_dir(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception as e:
        logger.warning("JSONL 追加失败 %s: %s", path, e)
        return False


_JSONL_CACHE: dict[str, tuple[float, list[dict]]] = {}
_JSONL_CACHE_TTL = 5.0


def read_jsonl(path: str, force: bool = False) -> list[dict]:
    """读取 JSONL 文件，带内存缓存（TTL 5s）。force=True 绕过缓存。"""
    if not force:
        cached = _JSONL_CACHE.get(path)
        if cached and time.time() - cached[0] < _JSONL_CACHE_TTL:
            return cached[1]
    if not os.path.exists(path):
        _JSONL_CACHE[path] = (time.time(), [])
        return []
    result = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    result.append(json.loads(line))
    except Exception as e:
        logger.warning("JSONL 读取失败 %s: %s", path, e)
    _JSONL_CACHE[path] = (time.time(), result)
    return result


def clear_jsonl_cache(path: str | None = None) -> None:
    """清除 JSONL 缓存。path=None 清除全部。"""
    if path:
        _JSONL_CACHE.pop(path, None)
    else:
        _JSONL_CACHE.clear()
