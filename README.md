# AIOps Agent — 5G NF Intelligent Operations

> An autonomous AIOps agent that monitors 5G core network NF registration failures, localizes root causes, applies configuration fixes, and verifies recovery — end-to-end in minutes.

**Stack:** LangGraph · OpenAI GPT-4o / OCI Generative AI · OCI OpenSearch RAG · Kafka · Redis · Kubernetes · Prometheus · Elasticsearch

---

## Architecture

```
AlertManager ──► Kafka (aiops-alerts topic)
                     │
                     ▼
              aiops-worker (Kafka Consumer)
              ├── Redis dedup lock (SET NX EX 300)
              └── Semaphore(3) ── LangGraph Agent
                                      │
                     ┌────────────────┼─────────────────────┐
                     ▼                ▼                     ▼
               fetch_logs      fetch_metrics        fetch_nrf_logs
                     └────────────────┼─────────────────────┘
                                      ▼
                                  rag_lookup  ──► Vector Knowledge Base
                                      ▼
                                   analyze   ──► LLM (GPT-4o / OCI Llama)
                                      ▼
                                   decide()  [deterministic routing]
                          ┌──────────┴──────────┐──────────┐
                          ▼                     ▼          ▼
                      auto_fix            fix_field    notify ──► Slack
                          └──────────┬──────────┘
                                     ▼
                               verify_fix  (3× retry, 30/60/90s backoff)
                                     ▼
                               incidents.jsonl  (audit log)
```

**Webhook flow:** AlertManager → `aiops-webhook` (FastAPI, Kafka producer) → returns 200 immediately → Kafka consumer handles async

---

## Fault Scenarios

| Scenario | Detection | Agent Action | MTTR |
|---|---|---|---|
| PLMN mismatch (`510/088` → `510/011`) | Prometheus `nrf_retry_rate > 3` | `update_plmn` via PCF REST API | ~3 min |
| Silent drop (field typo `nfSetIdLists`) | NRF WARN logs in Elasticsearch | `fix_field` via PCF REST API | ~12 sec |
| Unknown field (`capacities`) | NRF WARN logs in Elasticsearch | `notify_only` → Slack alert | immediate |
| System healthy | Prometheus `nrf_retry_rate < 3` | `no_action` → silent END | N/A |

---

## Tech Stack Decisions

| Component | Choice | Why |
|---|---|---|
| Agent framework | LangGraph | Explicit node graph + conditional edges; LLM controls diagnosis only, not routing |
| LLM | OpenAI GPT-4o / OCI Generative AI (Llama 3) | OpenAI-compatible API; pluggable — swap endpoint via env var |
| RAG | OCI OpenSearch vector index / managed KB | Provides 3GPP field name context for silent-drop detection; swappable backend |
| Message queue | Kafka KRaft | Decouples AlertManager from agent; survives burst alerts; enables multi-consumer patterns |
| Dedup lock | Redis `SET NX EX 300` | Distributed across potential multi-pod deployments; prevents duplicate runs on same alert |
| Observability | Elasticsearch + Prometheus | PCF/NRF logs go to ES, metrics to Prometheus |
| Config fix | PCF REST API (3GPP SBA) | Direct NF control plane — no manual SSH, auditable, reversible |

---

## Prompt Engineering

Three-layer separation of concerns:

```
SYSTEM_PROMPT   — role + output contract only (15 lines, stable)
ANALYSIS_PROMPT — parameterized evidence template ({allowed_plmns}, {retry_ok}, etc.)
RAG chunks      — 3GPP field knowledge (retrieved at runtime, not hardcoded)
decide()        — deterministic routing logic in Python (not LLM)
```

LLM only extracts structured diagnosis from evidence. All routing and execution is in code.

---

## Repository Structure

```
aiops-agent/
├── agent/
│   ├── graph.py          # LangGraph nodes + edges + decide() routing
│   ├── prompts.py        # SYSTEM_PROMPT + ANALYSIS_PROMPT
│   ├── state.py          # TypedDict state definition
│   ├── worker.py         # Kafka consumer + Redis dedup + Semaphore
│   └── log_fmt.py        # Structured log formatter
├── tools/
│   ├── es_tool.py        # Elasticsearch log query
│   ├── prometheus_tool.py# Prometheus metrics query
│   ├── pcf_tool.py       # PCF config read/write (3GPP SBA REST)
│   └── rag_tool.py       # RAG retrieval (OCI OpenSearch / Alibaba Bailian)
├── webhook/
│   └── server.py         # FastAPI: AlertManager → Kafka producer
├── k8s/
│   ├── aiops/            # Agent microservice manifests
│   │   ├── configmap.yaml
│   │   ├── webhook.yaml  # Deployment + ClusterIP Service
│   │   └── worker.yaml   # Deployment
│   ├── kafka/            # Kafka KRaft StatefulSet
│   ├── redis/            # Redis Deployment
│   └── alertmanager/     # AlertmanagerConfig CR + PrometheusRule
├── Dockerfile
└── requirements.txt
```

---

## Quick Start

### Prerequisites

- Kubernetes cluster with `aiops` namespace
- Kafka + Redis deployed in `aiops` namespace (see `k8s/kafka/` and `k8s/redis/`)
- Prometheus + Elasticsearch + 5G NF accessible from cluster nodes

### 1. Configure secrets

```bash
# Copy and fill in your credentials (do NOT commit this file)
cp k8s/aiops/secret.yaml.example k8s/aiops/secret.yaml
kubectl apply -f k8s/aiops/secret.yaml
```

### 2. Deploy to K8s

```bash
kubectl apply -f k8s/aiops/configmap.yaml
kubectl apply -f k8s/aiops/webhook.yaml
kubectl apply -f k8s/aiops/worker.yaml

# AlertManager routing
kubectl apply -f k8s/alertmanager/aiops-webhook.yaml
```

### 3. Verify

```bash
kubectl get pods -n aiops
kubectl logs -n aiops deploy/aiops-webhook
kubectl logs -n aiops deploy/aiops-worker -f
```

### 4. Trigger a fault (demo)

```bash
curl -X POST http://<alertmanager-host>/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"NfRegistrationFailure","namespace":"<nf-namespace>","NfType":"PCF"},"status":"firing"}]'
```

---

## Cloud Migration Path

| Self-hosted | OCI | AWS |
|---|---|---|
| Kubernetes (Kubespray) | OKE | EKS |
| Elasticsearch | OCI OpenSearch | Amazon OpenSearch |
| RAG knowledge base | OCI Generative AI + OpenSearch vector | Bedrock Knowledge Bases |
| LLM (GPT-4o / Llama) | OCI Generative AI Service | Amazon Bedrock |
| incidents.jsonl | OCI NoSQL Database | DynamoDB |
| FastAPI webhook | OCI API Gateway + Functions | API Gateway + Lambda |
| Slack notify | OCI Notifications | SNS |

---

## Key Engineering Trade-offs

**Why Kafka instead of direct webhook → agent?**  
Decouples ingestion from processing. AlertManager gets instant 200 response. Worker controls concurrency (Semaphore(3)) and deduplication (Redis). On restart, unprocessed offsets are re-consumed.

**Why Redis for dedup instead of in-memory set?**  
In-memory state is lost on pod restart. Redis survives worker restarts and works across multiple worker replicas.

**Why deterministic `decide()` instead of LLM routing?**  
LLM handles ambiguous log interpretation. Routing logic (PLMN whitelist check, confidence threshold, fix boundary) is code — testable, auditable, zero hallucination risk.

**Why separate webhook + worker pods?**  
Different resource profiles: webhook is lightweight (Kafka producer only), worker is CPU/memory intensive (LLM calls, concurrent threads). Scale independently.

---

## Deployment — `aiops` Namespace

```bash
$ kubectl get pods -n aiops -o wide
NAME                            READY   STATUS    RESTARTS   AGE   IP               NODE
aiops-webhook-869978964-klp7l   1/1     Running   0          97m   10.233.101.0     worker1
aiops-worker-7cd48b7c78-mb2v9   1/1     Running   0          57m   10.233.123.111   worker3
kafka-0                         1/1     Running   0          8h    10.233.101.4     worker1
redis-845d787d54-fdgtm          1/1     Running   0          9h    10.233.101.19    worker1

$ kubectl get svc -n aiops
NAME             TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)           AGE
aiops-webhook    ClusterIP   10.233.61.70    <none>        8000/TCP          97m
kafka            NodePort    10.233.43.141   <none>        9092:30092/TCP    8h
kafka-headless   ClusterIP   None            <none>        9092/TCP,9093/TCP 8h
redis            NodePort    10.233.44.168   <none>        6379:30379/TCP    9h

$ kubectl get deployment -n aiops
NAME            READY   UP-TO-DATE   AVAILABLE   AGE
aiops-webhook   1/1     1            1           97m
aiops-worker    1/1     1            1           97m
redis           1/1     1            1           9h
```

---

## Sample Agent Runs

### Scenario 1 — PLMN Mismatch (auto-fix in ~2 min)

Alert fires → agent fetches evidence → LLM diagnoses → PLMN corrected via PCF REST API → verified.

```
[ALERT]  NfRegistrationFailure | ns=occnp2 | NfType=PCF | status=firing
[AGENT]  Starting LangGraph agent for occnp2...

[TOOL/PM] nrf_retry_rate=20.0/2min  (healthy < 3, active failure loop > 10)
[TOOL/PCF] plmnList=[{'mcc':'510','mnc':'089'}]  local_status=PCF_LOCAL_REGISTERED
[TOOL/ES] PCF WARN — Error Response from NRF: 501 (PLMN not allowed)
[GRAPH]  → analyze  (mode=llm)

[LLM]  > POST /chat/completions  model=gpt-4o  temperature=0
[LLM]  ┌── SYSTEM PROMPT ──────────────────────────────────────────────
[LLM]  │  You are a 5G core network SRE agent specializing in NF
[LLM]  │  registration diagnostics.
[LLM]  │
[LLM]  │  Output contract:
[LLM]  │  - root_cause : one concise sentence describing the confirmed fault
[LLM]  │  - fix_action : "update_plmn:<mcc>:<mnc>"  |  "fix_field:<wrong>:<correct>"
[LLM]  │                 "notify_only"               |  "no_action"
[LLM]  │  - confidence : "high" | "medium" | "low"
[LLM]  └───────────────────────────────────────────────────────────────
[LLM]  ┌── USER MESSAGE (ANALYSIS_PROMPT) ─────────────────────────────
[LLM]  │  Alert: NfRegistrationFailure | namespace: occnp2
[LLM]  │
[LLM]  │  === EVIDENCE ===
[LLM]  │  NRF retry rate (2min): 20.0  [healthy < 3, active failure loop > 10]
[LLM]  │  PCF local status: PCF_LOCAL_REGISTERED
[LLM]  │  PCF plmnList: [{'mcc': '510', 'mnc': '089'}]
[LLM]  │  Allowed PLMNs: 510/011, 208/93, 001/01, 001/001, 505/02
[LLM]  │  Silent-drop field errors: none
[LLM]  │
[LLM]  │  === PCF LOGS (ERROR/WARN) ===
[LLM]  │  WARN nrf-client: Request rejected — PLMN 510/089 not allowed
[LLM]  │  ERROR nrf-client: Error Response received from NRF: 501
[LLM]  │
[LLM]  │  Respond in JSON: { observations, root_cause, fix_action, confidence }
[LLM]  └───────────────────────────────────────────────────────────────
[LLM]  < 200 OK  duration=4.4s
[LLM]  ── Observations ────────────────────────────────────
[LLM]  [1] NRF retry rate is 20.0, indicating an active failure loop.
[LLM]  [2] PCF plmnList contains 510/089, not in allowed list (510/011, ...).
[LLM]  [3] No silent-drop field errors detected.
[LLM]  ── Diagnosis ───────────────────────────────────────
[LLM]  root_cause → PCF plmnList contains PLMN 510/089 not accepted by NRF.
[LLM]  fix_action → update_plmn:510:011  confidence=high
[LLM]  ────────────────────────────────────────────────────

[GRAPH] → decide  route=auto_fix  (plmn mismatch, confidence=high)
[GRAPH] → auto_fix
[FIX]   > PUT /nrf-client-nfmanagement/nfProfileList
[FIX]   > body: plmnList=[{'mcc':'510','mnc':'011'}]  (was [{'mcc':'510','mnc':'089'}])
[PCF]   < 200 OK  duration=0.048s

[GRAPH] → verify_fix  attempt 1/3  (sleeping 30s...)
[VERIFY] rate=24.0 >= 3  ✗ Still recovering — will retry
[GRAPH] → verify_fix  attempt 2/3  (sleeping 60s...)
[VERIFY] rate=8.0  (dropped >50% from 20.0)  ✓ Fix taking effect

[AGENT] ===== Run Complete =====
[AGENT] Root cause  : PCF plmnList contains PLMN (510/089) not accepted by NRF.
[AGENT] Fix action  : update_plmn:510:011
[AGENT] Confidence  : high
[AGENT] Fix applied : True
[AGENT] Fix verified: True
[AUDIT] Incident saved → /data/incidents.jsonl
```

---

### Scenario 2 — Silent-Drop Field Typo (auto-fix in ~12 sec)

NRF silently drops a misspelled field; PCF thinks registration succeeded (200 OK) but NF profile is incomplete. Agent detects via NRF WARN logs + RAG lookup.

```text
[ALERT]  NfRegistrationFailure | ns=occnp2 | NfType=PCF | status=firing
[AGENT]  Starting LangGraph agent for occnp2...

[TOOL/PM]  nrf_retry_rate=0.0/2min  (no hard rejection loop)
[TOOL/PCF] plmnList=[{'mcc':'510','mnc':'011'}]  ✓ PLMN is correct
[TOOL/ES]  ⚠ PCF field typo detected (auto-fixable): 'nfSetIdLists'
[TOOL/ES]    WARN ocnrf [requesterNfType=PCF]: Allow only VendorSpecific
             attributes — field 'nfSetIdLists' is not a valid 3GPP attribute
[GRAPH]  → rag_lookup  (field typo detected, querying knowledge base)
[RAG]    query="nfSetIdLists nfSetIdList 3GPP NF profile"
[RAG]    top chunk: "Per 3GPP TS 29.510 §6.2.2, the correct field name is
          'nfSetIdList' (no trailing 's'). NRF silently drops unrecognized
          attributes — registration returns 200 but the field is absent."
[GRAPH]  → analyze  (mode=llm)
[LLM]  < 200 OK  duration=3.9s
[LLM]  root_cause → Field 'nfSetIdLists' is a typo of 'nfSetIdList';
                     NRF silently drops it causing incomplete NF profile.
[LLM]  fix_action → fix_field:nfSetIdLists:nfSetIdList  confidence=high

[GRAPH] → decide  route=fix_field  (field typo, confidence=high)
[GRAPH] → fix_field
[FIX]   > PATCH /nrf-client-nfmanagement/nfProfileList
[FIX]   > rename field 'nfSetIdLists' → 'nfSetIdList'
[PCF]   < 200 OK  duration=0.051s

[GRAPH] → verify_fix  attempt 1/3  (sleeping 30s...)
[VERIFY] NRF WARN logs cleared  ✓ Field accepted by NRF

[AGENT] ===== Run Complete =====
[AGENT] Root cause  : PCF field 'nfSetIdLists' silently dropped by NRF.
[AGENT] Fix action  : fix_field:nfSetIdLists:nfSetIdList
[AGENT] Confidence  : high
[AGENT] Fix applied : True
[AGENT] Fix verified: True
[AUDIT] Incident saved → /data/incidents.jsonl
```

---

### Scenario 3 — Unknown Field (notify only → Slack)

Field name is not in the auto-fix whitelist; agent escalates to on-call via Slack.

```text
[LLM]  root_cause → Unknown field 'capacities' dropped by NRF; not in auto-fix scope.
[LLM]  fix_action → notify_only  confidence=high

[GRAPH] → decide  route=notify  (notify_only)
[GRAPH] → notify
[SLACK] POST https://hooks.slack.com/...
[SLACK] > "NfRegistrationFailure in occnp2 — unknown dropped field: 'capacities'.
           Manual intervention required."
[SLACK] < 200 OK
```

---

## Audit Log (`incidents.jsonl`)

Every agent run appends a structured record — full decision trail, queryable, maps to DynamoDB/OCI NoSQL in production.

```jsonl
{"ts":"2026-05-18T03:22:47","alert_name":"NfRegistrationFailure","namespace":"occnp2","root_cause":"PCF plmnList contains invalid PLMN (510/088), not in NRF allowed list.","fix_action":"update_plmn:510:011","confidence":"high","fix_applied":true,"fix_verified":true,"error":null}
{"ts":"2026-05-18T04:09:22","alert_name":"NfRegistrationFailure","namespace":"occnp2","root_cause":"Field 'nfSetIdLists' silently dropped by NRF — typo of 'nfSetIdList'.","fix_action":"fix_field:nfSetIdLists:nfSetIdList","confidence":"high","fix_applied":true,"fix_verified":true,"error":null}
{"ts":"2026-05-18T11:08:06","alert_name":"NfRegistrationFailure","namespace":"occnp2","root_cause":"PCF plmnList contains invalid PLMN (510/089), not accepted by NRF.","fix_action":"update_plmn:510:011","confidence":"high","fix_applied":true,"fix_verified":true,"error":null}
{"ts":"2026-05-18T13:23:13","alert_name":"NfRegistrationFailure","namespace":"occnp2","root_cause":"Field 'nfSetIdLists' incorrectly named, silently dropped by NRF.","fix_action":"fix_field:nfSetIdLists:nfSetIdList","confidence":"high","fix_applied":true,"fix_verified":true,"error":null}
```
