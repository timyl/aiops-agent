from langchain_core.tools import tool


@tool
def update_pcf_plmn(mcc: str, mnc: str) -> str:
    """Update PCF plmnList to a specific PLMN.
    Use when PCF plmnList contains a PLMN not accepted by NRF.
    Args: mcc — Mobile Country Code (e.g. "510"), mnc — Mobile Network Code (e.g. "011").
    """
    return f"update_pcf_plmn({mcc}, {mnc})"


@tool
def fix_profile_field(wrong_name: str, correct_name: str) -> str:
    """Rename a mistyped field in PCF NF profile.
    Use when NRF silently drops a field because the field name is a typo
    that is not a valid 3GPP TS 29.510 field name.
    Args: wrong_name — current incorrect field name, correct_name — correct field name.
    """
    return f"fix_profile_field({wrong_name}, {correct_name})"


@tool
def notify_only(reason: str) -> str:
    """Signal that a fault is detected but outside auto-fix scope.
    Use when evidence is clear but no safe automated fix exists.
    Human operator intervention is required.
    """
    return f"notify_only: {reason}"


@tool
def no_action(reason: str) -> str:
    """Signal that the system is healthy or the alert has self-resolved.
    Use when NRF retry rate is low and no active fault is detected.
    """
    return f"no_action: {reason}"


# LLM binding — passed to llm.bind_tools()
TOOLS = [update_pcf_plmn, fix_profile_field, notify_only, no_action]

# Dispatch map — real PCF operations only; notify_only/no_action handled by decide()
from tools.pcf_tool import update_pcf_plmn as _pcf_update_plmn
from tools.pcf_tool import fix_profile_field as _pcf_fix_field

TOOL_MAP: dict[str, callable] = {
    "update_pcf_plmn":   _pcf_update_plmn,
    "fix_profile_field": _pcf_fix_field,
}
