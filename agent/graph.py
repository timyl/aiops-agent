import json
import logging
import os
import pathlib
import re
import time
import yaml
from datetime import datetime, timezone, timedelta
from langgraph.graph import StateGraph, END
from agent.state import AgentState
from tools.tool_registry import TOOL_MAP

log = logging.getLogger(__name__)

# Suppress raw HTTP client noise — we print our own curl-style lines instead
logging.getLogger("elastic_transport.transport").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

def _load_config() -> dict:
    default = pathlib.Path(__file__).parent.parent / "config" / "agent_config.yaml"
    path = pathlib.Path(os.getenv("AGENT_CONFIG_PATH", str(default)))
    with open(path) as f:
        return yaml.safe_load(f)

_NRF_RETRY_OK   = int(os.getenv("NRF_RETRY_OK_THRESHOLD",   "3"))
_NRF_RETRY_FAIL = int(os.getenv("NRF_RETRY_FAIL_THRESHOLD", "10"))

ES_URL   = os.getenv("ES_URL",         "http://172.16.100.91:30200")
PROM_URL = os.getenv("PROMETHEUS_URL", "http://172.16.100.91:30504")
PCF_URL  = os.getenv("PCF_CONFIG_URL", "http://172.16.100.231:8000")
PCF_PATH = "/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList"

# Regex to extract dropped field names from NRF WARN logs
_DROPPED_RE = re.compile(r"dropped/ignored\s+\[([^\]]+)\]", re.IGNORECASE)


def _build_fix_action(name: str, args: dict) -> str:
    """将 tool_call_name + args 转回 fix_action 字符串，供 verify_fix/notify 日志使用。"""
    if name == "update_pcf_plmn":
        return f"update_plmn:{args.get('mcc', '?')}:{args.get('mnc', '?')}"
    if name == "fix_profile_field":
        return f"fix_field:{args.get('wrong_name', '?')}:{args.get('correct_name', '?')}"
    return name  # "notify_only" or "no_action"


def _name_from_fix_action(state: "AgentState") -> str:
    """Rules 模式兼容：从 fix_action 字符串推导 tool_call_name。"""
    action = state.get("fix_action", "notify_only")
    if action.startswith("update_plmn"):
        return "update_pcf_plmn"
    if action.startswith("fix_field"):
        return "fix_profile_field"
    if action == "no_action":
        return "no_action"
    return "notify_only"


# ── Node: fetch_logs ──────────────────────────────────────────────────────────

def fetch_logs(state: AgentState) -> AgentState:
    from tools.es_tool import fetch_logs as es_fetch

    ns    = state["namespace"]
    index = f"k8s-{datetime.now(timezone.utc).strftime('%Y.%m.%d')}"
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%H:%M:%SZ")

    log.info(f"[GRAPH] → fetch_logs")
    log.info(f"[TOOL/ES]  > GET {ES_URL}/{index}/_search")
    log.info(f"[TOOL/ES]  > filter: namespace={ns}  level IN [ERROR,WARN]  @timestamp >= now-5m  (since {since})")

    t0   = time.time()
    logs = es_fetch(ns, minutes=5, max_lines=60)
    dur  = time.time() - t0

    error_lines = [l for l in logs if "ERROR" in l]
    warn_lines  = [l for l in logs if "WARN"  in l]
    log.info(f"[TOOL/ES]  < 200 OK  duration={dur:.3f}s  returned={len(logs)} lines  (ERROR={len(error_lines)}, WARN={len(warn_lines)})")

    if error_lines:
        log.info(f"[TOOL/ES]  sample error lines:")
        for line in error_lines[:3]:
            log.info(f"[TOOL/ES]    {line[:130]}")

    _existing = state.get("alert_start_time")
    _start = _existing if _existing is not None else time.time()
    return {**state, "logs": logs, "alert_start_time": _start}


# ── Node: fetch_metrics ───────────────────────────────────────────────────────

def fetch_metrics(state: AgentState) -> AgentState:
    from tools.prometheus_tool import get_nrf_registration_rate, get_nrf_response_errors, get_pcf_local_status
    from tools.pcf_tool import get_pcf_profile

    cfg = _load_config()
    ALLOWED_PLMNS = cfg["plmn"]["allowed"]

    ns = state["namespace"]
    log.info(f"[GRAPH] → fetch_metrics")

    # ── Prometheus ──
    prom_query = (f'increase(occnp_nrfclient_nw_conn_out_request_total'
                  f'{{MessageType="AutonomousNfRegistration",namespace="{ns}"}}[2m])')
    log.info(f"[TOOL/PM]  > GET {PROM_URL}/api/v1/query")
    log.info(f"[TOOL/PM]  > query: {prom_query}")

    t0     = time.time()
    rate   = get_nrf_registration_rate(ns)
    errors = get_nrf_response_errors(ns)
    status = get_pcf_local_status(ns)
    dur    = time.time() - t0

    log.info(f"[TOOL/PM]  < 200 OK  duration={dur:.3f}s")
    log.info(f"[TOOL/PM]    nrf_retry_rate={rate['value']:.1f}/2min  "
             f"(threshold=3, {'⚠ failure loop active' if rate['value'] > 3 else '✓ normal'})")
    log.info(f"[TOOL/PM]    pcf_local_status={status}")
    if errors:
        for e in errors:
            log.info(f"[TOOL/PM]    error_response: code={e['response_code']}  "
                     f"type={e['message_type']}  count={e['count']:.0f}")

    # ── PCF Config API ──
    log.info(f"[TOOL/PCF] > GET {PCF_URL}{PCF_PATH}")

    t0      = time.time()
    profile = get_pcf_profile()
    dur     = time.time() - t0
    plmn    = profile.get("plmnList", [])

    log.info(f"[TOOL/PCF] < 200 OK  duration={dur:.3f}s")
    log.info(f"[TOOL/PCF]   nfStatus={profile.get('nfStatus','?')}  plmnList={plmn}")
    allowed_str = [f"{p['mcc']}/{p['mnc']}" for p in ALLOWED_PLMNS]
    for p in plmn:
        ok = p in ALLOWED_PLMNS
        log.info(f"[TOOL/PCF]   plmn {p['mcc']}/{p['mnc']} → "
                 f"{'✓ in NRF allowed list' if ok else '✗ NOT in NRF allowed list ' + str(allowed_str)}")

    return {
        **state,
        "nrf_rate":         rate["value"],
        "nrf_errors":       errors,
        "pcf_plmn":         plmn,
        "pcf_local_status": status,
    }


# ── Node: fetch_nrf_logs ──────────────────────────────────────────────────────

def fetch_nrf_logs(state: AgentState) -> AgentState:
    from tools.es_tool import fetch_nrf_logs as es_nrf_fetch

    cfg = _load_config()
    _FIXABLE_TYPOS = set(cfg["nrf"]["fixable_typos"])
    _VENDOR_FIELDS = set(cfg["nrf"]["vendor_fields"])

    index = f"k8s-{datetime.now(timezone.utc).strftime('%Y.%m.%d')}"
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%H:%M:%SZ")

    log.info(f"[GRAPH] → fetch_nrf_logs")
    log.info(f"[TOOL/ES]  > GET {ES_URL}/{index}/_search")
    log.info(f"[TOOL/ES]  > filter: namespace=ocnrf1  level IN [ERROR,WARN]  @timestamp >= now-5m  (since {since})")

    t0       = time.time()
    nrf_logs = es_nrf_fetch(minutes=5, max_lines=60)
    dur      = time.time() - t0

    warn_lines = [l for l in nrf_logs if "WARN" in l]
    log.info(f"[TOOL/ES]  < 200 OK  duration={dur:.3f}s  returned={len(nrf_logs)} lines  (WARN={len(warn_lines)})")

    # Extract dropped field names from WARN logs, classify by severity
    fixable  = []
    vendor   = []
    unknown  = []
    for line in nrf_logs:
        m = _DROPPED_RE.search(line)
        if m:
            fields = [f.strip() for f in m.group(1).split(",")]
            for f in fields:
                if f in _FIXABLE_TYPOS:
                    fixable.append(f)
                    log.info(f"[TOOL/ES]  ⚠ PCF field typo detected (auto-fixable): '{f}'")
                elif f in _VENDOR_FIELDS:
                    vendor.append(f)
                    log.info(f"[TOOL/ES]  ℹ vendor field dropped (human review): '{f}'")
                else:
                    unknown.append(f)
                    log.info(f"[TOOL/ES]  ⚠ unknown dropped field (human review): '{f}'")
            log.info(f"[TOOL/ES]    {line[:200]}")

    all_dropped = list(set(fixable + vendor + unknown))

    if not all_dropped:
        log.info(f"[TOOL/ES]  ✓ No silent-drop WARN messages found in NRF logs")
    elif vendor and not fixable and not unknown:
        log.info(f"[TOOL/ES]  ℹ Only vendor-specific drops found — no auto-fix needed")

    # Suppress field_errors when there is an active PLMN failure loop (nrf_rate > 3).
    # A high retry rate means the current fault is hard-rejection, not silent drop.
    # The NRF 15-min window may still contain residual WARN logs from a previously
    # fixed field issue — don't let those stale logs override the PLMN diagnosis.
    nrf_rate = state.get("nrf_rate", 0)
    if fixable and nrf_rate > 3:
        log.info(f"[TOOL/ES]  ⚠ field typo(s) {fixable} found in NRF logs BUT nrf_rate={nrf_rate:.1f} > 3 "
                 f"— suppressing field_errors (residual logs from prior fix, active failure loop takes priority)")
        fixable = []

    # field_errors: only fixable typos trigger RAG + auto-fix
    # unknown_fields: dropped but not in fixable_typos — LLM must see these to call notify_only
    return {**state, "nrf_logs": nrf_logs,
            "field_errors": list(set(fixable)),
            "unknown_fields": list(set(unknown)),
            "all_dropped_fields": all_dropped}


# ── Node: rag_lookup ──────────────────────────────────────────────────────────

def rag_lookup(state: AgentState) -> AgentState:
    from tools.rag_tool import get_field_info, query_knowledge
    from agent.metrics import RAG_DURATION, RAG_CHUNKS

    field_errors   = state.get("field_errors", [])
    unknown_fields = state.get("unknown_fields", [])
    all_fields     = field_errors + unknown_fields
    if not all_fields:
        log.info(f"[GRAPH] → rag_lookup  (skipped — no dropped fields detected)")
        return {**state, "rag_context": []}

    if field_errors and unknown_fields:
        log.info(f"[GRAPH] → rag_lookup  (querying knowledge base for fixable={field_errors} unknown={unknown_fields})")
    elif field_errors:
        log.info(f"[GRAPH] → rag_lookup  (querying knowledge base for {field_errors})")
    else:
        log.info(f"[GRAPH] → rag_lookup  (querying knowledge base for unknown fields {unknown_fields} — context for notify message)")

    chunks = []
    rag_t0 = time.time()
    for field in all_fields:
        log.info(f"[LLM]   > RAG query: field name '{field}'")
        results = get_field_info(field)
        log.info(f"[LLM]   < RAG returned {len(results)} chunk(s)")
        for r in results:
            log.info(f"[LLM]     · {r[:100]}...")
        chunks.extend(results)
    RAG_DURATION.observe(time.time() - rag_t0)

    # Deduplicate
    seen = set()
    unique_chunks = []
    for c in chunks:
        if c not in seen:
            seen.add(c)
            unique_chunks.append(c)

    RAG_CHUNKS.observe(len(unique_chunks))
    return {**state, "rag_context": unique_chunks}


# ── Node: analyze ─────────────────────────────────────────────────────────────

def analyze(state: AgentState) -> AgentState:
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if api_key and api_key != "your-api-key-here":
        return _analyze_with_llm(state)
    log.info("[GRAPH] → analyze  (mode=rules, no API key)")
    return _analyze_with_rules(state)


def _analyze_with_rules(state: AgentState) -> AgentState:
    cfg = _load_config()
    ALLOWED_PLMNS = cfg["plmn"]["allowed"]

    plmn         = state["pcf_plmn"]
    rate         = state["nrf_rate"]
    field_errors = state.get("field_errors", [])

    # Silent drop fault: NRF rate normal but field errors present
    if field_errors and rate < 3:
        wrong = field_errors[0]
        # Simple rule: nfSetIdLists → nfSetIdList
        correct = wrong.rstrip("s") if wrong.endswith("s") else wrong
        return {**state,
                "root_cause": f"NRF silently dropped invalid field '{wrong}' — not a 3GPP TS 29.510 field",
                "fix_action": f"fix_field:{wrong}:{correct}", "confidence": "high"}

    if rate < 3:
        return {**state, "root_cause": "NRF retry rate is low — alert may have self-resolved",
                "fix_action": "no_action", "confidence": "high"}

    bad = [p for p in plmn if p not in ALLOWED_PLMNS]
    if bad:
        bad_str = ", ".join(f"{p['mcc']}/{p['mnc']}" for p in bad)
        return {**state,
                "root_cause": f"PCF plmnList contains PLMN(s) not in NRF allowed list: {bad_str}",
                "fix_action": "update_plmn:510:011", "confidence": "high"}

    errs = [l for l in state["logs"] if "ERROR" in l]
    return {**state,
            "root_cause": f"High retry rate ({rate:.0f}/2min) but plmnList looks valid. "
                          f"{len(errs)} ERROR lines require manual inspection.",
            "fix_action": "notify_only", "confidence": "low"}


def _analyze_with_llm(state: AgentState) -> AgentState:
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    from agent.prompts import SYSTEM_PROMPT, ANALYSIS_PROMPT
    from tools.tool_registry import TOOLS

    cfg = _load_config()
    _ALLOWED_PLMNS_STR = os.getenv(
        "ALLOWED_PLMNS",
        ",".join(f"{p['mcc']}/{p['mnc']}" for p in cfg["plmn"]["allowed"]),
    )

    log.info(f"[GRAPH] → analyze  (mode=llm)")

    # Keep only the 5 most recent distinct ERROR/WARN lines, truncated to 120 chars each
    _ew = [l for l in state["logs"] if "ERROR" in l or "WARN" in l]
    _seen: set[str] = set()
    _deduped: list[str] = []
    for _l in reversed(_ew):
        _key = _l[20:80]
        if _key not in _seen:
            _seen.add(_key)
            _deduped.append(_l[:120])
        if len(_deduped) == 5:
            break
    error_logs   = "\n".join(reversed(_deduped)) or "(no error/warn logs found)"
    nrf_logs_str = "\n".join(l[:120] for l in state.get("nrf_logs", [])[:5]) or "(no NRF WARN logs)"
    rag_ctx      = state.get("rag_context", [])
    rag_str      = "\n---\n".join(rag_ctx) if rag_ctx else "(no RAG context — PLMN fault scenario)"

    llm = ChatOpenAI(
        model="qwen-max",
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url=os.getenv("DASHSCOPE_API_HOST"),
        temperature=0,
    ).bind_tools(TOOLS)

    # Build field error context: fixable typos (RAG context available) + unknown drops (notify_only)
    _fixable  = state.get("field_errors", [])
    _unknown  = state.get("unknown_fields", [])
    _all_field_issues = _fixable + [f"{f} (unknown — cannot auto-fix)" for f in _unknown]
    field_errors_str  = _all_field_issues if _all_field_issues else "none"

    user_msg = ANALYSIS_PROMPT.format(
        alert_name=state["alert_name"],
        namespace=state["namespace"],
        nrf_rate=state["nrf_rate"],
        pcf_local_status=state["pcf_local_status"],
        pcf_plmn=state["pcf_plmn"],
        allowed_plmns=_ALLOWED_PLMNS_STR,
        retry_ok=_NRF_RETRY_OK,
        retry_fail=_NRF_RETRY_FAIL,
        field_errors=field_errors_str,
        error_logs=error_logs or "(no error/warn logs found)",
        nrf_logs=nrf_logs_str,
        rag_context=rag_str,
    )

    host = os.getenv("DASHSCOPE_API_HOST", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    div  = "─" * 60
    log.info(f"[LLM]   > POST {host}/chat/completions  model=qwen-max  temperature=0  tools={[t.name for t in TOOLS]}")
    log.info(f"[LLM]   ┌── SYSTEM PROMPT {div[:44]}")
    for line in SYSTEM_PROMPT.splitlines():
        log.info(f"[LLM]   │  {line}")
    log.info(f"[LLM]   ├── USER MESSAGE (ANALYSIS_PROMPT filled) {div[:16]}")
    for line in user_msg.splitlines():
        log.info(f"[LLM]   │  {line}")
    log.info(f"[LLM]   └{div}")

    from agent.metrics import LLM_DURATION, LLM_TOKENS

    t0       = time.time()
    response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_msg)])
    dur      = time.time() - t0
    LLM_DURATION.observe(dur)

    # Extract token counts — try usage_metadata (langchain>=0.3) then response_metadata fallback
    _usage = getattr(response, "usage_metadata", None) or {}
    _prompt_tokens     = _usage.get("input_tokens", 0)
    _completion_tokens = _usage.get("output_tokens", 0)
    if not _prompt_tokens:
        _rm = getattr(response, "response_metadata", {})
        _tu = _rm.get("token_usage", {})
        _prompt_tokens     = _tu.get("prompt_tokens", 0)
        _completion_tokens = _tu.get("completion_tokens", 0)
    if _prompt_tokens:
        LLM_TOKENS.labels(type="prompt").inc(_prompt_tokens)
    if _completion_tokens:
        LLM_TOKENS.labels(type="completion").inc(_completion_tokens)
    if not _prompt_tokens and not _completion_tokens:
        log.warning("[LLM]   tokens: usage data unavailable — LLM response missing usage_metadata and response_metadata.token_usage")
    log.info(f"[LLM]   tokens: prompt={_prompt_tokens}  completion={_completion_tokens}")

    # ── No tool_calls → fall back to rules ──────────────────────────────────
    if not response.tool_calls:
        log.warning(f"[LLM]   < {dur:.1f}s  WARNING: no tool_calls in response — falling back to rules")
        return _analyze_with_rules(state)

    tc        = response.tool_calls[0]
    tool_name = tc["name"]
    tool_args = tc["args"]
    log.info(f"[LLM]   < 200 OK  duration={dur:.1f}s")
    log.info(f"[LLM]   ── Tool Call {'─'*39}")
    log.info(f"[LLM]   tool → {tool_name}  args={tool_args}")

    # ── Parse observations/root_cause/confidence from content ───────────────
    content = (response.content or "").strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]

    root_cause = f"Tool '{tool_name}' selected by LLM"
    confidence = "medium"
    try:
        result = json.loads(content)
        log.info(f"[LLM]   ── Observations {'─'*36}")
        for i, obs in enumerate(result.get("observations", []), 1):
            log.info(f"[LLM]   [{i}] {obs}")
        log.info(f"[LLM]   ── Diagnosis {'─'*39}")
        root_cause = result.get("root_cause", root_cause)
        confidence = result.get("confidence", confidence)
        log.info(f"[LLM]   root_cause → {root_cause}")
        log.info(f"[LLM]   confidence → {confidence}")
    except Exception as e:
        log.warning(f"[LLM]   content parse failed ({e}) — using tool_call metadata as root_cause")

    log.info(f"[LLM]   tool_call  → {tool_name}({tool_args})")
    log.info(f"[LLM]   {'─'*52}")

    return {
        **state,
        "tool_call_name": tool_name,
        "tool_call_args":  tool_args,
        "fix_action":      _build_fix_action(tool_name, tool_args),  # backward compat
        "root_cause":      root_cause,
        "confidence":      confidence,
    }


# ── Node: execute_tool ────────────────────────────────────────────────────────

def execute_tool(state: AgentState) -> AgentState:
    """通用 PCF 工具执行节点，替代原 auto_fix + fix_field。"""
    cfg = _load_config()
    ALLOWED_PLMNS  = cfg["plmn"]["allowed"]
    _FIXABLE_TYPOS = set(cfg["nrf"]["fixable_typos"])

    name = state.get("tool_call_name") or ""
    args = state.get("tool_call_args") or {}

    log.info(f"[GRAPH] → execute_tool  tool={name}  args={args}")

    # Defense in depth: PLMN whitelist check — independent of LLM output
    if name == "update_pcf_plmn":
        candidate = {"mcc": args.get("mcc", ""), "mnc": args.get("mnc", "")}
        if candidate not in ALLOWED_PLMNS:
            from agent.metrics import SAFETY_GATE_REJECTED
            allowed_str = [f"{p['mcc']}/{p['mnc']}" for p in ALLOWED_PLMNS]
            log.error(f"[SAFETY] PLMN {candidate['mcc']}/{candidate['mnc']} not in whitelist — refusing. "
                      f"Allowed: {allowed_str}")
            SAFETY_GATE_REJECTED.inc()
            return {**state, "fix_applied": False,
                    "error": f"PLMN {candidate['mcc']}/{candidate['mnc']} rejected by safety whitelist"}
        log.info(f"[SAFETY] PLMN {candidate['mcc']}/{candidate['mnc']} ✓ confirmed in whitelist")

    # Defense in depth: field typo whitelist check — LLM may guess unknown field corrections
    if name == "fix_profile_field":
        wrong_name = args.get("wrong_name", "")
        if wrong_name not in _FIXABLE_TYPOS:
            from agent.metrics import SAFETY_GATE_REJECTED
            known = sorted(_FIXABLE_TYPOS)
            log.error(f"[SAFETY] Field '{wrong_name}' not in fixable_typos whitelist — refusing. "
                      f"Approved: {known}")
            SAFETY_GATE_REJECTED.inc()
            return {**state, "fix_applied": False,
                    "error": f"Field '{wrong_name}' not in approved fixable_typos list"}
        log.info(f"[SAFETY] Field '{wrong_name}' ✓ confirmed in fixable_typos whitelist")

    fn = TOOL_MAP.get(name)
    if fn is None:
        log.error(f"[EXECUTE] No implementation for tool '{name}'")
        return {**state, "fix_applied": False, "error": f"No implementation for tool: {name}"}

    log.info(f"[FIX]   > calling {name}(**{args})")
    try:
        t0 = time.time()
        fn(**args)
        dur = time.time() - t0
        log.info(f"[EXECUTE] {name} completed  duration={dur:.3f}s")
        return {**state, "fix_applied": True, "error": None}
    except Exception as e:
        log.error(f"[EXECUTE] {name} failed: {e}")
        return {**state, "fix_applied": False, "error": str(e)}


# ── Node: decide ──────────────────────────────────────────────────────────────

def decide(state: AgentState) -> str:
    name       = state.get("tool_call_name") or _name_from_fix_action(state)
    confidence = state.get("confidence", "low")

    if name == "no_action":
        from agent.metrics import ALERTS_PROCESSED, ALERT_DURATION
        log.info(f"[GRAPH] → decide  route=end  (no_action, alert self-resolved)")
        ALERTS_PROCESSED.labels(outcome="no_action").inc()
        _start = state.get("alert_start_time")
        if _start:
            ALERT_DURATION.observe(time.time() - _start)
        return END
    if name == "notify_only":
        log.info(f"[GRAPH] → decide  route=notify  (notify_only, outside auto-fix scope)")
        return "notify"
    if name in ("update_pcf_plmn", "fix_profile_field"):
        if confidence in ("high", "medium"):
            log.info(f"[GRAPH] → decide  route=execute_tool  tool={name}  confidence={confidence}")
            return "execute_tool"
    log.info(f"[GRAPH] → decide  route=notify  (tool={name}, confidence={confidence} — escalating)")
    return "notify"


# ── Node: verify_fix ──────────────────────────────────────────────────────────

def verify_fix(state: AgentState) -> AgentState:
    from tools.prometheus_tool import get_nrf_registration_rate
    from tools.pcf_tool import get_pcf_profile
    from tools.es_tool import fetch_nrf_logs as es_nrf_fetch

    if not state.get("fix_applied"):
        return {**state, "fix_verified": False}

    action = state.get("fix_action", "")

    # For silent-drop fixes: verify PCF profile has the correct key (not the wrong one)
    if action.startswith("fix_field"):
        wrong_name = action.split(":")[1]
        right_name = action.split(":")[2]
        log.info(f"[GRAPH] → verify_fix  (field fix — verifying PCF profile keys, sleeping 5s...)")
        time.sleep(5)

        profile = get_pcf_profile()
        keys    = list(profile.keys())
        log.info(f"[TOOL/PCF] < profile keys: {keys}")

        has_correct = right_name in profile
        has_wrong   = wrong_name in profile

        if has_correct and not has_wrong:
            from agent.metrics import FIX_VERIFIED, ALERTS_PROCESSED, ALERT_DURATION
            log.info(f"[VERIFY] profile has '{right_name}', '{wrong_name}' absent  ✓ Field fix confirmed")
            FIX_VERIFIED.labels(result="success").inc()
            ALERTS_PROCESSED.labels(outcome="auto_fixed").inc()
            _start = state.get("alert_start_time")
            if _start:
                ALERT_DURATION.observe(time.time() - _start)
            return {**state, "fix_verified": True}
        else:
            from agent.metrics import FIX_VERIFIED
            log.info(f"[VERIFY] '{right_name}' present={has_correct}  '{wrong_name}' present={has_wrong}  ✗ Fix incomplete")
            FIX_VERIFIED.labels(result="failure").inc()
            return {**state, "fix_verified": False}

    # For PLMN fixes: poll Prometheus rate
    prev_rate = state["nrf_rate"]
    healthy   = False

    for attempt in range(1, 4):
        wait = 30 * attempt
        log.info(f"[GRAPH] → verify_fix  attempt {attempt}/3  (sleeping {wait}s...)")
        time.sleep(wait)

        log.info(f"[TOOL/PM]  > GET {PROM_URL}/api/v1/query  (re-check retry rate)")
        rate     = get_nrf_registration_rate(state["namespace"])
        new_plmn = get_pcf_profile().get("plmnList", [])
        log.info(f"[TOOL/PM]  < nrf_retry_rate={rate['value']:.1f}/2min  (was {prev_rate:.1f} before fix)")
        log.info(f"[TOOL/PCF] < plmnList={new_plmn}")

        if rate["value"] < 3:
            log.info(f"[VERIFY] rate={rate['value']:.1f} < 3  ✓ Registration restored")
            healthy = True
            break
        elif rate["value"] < prev_rate * 0.5:
            log.info(f"[VERIFY] rate={rate['value']:.1f} (dropped >50% from {prev_rate:.1f})  ✓ Fix taking effect — registration recovering")
            healthy = True
            break
        else:
            log.info(f"[VERIFY] rate={rate['value']:.1f} >= 3  ✗ Still recovering{' — will retry' if attempt < 3 else ' — giving up'}")

    from agent.metrics import FIX_VERIFIED, ALERTS_PROCESSED, ALERT_DURATION
    FIX_VERIFIED.labels(result="success" if healthy else "failure").inc()
    if healthy:
        ALERTS_PROCESSED.labels(outcome="auto_fixed").inc()
        _start = state.get("alert_start_time")
        if _start:
            ALERT_DURATION.observe(time.time() - _start)
    return {**state, "fix_verified": healthy}


# ── Node: notify ──────────────────────────────────────────────────────────────

def notify(state: AgentState) -> AgentState:
    import requests as _requests

    sep = "═" * 52
    fix_verified = state.get("fix_verified")
    fix_applied  = state.get("fix_applied")

    # Determine escalation reason for clearer log/message
    if fix_applied and fix_verified is False:
        reason = "auto-fix applied but verification failed"
    elif state.get("confidence") == "low":
        reason = "low confidence — cannot determine safe fix"
    else:
        reason = "no applicable auto-fix for this fault"

    from agent.metrics import ALERTS_PROCESSED, ALERT_DURATION
    ALERTS_PROCESSED.labels(outcome="escalated").inc()
    _start = state.get("alert_start_time")
    if _start:
        ALERT_DURATION.observe(time.time() - _start)
    log.warning(f"[GRAPH] → notify  (human escalation — {reason})")
    log.warning(f"[ESCALATION] {sep}")
    log.warning(f"[ESCALATION] Alert     : {state['alert_name']} | ns={state['namespace']}")
    log.warning(f"[ESCALATION] Root cause: {state.get('root_cause', 'unknown')}")
    log.warning(f"[ESCALATION] Fix tried : {state.get('fix_action')}  verified={fix_verified}")
    log.warning(f"[ESCALATION] Confidence: {state.get('confidence')}")
    log.warning(f"[ESCALATION] Reason    : {reason}")
    log.warning(f"[ESCALATION] → Human operator intervention required")
    log.warning(f"[ESCALATION] {sep}")

    webhook_url = os.getenv("NOTIFY_WEBHOOK_URL", "")
    if webhook_url:
        dropped = state.get("all_dropped_fields", [])
        dropped_str = (", ".join(f"`{f}`" for f in dropped)) if dropped else "none detected"
        text = (
            f":rotating_light: *AIOps — Human Escalation Required*\n"
            f">*Alert:* `{state['alert_name']}` | ns=`{state['namespace']}`\n"
            f">*Root cause:* {state.get('root_cause', 'unknown')}\n"
            f">*Dropped fields:* {dropped_str}\n"
            f">*Fix tried:* `{state.get('fix_action')}` | verified=`{fix_verified}`\n"
            f">*Confidence:* `{state.get('confidence')}`\n"
            f">*Reason:* {reason}"
        )
        try:
            resp = _requests.post(webhook_url, json={"text": text}, timeout=5)
            log.info(f"[NOTIFY] Slack notified  status={resp.status_code}")
        except Exception as e:
            log.error(f"[NOTIFY] Slack notification failed: {e}")

    return state


# ── Build graph ───────────────────────────────────────────────────────────────

def build_graph():
    g = StateGraph(AgentState)
    g.add_node("fetch_logs",     fetch_logs)
    g.add_node("fetch_metrics",  fetch_metrics)
    g.add_node("fetch_nrf_logs", fetch_nrf_logs)
    g.add_node("rag_lookup",     rag_lookup)
    g.add_node("analyze",        analyze)
    g.add_node("execute_tool",   execute_tool)
    g.add_node("verify_fix",     verify_fix)
    g.add_node("notify",         notify)

    g.set_entry_point("fetch_logs")
    g.add_edge("fetch_logs",     "fetch_metrics")
    g.add_edge("fetch_metrics",  "fetch_nrf_logs")
    g.add_edge("fetch_nrf_logs", "rag_lookup")
    g.add_edge("rag_lookup",     "analyze")
    g.add_conditional_edges("analyze", decide, {
        "execute_tool": "execute_tool",
        "notify":       "notify",
        END:            END,
    })
    g.add_edge("execute_tool", "verify_fix")
    g.add_conditional_edges("verify_fix",
        lambda s: "notify" if not s.get("fix_verified") else END,
        {"notify": "notify", END: END})
    g.add_edge("notify", END)
    return g.compile()


graph = build_graph()
