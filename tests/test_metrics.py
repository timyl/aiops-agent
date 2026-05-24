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


def test_rag_lookup_observes_metrics():
    import types, sys
    import agent.graph as g
    from agent.metrics import RAG_DURATION, RAG_CHUNKS

    fake_rag = types.ModuleType("tools.rag_tool")
    fake_rag.get_field_info = lambda field: [f"chunk_a for {field}", f"chunk_b for {field}"]
    fake_rag.query_knowledge = lambda q: []

    import unittest.mock as mock
    with mock.patch.dict("sys.modules", {"tools.rag_tool": fake_rag}):
        state = {
            "field_errors": ["nfSetIdLists"],
            "unknown_fields": [],
            "alert_start_time": None,
        }
        before = RAG_DURATION._sum.get()
        result = g.rag_lookup(state)
        after = RAG_DURATION._sum.get()

    assert after > before, "RAG_DURATION histogram was not observed"
    assert result.get("rag_context") == ["chunk_a for nfSetIdLists", "chunk_b for nfSetIdLists"]


def test_llm_tokens_labels_are_valid():
    """LLM_TOKENS counter must accept 'prompt' and 'completion' labels without error."""
    from agent.metrics import LLM_TOKENS
    LLM_TOKENS.labels(type="prompt").inc(0)
    LLM_TOKENS.labels(type="completion").inc(0)


def test_safety_gate_rejected_increments_on_unknown_field():
    """execute_tool must increment SAFETY_GATE_REJECTED when field not in fixable_typos."""
    import agent.graph as g
    from agent.metrics import SAFETY_GATE_REJECTED

    before = SAFETY_GATE_REJECTED._value.get()

    state = {
        "tool_call_name": "fix_profile_field",
        "tool_call_args": {"wrong_name": "totallyUnknownField999", "correct_name": "something"},
        "alert_start_time": None,
    }
    g.execute_tool(state)

    after = SAFETY_GATE_REJECTED._value.get()
    assert after == before + 1, f"SAFETY_GATE_REJECTED should have incremented by 1, got delta={after - before}"


def test_notify_increments_alerts_processed_escalated():
    """notify() must increment ALERTS_PROCESSED with outcome=escalated."""
    import agent.graph as g
    from agent.metrics import ALERTS_PROCESSED
    import time as _time

    before = ALERTS_PROCESSED.labels(outcome="escalated")._value.get()

    state = {
        "alert_name": "TestAlert",
        "namespace": "occnp2",
        "fix_applied": False,
        "fix_verified": None,
        "fix_action": "notify_only",
        "confidence": "low",
        "root_cause": "test",
        "all_dropped_fields": [],
        "alert_start_time": _time.time() - 2.0,
    }

    g.notify(state)

    after = ALERTS_PROCESSED.labels(outcome="escalated")._value.get()
    assert after == before + 1, "ALERTS_PROCESSED[escalated] should have incremented"


def test_metrics_endpoint_returns_prometheus_text():
    """GET /metrics must return 200 with text/plain content and Prometheus exposition format."""
    from fastapi.testclient import TestClient

    # Patch KafkaProducer so importing webhook.server doesn't try to connect to Kafka
    import unittest.mock as mock
    with mock.patch("kafka.KafkaProducer"):
        from webhook.server import app

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert b"aiops_" in resp.content or b"python_gc" in resp.content
