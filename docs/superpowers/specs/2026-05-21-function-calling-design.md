# Function Calling 架构设计（v1.1）

**日期：** 2026-05-21  
**范围：** AIOps Agent — 改进1：LLM tool_calls 替代 fix_action 字符串  
**方案：** B2 — tool_registry 模块 + execute_tool 通用节点

---

## 1. 背景与目标

### 现状问题

当前 `_analyze_with_llm` 要求 LLM 输出一个固定格式的字符串：

```
fix_action: "update_plmn:510:011"
fix_action: "fix_field:nfSetIdLists:nfSetIdList"
```

`decide()` 和 `auto_fix` / `fix_field` 节点通过 `string.split(":")` 解析参数。  
这种设计存在以下问题：

- **类型不安全**：参数以字符串形式传递，无法在运行前验证
- **Prompt 维护负担**：SYSTEM_PROMPT 必须精确描述字符串格式，格式改变时 prompt 和代码要同步修改
- **扩展性差**：新增 PCF API 操作需要在多处（prompt、string 解析、新图节点）同时修改
- **无法体现 Agent 工程能力**：面试角度，function calling 是 LLM Agent 的核心机制，当前实现没有体现

### 目标

1. LLM 通过 `tool_calls` 选择并参数化操作（类型安全，schema 驱动）
2. 新增 `tools/tool_registry.py` 作为统一的工具注册/发现/分发中心
3. 用通用 `execute_tool` 节点替换 `auto_fix` + `fix_field`，消除重复代码
4. Rules 模式（无 API key）和 LLM 模式在 `decide()` 中统一路由，向后兼容

---

## 2. 架构总览

### 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `tools/tool_registry.py` | **新增** | 4 个 @tool 注册 + TOOLS 列表 + TOOL_MAP 分发字典 |
| `agent/state.py` | **修改** | 新增 `tool_call_name`, `tool_call_args` 字段 |
| `agent/graph.py` | **修改** | analyze 绑定工具、execute_tool 节点、decide 路由更新、删除 auto_fix/fix_field |
| `agent/prompts.py` | **修改** | SYSTEM_PROMPT 删除 fix_action 格式描述，ANALYSIS_PROMPT 删除 JSON 响应格式 |

### 图结构对比

**当前（v1.0）：**
```
analyze → decide → auto_fix  → verify_fix → [notify|END]
                → fix_field  ↗
                → notify    → END
                → END
```

**新（v1.1）：**
```
analyze → decide → execute_tool → verify_fix → [notify|END]
                → notify        → END
                → END
```

---

## 3. tools/tool_registry.py（新增）

### 设计原则

- `@tool` 装饰器的 docstring 是 LLM 选择工具的依据，**必须明确描述使用场景**
- `TOOLS`：传给 `llm.bind_tools()`，LLM 可见全部 4 个意图工具
- `TOOL_MAP`：只包含真实 PCF 操作，`notify_only` / `no_action` 由 `decide()` 路由，不执行函数

### 4 个工具定义

```python
@tool
def update_pcf_plmn(mcc: str, mnc: str) -> str:
    """Update PCF plmnList to a specific PLMN.
    Use when PCF plmnList contains a PLMN not accepted by NRF.
    Args: mcc — Mobile Country Code (e.g. "510"), mnc — Mobile Network Code (e.g. "011").
    """
    ...

@tool
def fix_profile_field(wrong_name: str, correct_name: str) -> str:
    """Rename a mistyped field in PCF NF profile.
    Use when NRF silently drops a field because the field name has a typo
    that is not a valid 3GPP TS 29.510 field name.
    Args: wrong_name — current incorrect field name, correct_name — correct field name.
    """
    ...

@tool
def notify_only(reason: str) -> str:
    """Signal that a fault is detected but outside auto-fix scope.
    Use when evidence is clear but no safe automated fix exists.
    Human operator intervention is required.
    """
    ...

@tool
def no_action(reason: str) -> str:
    """Signal that the system is healthy or the alert has self-resolved.
    Use when NRF retry rate is low and no active fault is detected.
    """
    ...
```

### TOOLS 和 TOOL_MAP

```python
TOOLS = [update_pcf_plmn, fix_profile_field, notify_only, no_action]

TOOL_MAP: dict[str, callable] = {
    "update_pcf_plmn":   lambda mcc, mnc: _pcf_update_plmn(mcc, mnc),
    "fix_profile_field": lambda wrong_name, correct_name: _pcf_fix_field(wrong_name, correct_name),
}
```

---

## 4. agent/state.py 变更

新增两个字段：

```python
# Function calling 结果（LLM 模式）
tool_call_name: str   # e.g. "update_pcf_plmn"
tool_call_args: dict  # e.g. {"mcc": "510", "mnc": "011"}
```

`fix_action: str` 字段**保留**，原因：
- Rules 模式仍写入此字段
- `verify_fix` 和 `notify` 节点读此字段输出日志
- LLM 模式中由 analyze 节点从 tool_calls 反向构造，保持向后兼容

---

## 5. agent/graph.py 关键变更

### 5.1 _analyze_with_llm

**绑定工具并提取 tool_calls：**

```python
from tools.tool_registry import TOOLS

llm = ChatOpenAI(
    model="qwen-max",
    ...
    temperature=0,
).bind_tools(TOOLS)

response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_msg)])

# 提取 tool_calls
if not response.tool_calls:
    log.warning("[LLM] No tool_calls in response — falling back to rules")
    return _analyze_with_rules(state)

tc = response.tool_calls[0]
tool_name = tc["name"]
tool_args  = tc["args"]

# 从 content 解析 observations/root_cause/confidence（同 v1.0）
result = json.loads(response.content.strip())

return {
    **state,
    "tool_call_name": tool_name,
    "tool_call_args":  tool_args,
    "fix_action":      _build_fix_action(tool_name, tool_args),  # 向后兼容
    "root_cause":      result["root_cause"],
    "confidence":      result["confidence"],
}
```

### 5.2 decide()

```python
def decide(state: AgentState) -> str:
    name = state.get("tool_call_name") or _name_from_fix_action(state)
    confidence = state.get("confidence", "low")

    if name == "no_action":
        return END
    if name == "notify_only":
        return "notify"
    if name in ("update_pcf_plmn", "fix_profile_field"):
        if confidence in ("high", "medium"):
            return "execute_tool"
    return "notify"
```

### 5.3 execute_tool 节点（新增）

替换 `auto_fix` + `fix_field`，统一处理所有真实 PCF 操作：

```python
def execute_tool(state: AgentState) -> AgentState:
    from tools.tool_registry import TOOL_MAP

    name = state["tool_call_name"]
    args = state["tool_call_args"]

    log.info(f"[GRAPH] → execute_tool  tool={name}  args={args}")

    # Defense in depth: PLMN whitelist check（不依赖 LLM 输出）
    if name == "update_pcf_plmn":
        candidate = {"mcc": args["mcc"], "mnc": args["mnc"]}
        if candidate not in ALLOWED_PLMNS:
            allowed_str = [f"{p['mcc']}/{p['mnc']}" for p in ALLOWED_PLMNS]
            log.error(f"[SAFETY] PLMN {args['mcc']}/{args['mnc']} not in whitelist. Allowed: {allowed_str}")
            return {**state, "fix_applied": False,
                    "error": f"PLMN {args['mcc']}/{args['mnc']} rejected by safety whitelist"}
        log.info(f"[SAFETY] PLMN {args['mcc']}/{args['mnc']} ✓ confirmed in whitelist")

    fn = TOOL_MAP.get(name)
    if fn is None:
        log.error(f"[EXECUTE] No implementation for tool '{name}'")
        return {**state, "fix_applied": False, "error": f"No implementation for tool: {name}"}

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

### 5.4 图连边变更

```python
g.add_node("execute_tool", execute_tool)
# 删除: g.add_node("auto_fix", auto_fix)
# 删除: g.add_node("fix_field", fix_field)

g.add_conditional_edges("analyze", decide, {
    "execute_tool": "execute_tool",
    "notify":       "notify",
    END:            END,
})
g.add_edge("execute_tool", "verify_fix")
# 删除: g.add_edge("auto_fix", "verify_fix")
# 删除: g.add_edge("fix_field", "verify_fix")
```

---

## 6. agent/prompts.py 变更

### SYSTEM_PROMPT

删除原有「Output contract: fix_action: exactly one of...」，替换为：

```
Output:
- root_cause: one concise sentence describing the confirmed fault
- confidence: "high" | "medium" | "low"
- Call the appropriate tool to express your decision:
    update_pcf_plmn(mcc, mnc)     — PCF plmnList contains a PLMN not accepted by NRF
    fix_profile_field(wrong, correct) — field name typo silently dropped by NRF  
    notify_only(reason)            — fault detected, no safe auto-fix exists
    no_action(reason)              — system healthy, alert stale or self-resolved
```

### ANALYSIS_PROMPT

删除末尾「Respond in JSON: { fix_action: ... }」段，替换为：

```
Analyze the evidence step by step. Call the appropriate tool with correct parameters.
Also include in your text response:
{
  "observations": ["<key fact 1>", "<key fact 2>", "<key fact 3 if any>"],
  "root_cause": "<one concise sentence>",
  "confidence": "high" | "medium" | "low"
}
```

---

## 7. 错误处理与兼容性

### Rules 模式兼容

- `_analyze_with_rules` 继续写 `fix_action` 字符串，不改动
- `decide()` 检测 `tool_call_name` 为空时，调用内部函数 `_name_from_fix_action()` 从 fix_action 字段降级路由
- 两条路径在 `decide()` 统一汇聚

### LLM 无 tool_calls fallback

```python
if not response.tool_calls:
    log.warning("[LLM] No tool_calls in response — falling back to rules")
    return _analyze_with_rules(state)
```

### execute_tool 防御

- `TOOL_MAP` 中找不到 name → 记录错误，`fix_applied=False`，进 `verify_fix` → `notify`
- pcf_tool 抛异常（tenacity 重试耗尽）→ 同现有逻辑

---

## 8. 升级路径

| 版本 | 内容 |
|------|------|
| v1.0 | 当前：fix_action 字符串，auto_fix/fix_field 节点 |
| **v1.1** | **本设计：tool_registry + execute_tool，LLM tool_calls** |
| v1.2 | 通用化：tool_registry 按 NF 类型分组，analyze 动态 bind_tools |
| v1.3 | ReAct 多轮：execute_tool 返回 ToolMessage，analyze 循环推理 |

---

## 9. 不在本次范围

- **改进2（Prompt 模板工厂）**：ANALYSIS_PROMPT 按 NF 类型生成，待 v1.2
- **改进8（Agent metrics）**：Prometheus 指标上报，独立 task
- **多 NF 支持**：AMF/SMF/UPF tool 注册，待 v1.2
- **LangGraph ToolNode 消息流**：待 v1.3
