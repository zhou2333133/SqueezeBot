import pytest


@pytest.fixture(autouse=True)
def no_runtime_side_effect_files(monkeypatch):
    import signals
    import scanner.knowledge_base as knowledge_base

    monkeypatch.setattr(signals, "_persist_ledger", lambda: None)
    monkeypatch.setattr(knowledge_base, "record_trade_feedback", lambda trade: None)
