"""
Kafka consumer worker — reads alerts from aiops-alerts topic,
deduplicates via Redis lock, runs LangGraph agent.
"""
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import redis as _redis_lib
from kafka import KafkaConsumer, TopicPartition, OffsetAndMetadata
from dotenv import load_dotenv

load_dotenv()

from agent.log_fmt import AIOpsFormatter

_handler = logging.StreamHandler()
_handler.setFormatter(AIOpsFormatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)
log = logging.getLogger(__name__)

for _noisy_logger in ("elastic_transport", "elastic_transport._transport",
                       "elastic_transport._node._http_urllib3",
                       "elasticsearch", "urllib3", "urllib3.connectionpool"):
    logging.getLogger(_noisy_logger).setLevel(logging.ERROR)

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "172.16.100.91:30092")
KAFKA_TOPIC     = os.getenv("KAFKA_TOPIC", "aiops-alerts")
KAFKA_GROUP     = os.getenv("KAFKA_GROUP", "aiops-worker")

INCIDENTS_FILE  = Path(os.getenv("INCIDENTS_FILE", "incidents.jsonl"))
LOCK_TTL        = int(os.getenv("REDIS_LOCK_TTL", "300"))

_redis = _redis_lib.Redis(
    host=os.getenv("REDIS_HOST", "172.16.100.91"),
    port=int(os.getenv("REDIS_PORT", "30379")),
    decode_responses=True,
)

# Limit concurrent agent runs to avoid hammering LLM API
_semaphore = threading.Semaphore(int(os.getenv("MAX_CONCURRENT_AGENTS", "3")))


def _save_incident(alert: dict, final_state: dict) -> None:
    record = {
        "ts":           datetime.utcnow().isoformat(),
        "alert_name":   alert.get("labels", {}).get("alertname"),
        "namespace":    alert.get("labels", {}).get("namespace"),
        "root_cause":   final_state.get("root_cause"),
        "fix_action":   final_state.get("fix_action"),
        "confidence":   final_state.get("confidence"),
        "fix_applied":  final_state.get("fix_applied"),
        "fix_verified": final_state.get("fix_verified"),
        "error":        final_state.get("error"),
    }
    with open(INCIDENTS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    log.info(f"[AUDIT] Incident saved → {INCIDENTS_FILE}")


def handle_alert(alert: dict, consumer: KafkaConsumer, partition: int, offset: int) -> None:
    alert_name = alert.get("labels", {}).get("alertname", "unknown")
    namespace  = alert.get("labels", {}).get("namespace", "unknown")
    nf_type    = alert.get("labels", {}).get("NfType", "unknown")
    status     = alert.get("status", "unknown")

    log.info(f"[ALERT] {alert_name} | ns={namespace} | NfType={nf_type} | status={status}")

    if alert_name != "NfRegistrationFailure" or status != "firing":
        return

    lock_key = f"aiops:lock:{alert_name}:{namespace}"
    acquired = _redis.set(lock_key, "1", nx=True, ex=LOCK_TTL)
    if not acquired:
        log.warning(f"[DEDUP] Redis lock exists for {lock_key}, skipping duplicate")
        return

    with _semaphore:
        log.info(f"[AGENT] Starting LangGraph agent for {namespace}...")
        try:
            from agent.graph import graph
            initial_state = {
                "alert_name": alert_name,
                "namespace":  namespace,
                "nf_type":    nf_type,
                "logs":       [],
                "nrf_rate":   0.0,
                "nrf_errors": [],
                "pcf_plmn":   [],
                "pcf_local_status": "UNKNOWN",
                "nrf_logs":          [],
                "field_errors":      [],
                "all_dropped_fields": [],
                "rag_context":  [],
                "root_cause": "",
                "fix_action": "",
                "confidence": "",
                "fix_applied":  False,
                "fix_verified": False,
                "error":        None,
            }
            final_state = graph.invoke(initial_state)

            log.info("[AGENT] ===== Run Complete =====")
            log.info(f"[AGENT] Root cause  : {final_state.get('root_cause')}")
            log.info(f"[AGENT] Fix action  : {final_state.get('fix_action')}")
            log.info(f"[AGENT] Confidence  : {final_state.get('confidence')}")
            log.info(f"[AGENT] Fix applied : {final_state.get('fix_applied')}")
            log.info(f"[AGENT] Fix verified: {final_state.get('fix_verified')}")
            if final_state.get("error"):
                log.error(f"[AGENT] Error       : {final_state.get('error')}")
            log.info("[AGENT] ==========================")
            _save_incident(alert, final_state)
            tp = TopicPartition(KAFKA_TOPIC, partition)
            consumer.commit({tp: OffsetAndMetadata(offset + 1, None)})
            log.info(f"[KAFKA] Committed offset={offset + 1} partition={partition}")
        except Exception as e:
            log.error(f"[AGENT] Unhandled exception: {e}", exc_info=True)
        finally:
            _redis.delete(lock_key)


def run_worker() -> None:
    from prometheus_client import start_http_server
    start_http_server(9100)
    log.info("[WORKER] Prometheus metrics server started on port 9100")
    log.info(f"[WORKER] Connecting to Kafka {KAFKA_BOOTSTRAP}, topic={KAFKA_TOPIC}, group={KAFKA_GROUP}")
    def _deserialize(b: bytes):
        try:
            return json.loads(b.decode("utf-8"))
        except Exception:
            return None

    consumer = KafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=KAFKA_GROUP,
        value_deserializer=_deserialize,
        auto_offset_reset="latest",
        enable_auto_commit=False,
    )
    log.info("[WORKER] Consumer ready, waiting for alerts...")

    for msg in consumer:
        if not isinstance(msg.value, dict):
            log.warning(f"[WORKER] Skipping non-dict message at offset={msg.offset}")
            continue
        alert = msg.value
        log.info(f"[WORKER] Consumed offset={msg.offset} partition={msg.partition}")
        t = threading.Thread(target=handle_alert, args=(alert, consumer, msg.partition, msg.offset), daemon=True)
        t.start()


if __name__ == "__main__":
    run_worker()
