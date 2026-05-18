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
