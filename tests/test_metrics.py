def test_metrics_module_exports_all_expected_names():
    from agent import metrics
    expected = [
        "ALERTS_PROCESSED",
        "ALERT_DURATION",
        "FIX_VERIFIED",
        "SAFETY_GATE_REJECTED",
        "LLM_DURATION",
        "LLM_TOKENS",
        "RAG_DURATION",
        "RAG_CHUNKS",
    ]
    for name in expected:
        assert hasattr(metrics, name), f"agent/metrics.py missing: {name}"


def test_alerts_processed_has_outcome_label():
    from agent.metrics import ALERTS_PROCESSED
    from prometheus_client import Counter
    assert isinstance(ALERTS_PROCESSED, Counter)
    assert "outcome" in ALERTS_PROCESSED._labelnames


def test_llm_tokens_has_type_label():
    from agent.metrics import LLM_TOKENS
    from prometheus_client import Counter
    assert isinstance(LLM_TOKENS, Counter)
    assert "type" in LLM_TOKENS._labelnames


def test_fix_verified_has_result_label():
    from agent.metrics import FIX_VERIFIED
    from prometheus_client import Counter
    assert isinstance(FIX_VERIFIED, Counter)
    assert "result" in FIX_VERIFIED._labelnames


def test_agent_state_has_alert_start_time():
    from agent.state import AgentState
    import typing
    hints = typing.get_type_hints(AgentState)
    assert "alert_start_time" in hints, "AgentState missing alert_start_time field"
