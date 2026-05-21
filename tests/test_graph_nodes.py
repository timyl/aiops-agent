import pytest
from unittest.mock import patch, MagicMock
from langgraph.graph import END


# ── execute_tool tests ───────────────────────────────────────────────────────

def _make_state(**overrides):
    base = {
        "alert_name": "NRFRegistrationFail", "namespace": "5gpcf1", "nf_type": "PCF",
        "logs": [], "nrf_rate": 15.0, "nrf_errors": [], "pcf_plmn": [],
        "pcf_local_status": "REGISTERED", "nrf_logs": [], "field_errors": [],
        "all_dropped_fields": [], "root_cause": "test", "fix_action": "",
        "confidence": "high", "rag_context": [],
        "tool_call_name": None, "tool_call_args": None,
        "fix_applied": False, "fix_verified": False, "error": None,
    }
    return {**base, **overrides}


def test_execute_tool_rejects_bad_plmn():
    """execute_tool must refuse a PLMN not in ALLOWED_PLMNS."""
    state = _make_state(
        tool_call_name="update_pcf_plmn",
        tool_call_args={"mcc": "999", "mnc": "99"},
    )
    from agent.graph import execute_tool
    result = execute_tool(state)
    assert result["fix_applied"] is False
    assert "safety whitelist" in result["error"]


def test_execute_tool_accepts_allowed_plmn():
    """execute_tool must proceed with an allowed PLMN."""
    from agent.graph import execute_tool
    state = _make_state(
        tool_call_name="update_pcf_plmn",
        tool_call_args={"mcc": "510", "mnc": "011"},
    )
    mock_fn = MagicMock(return_value={"plmnList": [{"mcc": "510", "mnc": "011"}]})
    with patch("agent.graph.TOOL_MAP", {"update_pcf_plmn": mock_fn}):
        result = execute_tool(state)
    assert result["fix_applied"] is True
    assert result["error"] is None
    mock_fn.assert_called_once_with(mcc="510", mnc="011")


def test_execute_tool_unknown_name_returns_error():
    state = _make_state(tool_call_name="unknown_tool", tool_call_args={})
    from agent.graph import execute_tool
    result = execute_tool(state)
    assert result["fix_applied"] is False
    assert "unknown_tool" in result["error"]


# ── decide() tests ────────────────────────────────────────────────────────────

def test_decide_routes_update_plmn_to_execute_tool():
    from agent.graph import decide
    state = _make_state(tool_call_name="update_pcf_plmn",
                        tool_call_args={"mcc": "510", "mnc": "011"},
                        confidence="high")
    assert decide(state) == "execute_tool"


def test_decide_routes_fix_field_to_execute_tool():
    from agent.graph import decide
    state = _make_state(tool_call_name="fix_profile_field",
                        tool_call_args={"wrong_name": "nfSetIdLists", "correct_name": "nfSetIdList"},
                        confidence="high")
    assert decide(state) == "execute_tool"


def test_decide_routes_notify_only_to_notify():
    from agent.graph import decide
    state = _make_state(tool_call_name="notify_only",
                        tool_call_args={"reason": "unknown fault"},
                        confidence="high")
    assert decide(state) == "notify"


def test_decide_routes_no_action_to_end():
    from agent.graph import decide
    state = _make_state(tool_call_name="no_action",
                        tool_call_args={"reason": "self-resolved"},
                        confidence="high")
    assert decide(state) == END


def test_decide_low_confidence_routes_to_notify():
    from agent.graph import decide
    state = _make_state(tool_call_name="update_pcf_plmn",
                        tool_call_args={"mcc": "510", "mnc": "011"},
                        confidence="low")
    assert decide(state) == "notify"


def test_decide_rules_mode_compat_update_plmn():
    """rules 模式：tool_call_name 为空，从 fix_action 字符串降级路由。"""
    from agent.graph import decide
    state = _make_state(tool_call_name=None, tool_call_args=None,
                        fix_action="update_plmn:510:011", confidence="high")
    assert decide(state) == "execute_tool"


def test_decide_rules_mode_compat_no_action():
    from agent.graph import decide
    state = _make_state(tool_call_name=None, tool_call_args=None,
                        fix_action="no_action", confidence="high")
    assert decide(state) == END
