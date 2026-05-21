# Function Calling v1.1 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 LLM 的输出从手工解析的 `fix_action` 字符串迁移到类型安全的 `tool_calls`，新增通用 `execute_tool` 节点替换 `auto_fix` + `fix_field`，所有工具统一注册在 `tool_registry.py`。

**Architecture:** 新增 `tools/tool_registry.py`，用 LangChain `@tool` 注册 4 个工具并暴露 `TOOLS`（bind 给 LLM）和 `TOOL_MAP`（execute_tool 分发）。`_analyze_with_llm` 绑定工具后从 `response.tool_calls` 提取意图，`decide()` 读 `tool_call_name` 路由，`execute_tool` 统一执行真实 PCF 操作（含 PLMN safety gate）。Rules 模式通过兼容函数降级，无 API key 时零改动。

**Tech Stack:** Python 3.11, LangChain `langchain_core.tools.tool`, `langchain_openai.ChatOpenAI.bind_tools`, `pytest`, qwen-max via DashScope OpenAI-compatible API

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `tools/tool_registry.py` | **新增** | 4 个 @tool schema + TOOLS 列表 + TOOL_MAP 分发字典 |
| `agent/state.py` | **修改** | 新增 `tool_call_name`, `tool_call_args` 字段 |
| `agent/prompts.py` | **修改** | SYSTEM_PROMPT 去除 fix_action 格式；ANALYSIS_PROMPT 去除 JSON 响应要求 |
| `agent/graph.py` | **修改** | `_analyze_with_llm` bind_tools；新增辅助函数；新增 `execute_tool` 节点；更新 `decide()`；更新图连边；删除 `auto_fix`、`fix_field` |
| `tests/test_tool_registry.py` | **新增** | 验证 TOOLS/TOOL_MAP 结构和工具 schema |
| `tests/test_graph_nodes.py` | **新增** | 验证 `decide()` 路由 + `execute_tool` safety gate |
| `requirements-dev.txt` | **新增** | pytest |

---

## Task 1: 搭建测试基础设施

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: 创建 requirements-dev.txt**

```
pytest>=8.0.0
```

- [ ] **Step 2: 安装 pytest**

```bash
pip install pytest
```

Expected: `Successfully installed pytest-x.x.x`

- [ ] **Step 3: 创建测试目录**

```bash
mkdir -p /Users/geng/aiops-agent/tests
touch /Users/geng/aiops-agent/tests/__init__.py
```

- [ ] **Step 4: 创建 tests/conftest.py**

内容如下（添加项目根到 sys.path，避免 import 报错）：

```python
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
```

- [ ] **Step 5: 验证 pytest 可运行**

```bash
cd /Users/geng/aiops-agent && pytest tests/ -v
```

Expected: `no tests ran` 或 `0 passed`，无 ImportError

- [ ] **Step 6: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py
git commit -m "test: add pytest infrastructure"
```

---

## Task 2: 创建 tools/tool_registry.py

**Files:**
- Create: `tools/tool_registry.py`
- Create: `tests/test_tool_registry.py`

- [ ] **Step 1: 写失败测试 tests/test_tool_registry.py**

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /Users/geng/aiops-agent && pytest tests/test_tool_registry.py -v
```

Expected: `ImportError: No module named 'tools.tool_registry'`

- [ ] **Step 3: 创建 tools/tool_registry.py**

```python
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
    "update_pcf_plmn":   lambda mcc, mnc: _pcf_update_plmn(mcc, mnc),
    "fix_profile_field": lambda wrong_name, correct_name: _pcf_fix_field(wrong_name, correct_name),
}
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
cd /Users/geng/aiops-agent && pytest tests/test_tool_registry.py -v
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add tools/tool_registry.py tests/test_tool_registry.py
git commit -m "feat: add tool_registry with 4 LangChain tools"
```

---

## Task 3: 更新 agent/state.py

**Files:**
- Modify: `agent/state.py`

- [ ] **Step 1: 在 AgentState 中新增两个字段**

打开 `agent/state.py`，在 `# Fix outcome` 注释上方新增：

```python
    # Function calling result (LLM mode)
    tool_call_name: Optional[str]   # e.g. "update_pcf_plmn"
    tool_call_args: Optional[dict]  # e.g. {"mcc": "510", "mnc": "011"}
```

完整文件应为：

```python
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
    nrf_logs: list[str]
    field_errors: list[str]
    all_dropped_fields: list[str]

    # Analysis result
    root_cause: str
    fix_action: str              # kept for backward compat (rules mode + verify_fix logs)
    confidence: str
    rag_context: list[str]

    # Function calling result (LLM mode)
    tool_call_name: Optional[str]   # e.g. "update_pcf_plmn"
    tool_call_args: Optional[dict]  # e.g. {"mcc": "510", "mnc": "011"}

    # Fix outcome
    fix_applied: bool
    fix_verified: bool
    error: Optional[str]
```

- [ ] **Step 2: 确认无语法错误**

```bash
cd /Users/geng/aiops-agent && python -c "from agent.state import AgentState; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/state.py
git commit -m "feat: add tool_call_name/tool_call_args to AgentState"
```

---

## Task 4: 更新 agent/prompts.py

**Files:**
- Modify: `agent/prompts.py`

- [ ] **Step 1: 替换 SYSTEM_PROMPT 和 ANALYSIS_PROMPT**

将 `agent/prompts.py` 整体替换为：

```python
SYSTEM_PROMPT = """You are a 5G core network SRE agent specializing in NF registration diagnostics.

Your job: read the evidence provided and output a structured diagnosis, then call the appropriate tool.

Output:
- root_cause: one concise sentence describing the confirmed fault
- confidence:
    "high"   — evidence is unambiguous, single clear cause
    "medium" — most likely cause but minor uncertainty remains
    "low"    — signals conflict or insufficient evidence to determine root cause
- Call exactly one tool to express your decision:
    update_pcf_plmn(mcc, mnc)             — PCF plmnList contains a PLMN not accepted by NRF; correct value is mcc/mnc
    fix_profile_field(wrong_name, correct_name) — field name typo in PCF profile dropped silently by NRF
    notify_only(reason)                   — fault detected but outside auto-fix scope; human intervention required
    no_action(reason)                     — system is healthy; alert is stale or self-resolved
"""

ANALYSIS_PROMPT = """Analyze this NF registration incident. Think step by step.

Alert: {alert_name} | namespace: {namespace}

=== EVIDENCE ===
NRF retry rate (2min): {nrf_rate:.1f}  [healthy < {retry_ok}, active failure loop > {retry_fail}]
PCF local status: {pcf_local_status}
PCF plmnList: {pcf_plmn}
Allowed PLMNs: {allowed_plmns}
Silent-drop field errors (auto-detected from NRF logs): {field_errors}

=== PCF LOGS (ERROR/WARN) ===
{error_logs}

=== NRF LOGS (WARN) ===
{nrf_logs}

=== KNOWLEDGE BASE (retrieved for this incident) ===
{rag_context}

Analyze the evidence, then call the appropriate tool with correct parameters.
Also include in your text response:
{{
  "observations": [
    "<key fact 1 from the evidence>",
    "<key fact 2>",
    "<key fact 3 if any>"
  ],
  "root_cause": "<one concise sentence>",
  "confidence": "high" | "medium" | "low"
}}
"""
```

- [ ] **Step 2: 确认无语法错误**

```bash
cd /Users/geng/aiops-agent && python -c "from agent.prompts import SYSTEM_PROMPT, ANALYSIS_PROMPT; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/prompts.py
git commit -m "feat: update prompts for tool_calls — remove fix_action string contract"
```

---

## Task 5: 新增辅助函数到 graph.py

**Files:**
- Modify: `agent/graph.py` (在 `_FIXABLE_TYPOS` 定义之后新增两个函数)

这两个函数处理 LLM 模式与 Rules 模式的互转，放在所有节点函数之前。

- [ ] **Step 1: 在 `_VENDOR_FIELDS` 定义行之后（约第 41 行）添加两个辅助函数**

在 `_VENDOR_FIELDS = set(_cfg["nrf"]["vendor_fields"])` 这一行**之后**，紧接着添加：

```python

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
```

- [ ] **Step 2: 确认无语法错误**

```bash
cd /Users/geng/aiops-agent && python -c "from agent.graph import _build_fix_action, _name_from_fix_action; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/graph.py
git commit -m "feat: add _build_fix_action and _name_from_fix_action helpers"
```

---

## Task 6: 更新 _analyze_with_llm 使用 bind_tools

**Files:**
- Modify: `agent/graph.py` — `_analyze_with_llm` 函数

- [ ] **Step 1: 替换 `_analyze_with_llm` 函数体**

将 `agent/graph.py` 中的 `_analyze_with_llm` 函数（约第 264-343 行）替换为下方完整版本：

```python
def _analyze_with_llm(state: AgentState) -> AgentState:
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    from agent.prompts import SYSTEM_PROMPT, ANALYSIS_PROMPT
    from tools.tool_registry import TOOLS

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

    user_msg = ANALYSIS_PROMPT.format(
        alert_name=state["alert_name"],
        namespace=state["namespace"],
        nrf_rate=state["nrf_rate"],
        pcf_local_status=state["pcf_local_status"],
        pcf_plmn=state["pcf_plmn"],
        allowed_plmns=_ALLOWED_PLMNS_STR,
        retry_ok=_NRF_RETRY_OK,
        retry_fail=_NRF_RETRY_FAIL,
        field_errors=state.get("field_errors") or "none",
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

    t0       = time.time()
    response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_msg)])
    dur      = time.time() - t0

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
```

- [ ] **Step 2: 确认无语法错误**

```bash
cd /Users/geng/aiops-agent && python -c "from agent.graph import _analyze_with_llm; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agent/graph.py
git commit -m "feat: _analyze_with_llm — bind_tools, extract tool_calls from response"
```

---

## Task 7: 新增 execute_tool 节点

**Files:**
- Modify: `agent/graph.py` — 在 `decide()` 函数之前新增 `execute_tool` 函数
- Create: `tests/test_graph_nodes.py`

- [ ] **Step 1: 写失败测试 tests/test_graph_nodes.py**

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /Users/geng/aiops-agent && pytest tests/test_graph_nodes.py -v
```

Expected: `ImportError` 或 `AttributeError: module 'agent.graph' has no attribute 'execute_tool'`

- [ ] **Step 3: 在 graph.py 中新增 execute_tool 函数**

在 `decide()` 函数**之前**（约第 346 行），插入：

```python
# ── Node: execute_tool ────────────────────────────────────────────────────────

def execute_tool(state: AgentState) -> AgentState:
    """通用 PCF 工具执行节点，替代原 auto_fix + fix_field。"""
    name = state.get("tool_call_name") or ""
    args = state.get("tool_call_args") or {}

    log.info(f"[GRAPH] → execute_tool  tool={name}  args={args}")

    # Defense in depth: PLMN whitelist check — independent of LLM output
    if name == "update_pcf_plmn":
        candidate = {"mcc": args.get("mcc", ""), "mnc": args.get("mnc", "")}
        if candidate not in ALLOWED_PLMNS:
            allowed_str = [f"{p['mcc']}/{p['mnc']}" for p in ALLOWED_PLMNS]
            log.error(f"[SAFETY] PLMN {candidate['mcc']}/{candidate['mnc']} not in whitelist — refusing. "
                      f"Allowed: {allowed_str}")
            return {**state, "fix_applied": False,
                    "error": f"PLMN {candidate['mcc']}/{candidate['mnc']} rejected by safety whitelist"}
        log.info(f"[SAFETY] PLMN {candidate['mcc']}/{candidate['mnc']} ✓ confirmed in whitelist")

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
```

- [ ] **Step 4: 在 graph.py 顶部（模块级）从 tool_registry 导入 TOOL_MAP**

在 `from langgraph.graph import StateGraph, END` 这行**之后**，添加：

```python
from tools.tool_registry import TOOL_MAP
```

- [ ] **Step 5: 运行测试 — execute_tool 部分应通过（decide 部分可能还报错）**

```bash
cd /Users/geng/aiops-agent && pytest tests/test_graph_nodes.py -v -k "execute_tool"
```

Expected: `3 passed`（`test_execute_tool_*` 全部通过）

- [ ] **Step 6: Commit**

```bash
git add agent/graph.py tests/test_graph_nodes.py
git commit -m "feat: add execute_tool node with PLMN safety gate"
```

---

## Task 8: 更新 decide()

**Files:**
- Modify: `agent/graph.py` — `decide()` 函数

- [ ] **Step 1: 替换 decide() 函数**

将现有 `decide()` 函数（约第 346-362 行）整体替换为：

```python
def decide(state: AgentState) -> str:
    name       = state.get("tool_call_name") or _name_from_fix_action(state)
    confidence = state.get("confidence", "low")

    if name == "no_action":
        log.info(f"[GRAPH] → decide  route=end  (no_action, alert self-resolved)")
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
```

- [ ] **Step 2: 运行所有 decide 测试**

```bash
cd /Users/geng/aiops-agent && pytest tests/test_graph_nodes.py -v
```

Expected: `全部 10 个测试通过`

- [ ] **Step 3: Commit**

```bash
git add agent/graph.py
git commit -m "feat: update decide() to route on tool_call_name with rules-mode compat"
```

---

## Task 9: 更新 build_graph() — 图连边重构

**Files:**
- Modify: `agent/graph.py` — `build_graph()` 函数 + 删除 `auto_fix`、`fix_field` 函数

- [ ] **Step 1: 替换 build_graph() 函数**

将现有 `build_graph()` 替换为：

```python
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
```

- [ ] **Step 2: 删除 auto_fix 和 fix_field 函数**

删除 `agent/graph.py` 中的 `auto_fix()` 函数（约 367-396 行）和 `fix_field()` 函数（约 400-421 行）。这两个函数已被 `execute_tool` 完全替代。

- [ ] **Step 3: 确认 graph 可以构建**

```bash
cd /Users/geng/aiops-agent && python -c "from agent.graph import graph; print('Graph built OK')"
```

Expected: `Graph built OK`（无任何报错）

- [ ] **Step 4: 运行所有测试**

```bash
cd /Users/geng/aiops-agent && pytest tests/ -v
```

Expected: 全部测试通过

- [ ] **Step 5: Commit**

```bash
git add agent/graph.py
git commit -m "feat: replace auto_fix/fix_field with execute_tool in graph wiring"
```

---

## Task 10: 集成验证（Rules 模式）

Rules 模式不需要 LLM API，可以直接在本地验证图的全路径正确性。

**Files:** 无修改，纯验证步骤。

- [ ] **Step 1: 验证 rules 模式 PLMN 路径（无 API key）**

确保 `DASHSCOPE_API_KEY` 未设置，然后运行：

```bash
cd /Users/geng/aiops-agent && python -c "
import os
os.environ.pop('DASHSCOPE_API_KEY', None)
from agent.graph import graph
state = {
    'alert_name': 'NRFRegistrationFail',
    'namespace': '5gpcf1',
    'nf_type': 'PCF',
    'logs': ['ERROR something failed'],
    'nrf_rate': 15.0,
    'nrf_errors': [],
    'pcf_plmn': [{'mcc': '999', 'mnc': '99'}],
    'pcf_local_status': 'REGISTERED',
    'nrf_logs': [],
    'field_errors': [],
    'all_dropped_fields': [],
    'root_cause': '',
    'fix_action': '',
    'confidence': '',
    'rag_context': [],
    'tool_call_name': None,
    'tool_call_args': None,
    'fix_applied': False,
    'fix_verified': False,
    'error': None,
}
result = graph.invoke(state)
print('tool_call_name:', result.get('tool_call_name'))
print('fix_action:', result.get('fix_action'))
print('confidence:', result.get('confidence'))
"
```

Expected:
```
tool_call_name: None          # rules 模式不设置
fix_action: update_plmn:510:011
confidence: high
```

（注意：rules 模式下 `execute_tool` 会因 PCF API 不可达而报错进入 notify，这是预期行为）

- [ ] **Step 2: 验证 decide() 路由（rules 模式 compat）**

```bash
cd /Users/geng/aiops-agent && python -c "
from agent.graph import decide
from langgraph.graph import END
state = {
    'tool_call_name': None, 'tool_call_args': None,
    'fix_action': 'update_plmn:510:011', 'confidence': 'high',
}
print('route:', decide(state))
assert decide(state) == 'execute_tool', 'rules mode compat broken'
print('OK: rules mode routes to execute_tool')
"
```

Expected: `route: execute_tool` + `OK: rules mode routes to execute_tool`

- [ ] **Step 3: Commit 集成验证脚本（可选，如有临时脚本）**

如有任何临时验证脚本，清理后 commit；否则跳过此步。

```bash
git status
```

---

## Task 11: 部署到 K8s 并 end-to-end 验证

**Files:** 无代码修改，部署操作。

- [ ] **Step 1: 重新构建 Docker 镜像**

```bash
docker build -t 172.16.100.100:5000/aiops-agent:latest .
docker push 172.16.100.100:5000/aiops-agent:latest
```

Expected: `Successfully pushed`

- [ ] **Step 2: 重启 K8s Deployment**

```bash
kubectl rollout restart deployment/aiops-worker -n aiops
kubectl rollout status deployment/aiops-worker -n aiops
```

Expected: `deployment "aiops-worker" successfully rolled out`

- [ ] **Step 3: 注入测试故障，触发 Agent**

```bash
# 注入错误 PLMN
python tools/pcf_tool.py  # 或用 inject_bad_plmn() 触发故障
```

- [ ] **Step 4: 观察日志，确认 tool_calls 出现**

```bash
kubectl logs -n aiops deployment/aiops-worker -f | grep -E "\[LLM\]|\[EXECUTE\]|\[SAFETY\]"
```

Expected 关键日志行（LLM 模式）：
```
[LLM]   > POST ... tools=['update_pcf_plmn', 'fix_profile_field', 'notify_only', 'no_action']
[LLM]   tool_call  → update_pcf_plmn({'mcc': '510', 'mnc': '011'})
[GRAPH] → decide  route=execute_tool  tool=update_pcf_plmn  confidence=high
[SAFETY] PLMN 510/011 ✓ confirmed in whitelist
[EXECUTE] update_pcf_plmn completed  duration=0.XXXs
```

- [ ] **Step 5: 打 v1.1 tag 并创建 GitHub Release**

```bash
git tag v1.1
git push origin v1.1
```

然后在 GitHub 创建 Release，标题 `v1.1 — Function Calling`，说明：
- 新增 `tools/tool_registry.py`：4 个 LangChain @tool 工具注册
- `_analyze_with_llm` 改为 `bind_tools` + `response.tool_calls`
- 新增 `execute_tool` 通用节点替代 `auto_fix` + `fix_field`
- `decide()` 路由从字符串解析改为 `tool_call_name` 读取
- Rules 模式向后兼容，无 API key 时零改动

---

## 附：关键函数签名速查

| 函数 | 文件 | 签名 |
|------|------|------|
| `_build_fix_action` | graph.py | `(name: str, args: dict) -> str` |
| `_name_from_fix_action` | graph.py | `(state: AgentState) -> str` |
| `execute_tool` | graph.py | `(state: AgentState) -> AgentState` |
| `decide` | graph.py | `(state: AgentState) -> str` |
| `TOOLS` | tool_registry.py | `list[BaseTool]` — 4 items |
| `TOOL_MAP` | tool_registry.py | `dict[str, callable]` — 2 items |
