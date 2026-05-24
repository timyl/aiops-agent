from typing import TypedDict, Optional


class AgentState(TypedDict):
    # Input from alert
    alert_name: str
    namespace: str
    nf_type: str

    # Collected context — PCF side
    logs: list[str]
    nrf_rate: float
    nrf_errors: list[dict]
    pcf_plmn: list[dict]
    pcf_local_status: str

    # Collected context — NRF side (fault scenario 2)
    nrf_logs: list[str]          # WARN logs from ocnrf1 namespace
    field_errors: list[str]      # fixable typo fields (in _FIXABLE_TYPOS) → triggers RAG + auto-fix
    unknown_fields: list[str]    # dropped fields not in fixable_typos/vendor_fields → notify_only
    all_dropped_fields: list[str] # ALL dropped fields detected (fixable + vendor + unknown)

    # Analysis result
    root_cause: str              # human-readable diagnosis
    fix_action: str              # "update_plmn:<mcc>:<mnc>" | "fix_field:<wrong>:<correct>"
                                 # | "notify_only" | "no_action"
    confidence: str              # "high" | "medium" | "low"
    rag_context: list[str]       # knowledge chunks retrieved for LLM

    # Function calling result (LLM mode)
    tool_call_name: Optional[str]   # e.g. "update_pcf_plmn"
    tool_call_args: Optional[dict]  # e.g. {"mcc": "510", "mnc": "011"}

    # Fix outcome
    fix_applied: bool
    fix_verified: bool
    error: Optional[str]

    # Metrics: wall-clock time when fetch_logs started (used to compute alert_duration)
    alert_start_time: Optional[float]
