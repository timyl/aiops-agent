import logging
import os
from datetime import datetime, timezone, timedelta
from elasticsearch import Elasticsearch
from dotenv import load_dotenv

load_dotenv()

logging.getLogger("elastic_transport").setLevel(logging.ERROR)
logging.getLogger("elasticsearch").setLevel(logging.ERROR)

_client = None

def _get_client() -> Elasticsearch:
    global _client
    if _client is None:
        _client = Elasticsearch(
            os.getenv("ES_URL"),
            basic_auth=(os.getenv("ES_USER"), os.getenv("ES_PASSWORD")),
            verify_certs=False,
        )
    return _client


def fetch_logs(namespace: str, minutes: int = 5, max_lines: int = 60) -> list[str]:
    """Query recent ERROR/WARN logs from a namespace in EFK."""
    es = _get_client()

    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    index = f"k8s-{now.strftime('%Y.%m.%d')}"

    body = {
        "size": max_lines,
        "sort": [{"@timestamp": {"order": "asc"}}],
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kubernetes.namespace_name": namespace}},
                    {"terms": {"level.keyword": ["ERROR", "WARN"]}},
                    {"range": {"@timestamp": {"gte": since.isoformat()}}},
                ]
            }
        },
        "_source": ["@timestamp", "level", "kubernetes.pod_name",
                    "kubernetes.container_name", "message"],
    }

    resp = es.search(index=index, body=body)
    hits = resp["hits"]["hits"]

    lines = []
    for h in hits:
        src = h["_source"]
        ts = src.get("@timestamp", "")[:19]
        level = src.get("level", "")
        pod = src.get("kubernetes", {}).get("pod_name", "")
        msg = src.get("message", "").strip()[:300]
        lines.append(f"[{ts}] {level} {pod}: {msg}")

    return lines


def fetch_logs_for_pod(pod_name: str, namespace: str, minutes: int = 5) -> list[str]:
    """Query logs for a specific pod."""
    es = _get_client()

    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    index = f"k8s-{now.strftime('%Y.%m.%d')}"

    body = {
        "size": 100,
        "sort": [{"@timestamp": {"order": "asc"}}],
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kubernetes.pod_name": pod_name}},
                    {"term": {"kubernetes.namespace_name": namespace}},
                    {"range": {"@timestamp": {"gte": since.isoformat()}}},
                ]
            }
        },
        "_source": ["@timestamp", "level", "message"],
    }

    resp = es.search(index=index, body=body)
    hits = resp["hits"]["hits"]

    lines = []
    for h in hits:
        src = h["_source"]
        ts = src.get("@timestamp", "")[:19]
        level = src.get("level", "?")
        msg = src.get("message", "").strip()[:300]
        lines.append(f"[{ts}] {level}: {msg}")

    return lines


def fetch_nrf_logs(minutes: int = 5, max_lines: int = 60) -> list[str]:
    """Query recent WARN logs from NRF (ocnrf1) — detects silent drop faults.

    Each returned line includes the requesterNfType so callers can filter
    PCF drops from SCP/AMF drops.
    Format: [timestamp] LEVEL pod [requesterNfType=X]: message
    """
    es = _get_client()

    now   = datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    index = f"k8s-{now.strftime('%Y.%m.%d')}"

    body = {
        "size": max_lines,
        "sort": [{"@timestamp": {"order": "desc"}}],   # newest first — avoid old errors crowding out recent WARNs
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kubernetes.namespace_name": "ocnrf1"}},
                    {"terms": {"level.keyword": ["WARN"]}},   # WARN only — hard-rejection ERRORs are already captured via Prometheus
                    {"range": {"@timestamp": {"gte": since.isoformat()}}},
                ]
            }
        },
        "_source": ["@timestamp", "level", "kubernetes.pod_name",
                    "message", "requesterNfType"],
    }

    resp = es.search(index=index, body=body)
    hits = resp["hits"]["hits"]

    lines = []
    for h in hits:
        src      = h["_source"]
        ts       = src.get("@timestamp", "")[:19]
        lvl      = src.get("level", "")
        pod      = src.get("kubernetes", {}).get("pod_name", "")
        msg      = src.get("message", "").strip()[:500]
        nf_type  = src.get("requesterNfType", "")
        tag      = f" [requesterNfType={nf_type}]" if nf_type else ""
        lines.append(f"[{ts}] {lvl} {pod}{tag}: {msg}")

    return lines


if __name__ == "__main__":
    print("=== ES Tool Test ===")
    logs = fetch_logs("occnp2", minutes=30)
    print(f"Fetched {len(logs)} log lines from occnp2 (last 30min):")
    for line in logs[:5]:
        print(line)
