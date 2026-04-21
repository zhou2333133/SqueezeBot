from __future__ import annotations

import time
from collections import defaultdict
from copy import deepcopy


_metrics: dict[str, dict] = defaultdict(lambda: {
    "calls": 0,
    "ok": 0,
    "fail": 0,
    "skipped": 0,
    "items": 0,
    "last_endpoint": "",
    "last_status": "",
    "last_reason": "",
    "last_error": "",
    "last_at": 0.0,
    "endpoints": {},
})


def record_provider_call(
    provider: str,
    endpoint: str,
    ok: bool,
    *,
    status: str | int | None = None,
    reason: str = "",
    error: str = "",
    items: int = 0,
) -> None:
    row = _metrics[provider]
    row["calls"] += 1
    row["ok" if ok else "fail"] += 1
    row["items"] += max(0, int(items or 0))
    row["last_endpoint"] = endpoint
    row["last_status"] = "" if status is None else str(status)
    row["last_reason"] = reason[:160]
    row["last_error"] = error[:160]
    row["last_at"] = time.time()

    ep = row["endpoints"].setdefault(endpoint, {"calls": 0, "ok": 0, "fail": 0, "skipped": 0, "items": 0})
    ep["calls"] += 1
    ep["ok" if ok else "fail"] += 1
    ep["items"] += max(0, int(items or 0))


def record_provider_skip(provider: str, endpoint: str, reason: str, *, items: int = 0) -> None:
    row = _metrics[provider]
    row["skipped"] += 1
    row["last_endpoint"] = endpoint
    row["last_status"] = "skipped"
    row["last_reason"] = reason[:160]
    row["last_at"] = time.time()

    ep = row["endpoints"].setdefault(endpoint, {"calls": 0, "ok": 0, "fail": 0, "skipped": 0, "items": 0})
    ep["skipped"] += 1
    ep["items"] += max(0, int(items or 0))


def provider_metrics_snapshot(provider: str | None = None) -> dict:
    if provider:
        return deepcopy(_metrics.get(provider, {}))
    return deepcopy(dict(_metrics))

