# AIOps Agent v1.3 — 手动测试流程

> **自动化运行：** 所有场景可通过 `scripts/load_test.py` 执行（单个场景：`--scenario 1`，全套省略参数）：
>
> ```bash
> NO_PROXY="172.16.100.0/24" no_proxy="172.16.100.0/24" python3 scripts/load_test.py
> ```
>
> 本文档用于手动逐步验证，加深对各节点行为的理解。

## 环境信息

| 组件 | 地址 |
|------|------|
| Webhook | `http://172.16.100.234:8000` |
| PCF API | `http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList` |
| Redis | `172.16.100.91:30379` |
| Grafana | `http://172.16.100.91:31225` |
| Prometheus | `http://172.16.100.91:30504` |

**本机运行脚本需绕过代理：**
```bash
export NO_PROXY="172.16.100.0/24" no_proxy="172.16.100.0/24"
```

**Worker 日志实时监控（建议另开终端）：**
```bash
ssh dscl1 "kubectl logs -n aiops -l app=aiops-worker -f --tail=50"
```

---

## 涉及的核心流程

```
Prometheus AlertManager
      │ HTTP POST /alarm
      ▼
aiops-webhook (FastAPI)
  ├─ 解析 alert payload
  ├─ 尝试获取 Redis Lock (key: aiops:lock:{alertname}:{namespace})
  │   ├─ 已有 Lock → [DEDUP] 丢弃
  │   └─ 获取成功 → 发消息到 Kafka
      ▼
aiops-worker (Kafka Consumer)
  └─ LangGraph 状态机
       ├─ fetch_nrf_logs  → ES 查 NRF WARN 日志，提取 dropped fields
       ├─ fetch_metrics   → Prometheus 查注册失败率（namespace 过滤）
       ├─ rag_lookup      → 向量搜索分类 field_errors / unknown_fields
       │                    （仅当有 dropped fields 时触发，PLMN 场景跳过）
       ├─ decide          → LLM 决策路由
       │   ├─ update_pcf_plmn   → execute_tool
       │   ├─ fix_profile_field → execute_tool
       │   ├─ no_action         → END
       │   └─ notify_only       → notify
       ├─ execute_tool    → Safety Gate 检查 → 调 PCF API
       └─ verify_fix      → 轮询 Prometheus 确认修复生效
```

---

## S1：PLMN 白名单不匹配 → 自动修复

**验证目标：** Agent 识别非法 PLMN，LLM 调用 `update_pcf_plmn` 恢复正确值

**步骤：**

1. 确认 PCF 当前 PLMN 正常：
```bash
curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList | python3 -m json.tool | grep -A5 plmnList
```

2. 注入错误 PLMN（999/99 不在白名单）：
```bash
PROFILE=$(curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList)
PATCHED=$(echo $PROFILE | python3 -c "
import json,sys
d=json.load(sys.stdin)
p=d[0] if isinstance(d,list) else d
p['plmnList']=[{'mcc':'999','mnc':'99'}]
print(json.dumps([p]))
")
curl -s -X PUT http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList \
  -H 'Content-Type: application/json' -d "$PATCHED"
```

3. 清除 Redis Lock（防止 dedup 干扰）：
```bash
redis-cli -h 172.16.100.91 -p 30379 DEL "aiops:lock:NfRegistrationFailure:occnp2"
```

4. 发送告警：
```bash
curl -s -X POST http://172.16.100.234:8000/alarm \
  -H 'Content-Type: application/json' \
  -d '{
    "groupKey": "test-s1",
    "alerts": [{
      "status": "firing",
      "labels": {"alertname":"NfRegistrationFailure","namespace":"occnp2","NfType":"PCF"},
      "annotations": {"summary":"S1 PLMN mismatch test"}
    }]
  }'
```

5. 观察 Worker 日志，期望看到：
```
[AGENT] Starting graph for namespace=occnp2
[GRAPH] → decide  tool=update_pcf_plmn  mcc=510  mnc=011
[SAFETY] PLMN 510/011 ✓ confirmed in whitelist
[EXECUTE] update_pcf_plmn success
[VERIFY] fix verified — rate dropped
```

6. 验证 PCF 已恢复（等 ~3 分钟，verify_fix 节点轮询 Prometheus 直到注册率下降）：

```bash
curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['plmnList'])"
# 期望: [{'mcc': '510', 'mnc': '011'}]
```

**Grafana 指标验证：**
- `aiops_alerts_processed_total{outcome="auto_fixed"}` +1
- `aiops_fix_verified_total{result="success"}` +1

---

## S2：PCF 健康 → No Action

**验证目标：** PCF 正常时 Agent 不做任何修改直接结束

**步骤：**

1. 确认 PCF PLMN 为正常值（510/011）：

   ```bash
   curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList | python3 -c "import json,sys; d=json.load(sys.stdin); print('PLMN:', d[0]['plmnList'])"
   ```

2. 清除 Redis Lock：

   ```bash
   redis-cli -h 172.16.100.91 -p 30379 DEL "aiops:lock:NfRegistrationFailure:occnp2"
   ```

3. 发送告警：

   ```bash
   curl -s -X POST http://172.16.100.234:8000/alarm \
     -H 'Content-Type: application/json' \
     -d '{
       "groupKey": "test-s2",
       "alerts": [{
         "status": "firing",
         "labels": {"alertname":"NfRegistrationFailure","namespace":"occnp2","NfType":"PCF"},
         "annotations": {"summary":"S2 no-action test"}
       }]
     }'
   ```

4. 观察 Worker 日志，期望看到：

   ```text
   [GRAPH] → decide  route=end  (no_action)
   ```

   注意：没有 execute_tool，没有 PCF API 调用，直接到 END

**Grafana 指标验证：**
- `aiops_alerts_processed_total{outcome="no_action"}` +1

---

## S3：Dedup — 重复告警只处理一次

**验证目标：** 短时间内多条相同告警只有第一条进入 Agent

**步骤：**

1. 注入错误 PLMN（999/99 不在白名单）：

   ```bash
   PROFILE=$(curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList)
   PATCHED=$(echo $PROFILE | python3 -c "
   import json,sys
   d=json.load(sys.stdin)
   p=d[0] if isinstance(d,list) else d
   p['plmnList']=[{'mcc':'999','mnc':'99'}]
   print(json.dumps([p]))
   ")
   curl -s -X PUT http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList \
     -H 'Content-Type: application/json' -d "$PATCHED"
   ```

2. 清除 Redis Lock：
```bash
redis-cli -h 172.16.100.91 -p 30379 DEL "aiops:lock:NfRegistrationFailure:occnp2"
```

3. 在 **500ms 内** 连发 3 条告警（分别执行）：
```bash
# 终端快速执行 3 次，或用 &
for i in 1 2 3; do
  curl -s -X POST http://172.16.100.234:8000/alarm \
    -H 'Content-Type: application/json' \
    -d "{\"groupKey\":\"test-s3-$i\",\"alerts\":[{\"status\":\"firing\",\"labels\":{\"alertname\":\"NfRegistrationFailure\",\"namespace\":\"occnp2\",\"NfType\":\"PCF\"},\"annotations\":{\"summary\":\"S3 dedup #$i\"}}]}" &
done
wait
```

4. 观察 Worker 日志，期望：
```
[AGENT] Starting graph ...     ← 只出现 1 次
[DEDUP] Redis lock exists, skipping   ← 出现 2 次
```

5. 恢复 PCF PLMN 为 510/011：

   ```bash
   PROFILE=$(curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList)
   PATCHED=$(echo $PROFILE | python3 -c "
   import json,sys
   d=json.load(sys.stdin)
   p=d[0] if isinstance(d,list) else d
   p['plmnList']=[{'mcc':'510','mnc':'011'}]
   print(json.dumps([p]))
   ")
   curl -s -X PUT http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList \
     -H 'Content-Type: application/json' -d "$PATCHED"
   ```

**Grafana 指标验证：**
- `aiops_alerts_processed_total` 只 +1（不是 +3）

---

## S4：字段名拼写错误 → RAG 识别 → 自动修复

**验证目标：** Agent 通过 ES 日志 + RAG 向量搜索识别字段 typo 并修复

**背景：** NRF 每 ~60s 与 PCF 做一次心跳注册，若 PCF profile 有非法字段，NRF 会在 ES 里写入 WARN 日志 `dropped/ignored [xxx]`。Agent 用这条日志触发 RAG 分类。

**步骤：**

1. 注入字段 typo（nfSetIdList → nfSetIdLists）：

   ```bash
   PROFILE=$(curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList)
   PATCHED=$(echo $PROFILE | python3 -c "
   import json,sys
   d=json.load(sys.stdin)
   p=d[0] if isinstance(d,list) else d
   if 'nfSetIdList' in p:
       p['nfSetIdLists'] = p.pop('nfSetIdList')
   print(json.dumps([p]))
   ")
   curl -s -X PUT http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList \
     -H 'Content-Type: application/json' -d "$PATCHED"
   ```

2. **等待 65 秒**（NRF 心跳周期），让 NRF 生成 WARN 日志到 ES

3. 清除 Redis Lock：

   ```bash
   redis-cli -h 172.16.100.91 -p 30379 DEL "aiops:lock:NfRegistrationFailure:occnp2"
   ```

4. 发送告警：

   ```bash
   curl -s -X POST http://172.16.100.234:8000/alarm \
     -H 'Content-Type: application/json' \
     -d '{
       "groupKey": "test-s4",
       "alerts": [{
         "status": "firing",
         "labels": {"alertname":"NfRegistrationFailure","namespace":"occnp2","NfType":"PCF"},
         "annotations": {"summary":"S4 field-typo test"}
       }]
     }'
   ```

5. 观察 Worker 日志，期望：

   ```text
   [NRF] dropped fields: ['nfSetIdLists']
   [RAG] field_errors=[{'dropped_field': 'nfSetIdLists', 'correct': 'nfSetIdList'}]
   [GRAPH] → decide  tool=fix_profile_field  wrong=nfSetIdLists  correct=nfSetIdList
   [SAFETY] Field 'nfSetIdLists' ✓ confirmed in fixable_typos whitelist
   [EXECUTE] fix_profile_field success
   ```

6. 验证 PCF 字段已还原（等 ~15s）：

   ```bash
   curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList | python3 -c "
   import json,sys; d=json.load(sys.stdin); p=d[0]
   print('nfSetIdList  :', 'nfSetIdList' in p)
   print('nfSetIdLists :', 'nfSetIdLists' in p)
   "
   # 期望: nfSetIdList=True, nfSetIdLists=False
   ```

**Grafana 指标验证（S4 特有）：**
- `aiops_rag_duration_seconds_count` +1（RAG 延迟首次出现）
- `aiops_rag_chunks_returned` 有数据（≈5 chunks）

---

## S5：并发 4 条告警 — Semaphore 限流验证

**验证目标：** MAX_CONCURRENT_AGENTS=3，第 4 条告警需等待 slot 释放

**步骤：**

1. 注入错误 PLMN（同 S1）

2. 清除 4 个 namespace 的 Lock：
```bash
for ns in occnp2 test-ns-alpha test-ns-beta test-ns-gamma; do
  redis-cli -h 172.16.100.91 -p 30379 DEL "aiops:lock:NfRegistrationFailure:$ns"
done
```

3. **在 200ms 内** 并发发 4 条告警到不同 namespace：
```bash
for i in 1 2 3 4; do
  NS=$([ $i -eq 1 ] && echo "occnp2" || echo "test-ns-$([ $i -eq 2 ] && echo alpha || ([ $i -eq 3 ] && echo beta || echo gamma))")
  curl -s -X POST http://172.16.100.234:8000/alarm \
    -H 'Content-Type: application/json' \
    -d "{\"groupKey\":\"test-s5-$i\",\"alerts\":[{\"status\":\"firing\",\"labels\":{\"alertname\":\"NfRegistrationFailure\",\"namespace\":\"$NS\",\"NfType\":\"PCF\"},\"annotations\":{\"summary\":\"S5 concurrent #$i\"}}]}" &
done
wait
```

4. 观察 Worker 日志，关键点：
   - 前 3 条：立即出现 `[AGENT] Starting graph`
   - 第 4 条：有明显时间差后才出现 `[AGENT] Starting graph`（等 semaphore 释放）

5. 恢复 PCF PLMN 为 510/011

**Grafana 指标验证：**
- `aiops_alerts_processed_total` +4

---

## S6：Safety Gate 拦截 — 防 LLM 幻觉

**验证目标：** Safety Gate 阻止 Agent 执行白名单之外的字段修复

**原理：** 临时从 ConfigMap 删除 `nfSetIdLists`，让 `_FIXABLE_TYPOS` 不包含该字段。LLM 仍会尝试调用 `fix_profile_field`（RAG 有记录），但 Python 层安全检查拦截。

**步骤：**

1. 本地编辑 `k8s/aiops/agent-config.yaml`，在 `fixable_typos` 列表中注释掉 `nfSetIdLists` 一行：

   ```yaml
       fixable_typos:
       # - nfSetIdLists   # ← 注释掉，模拟该 typo 不在白名单
       - nfServiceLists
       - plmnLists
       - ipv4Address
       - NfSetIdLists
   ```

2. rsync 并 apply ConfigMap，等待自动同步（无需 rollout restart）：

   ```bash
   rsync -az /Users/geng/aiops-agent/ dscl1:~/aiops-agent/
   ssh dscl1 "kubectl apply -f ~/aiops-agent/k8s/aiops/agent-config.yaml"
   # 等 ~60s，ConfigMap 目录挂载会自动同步
   # 确认 pod 内文件已更新：
   ssh dscl1 "kubectl exec -n aiops deployment/aiops-worker -- grep -A5 fixable_typos /app/config/agent_config.yaml"
   ```

3. 注入 `nfSetIdLists` typo（同 S4 步骤 1）

4. **等待 65 秒**（NRF 心跳）

5. 清除 Redis Lock，发送告警（同 S4 步骤 3~4）

6. 观察 Worker 日志，期望：

   ```text
   [SAFETY] Field 'nfSetIdLists' not in fixable_typos whitelist — refusing.
            Approved: ['NfSetIdLists', 'ipv4Address', 'localities', 'nfServiceLists', 'plmnLists']
   ```

   **注意：** 没有 `nfSetIdLists`（因为刚注释掉了）

7. 验证 PCF 字段**未被修复**（safety gate 成功拦截）：

   ```bash
   curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList | python3 -c "
   import json,sys; d=json.load(sys.stdin); p=d[0]
   print('nfSetIdLists still present:', 'nfSetIdLists' in p)
   # 期望: True（没有被修复，因为被拦截了）
   "
   ```

8. **还原配置**（恢复 `k8s/aiops/agent-config.yaml` 中的注释，重新 rsync + apply，等 ~60s 自动同步，无需重启 Worker）

9. 手动恢复 PCF 字段（还原 nfSetIdLists → nfSetIdList）：

   ```bash
   PROFILE=$(curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList)
   FIXED=$(echo $PROFILE | python3 -c "
   import json,sys
   d=json.load(sys.stdin)
   p=d[0] if isinstance(d,list) else d
   if 'nfSetIdLists' in p:
       p['nfSetIdList'] = p.pop('nfSetIdLists')
   print(json.dumps([p]))
   ")
   curl -s -X PUT http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList \
     -H 'Content-Type: application/json' -d "$FIXED"
   ```

**Grafana 指标验证：**
- `aiops_safety_gate_rejected_total` 从 0 变为 **1**

---

## Grafana 完整核查清单

测试完成后在 Grafana（`http://172.16.100.91:31225`）逐项确认：

| Panel | 期望值 | PromQL |
|-------|--------|--------|
| Total Alerts Processed | 累计增加 | `sum(aiops_alerts_processed_total)` |
| Auto-Fixed | 增加（S1/S3/S4/S5-PCF） | `sum(aiops_alerts_processed_total{outcome="auto_fixed"})` |
| No Action | 增加（S2；S5 test-ns 因无真实 ES/Prometheus 数据） | `sum(aiops_alerts_processed_total{outcome="no_action"})` |
| Safety Gate Rejections | **1**（S6 后） | `sum(aiops_safety_gate_rejected_total)` |
| Fix Verification Rate | 100% | `sum(...success) / sum(...)` |
| E2E Duration p99 | ~60s（含 S4 等待） | histogram_quantile |
| LLM Latency p99 | ~20s | histogram_quantile |
| Total Prompt Tokens | 持续增加 | `sum(aiops_llm_tokens_total{type="prompt"})` |
| RAG Chunks (S4 后) | ≈5 chunks | histogram_quantile |
| Estimated Cost | > $0 | `sum(tokens) * price` |

---

## ConfigMap 热加载验证

**背景：** `agent-config.yaml` 以目录挂载到 pod，ConfigMap 更新后 K8s 会在 ~60s 内自动同步（symlink rotation），无需 rollout restart。

**步骤：**

1. 修改 `k8s/aiops/agent-config.yaml`（例如在 `fixable_typos` 加一个测试条目）

2. apply ConfigMap：

   ```bash
   rsync -az /Users/geng/aiops-agent/ dscl1:~/aiops-agent/
   ssh dscl1 "kubectl apply -f ~/aiops-agent/k8s/aiops/agent-config.yaml"
   ```

3. 等 ~60s，确认 pod 内文件已更新：

   ```bash
   kubectl exec -n aiops deployment/aiops-worker -- cat /app/config/agent_config.yaml | grep fixable -A 10
   ```

4. 发送一条告警，观察 Worker 日志中 `[SAFETY]` 打印的 `Approved` 列表是否包含新条目（Python 每次 alert 都重新读文件）

**注意：** 仅当 `kubectl apply` 成功后 pod 内文件才会更新；如果改了 worker.yaml 本身（如资源限制、镜像版本），仍需 rollout restart。

---

## 常用排查命令

```bash
# Worker 日志
ssh dscl1 "kubectl logs -n aiops -l app=aiops-worker --tail=100"

# Webhook 日志
ssh dscl1 "kubectl logs -n aiops -l app=aiops-webhook --tail=50"

# 查 Redis Lock 状态
redis-cli -h 172.16.100.91 -p 30379 KEYS "aiops:lock:*"

# 查 PCF 当前 profile
curl -s http://172.16.100.231:8000/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList | python3 -m json.tool

# Prometheus 查指标
curl -sg "http://172.16.100.91:30504/api/v1/query" \
  --data-urlencode 'query={__name__=~"aiops_.*"}' | python3 -m json.tool
```
