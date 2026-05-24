from prometheus_client import Counter, Histogram

ALERTS_PROCESSED = Counter(
    "aiops_alerts_processed_total",
    "Total alerts processed by the AIOps agent",
    ["outcome"],  # auto_fixed | escalated | no_action
)

ALERT_DURATION = Histogram(
    "aiops_alert_duration_seconds",
    "End-to-end alert processing time from first node to terminal node",
    buckets=[1, 5, 15, 30, 60, 120, 180, 300, 600],
)

FIX_VERIFIED = Counter(
    "aiops_fix_verified_total",
    "PCF fix verification results",
    ["result"],  # success | failure
)

SAFETY_GATE_REJECTED = Counter(
    "aiops_safety_gate_rejected_total",
    "Times execute_tool refused to act due to PLMN or fixable_typos whitelist check",
)

LLM_DURATION = Histogram(
    "aiops_llm_duration_seconds",
    "LLM (qwen-max) call latency",
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60],
)

LLM_TOKENS = Counter(
    "aiops_llm_tokens_total",
    "LLM token usage",
    ["type"],  # prompt | completion
)

RAG_DURATION = Histogram(
    "aiops_rag_duration_seconds",
    "RAG knowledge base query latency (per field, cumulative)",
    buckets=[0.1, 0.5, 1, 2, 5, 10],
)

RAG_CHUNKS = Histogram(
    "aiops_rag_chunks_returned",
    "Number of unique RAG chunks returned per alert",
    buckets=[0, 1, 2, 3, 5, 10],
)
