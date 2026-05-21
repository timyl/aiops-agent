import pytest
from unittest.mock import patch, MagicMock


def test_tools_list_has_four_entries():
    from tools.tool_registry import TOOLS
    assert len(TOOLS) == 4


def test_tool_names():
    from tools.tool_registry import TOOLS
    names = {t.name for t in TOOLS}
    assert names == {"update_pcf_plmn", "fix_profile_field", "notify_only", "no_action"}


def test_update_pcf_plmn_schema_has_mcc_mnc():
    from tools.tool_registry import TOOLS
    tool = next(t for t in TOOLS if t.name == "update_pcf_plmn")
    schema = tool.args_schema.schema()
    props = schema["properties"]
    assert "mcc" in props
    assert "mnc" in props


def test_fix_profile_field_schema_has_names():
    from tools.tool_registry import TOOLS
    tool = next(t for t in TOOLS if t.name == "fix_profile_field")
    schema = tool.args_schema.schema()
    props = schema["properties"]
    assert "wrong_name" in props
    assert "correct_name" in props


def test_tool_map_has_pcf_operations():
    # pcf_tool.py imports fine — no network calls at import time
    from tools.tool_registry import TOOL_MAP
    assert "update_pcf_plmn" in TOOL_MAP
    assert "fix_profile_field" in TOOL_MAP
    assert "notify_only" not in TOOL_MAP
    assert "no_action" not in TOOL_MAP
