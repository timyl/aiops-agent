import json
import logging
import os
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import Response
from kafka import KafkaProducer
from dotenv import load_dotenv
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

load_dotenv()

from agent.log_fmt import AIOpsFormatter

_handler = logging.StreamHandler()
_handler.setFormatter(AIOpsFormatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)
logging.getLogger("uvicorn.access").propagate = False
log = logging.getLogger(__name__)

app = FastAPI(title="AIOps Webhook Receiver")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "172.16.100.91:30092")
KAFKA_TOPIC     = os.getenv("KAFKA_TOPIC", "aiops-alerts")

_producer = KafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
)


@app.post("/alarm")
async def receive_alarm(request: Request):
    """Receive AlertManager webhook POST, forward each alert to Kafka."""
    payload = await request.json()
    alerts  = payload.get("alerts", [])
    log.info(f"[WEBHOOK] Received {len(alerts)} alert(s), groupKey={payload.get('groupKey','?')}")

    for alert in alerts:
        _producer.send(KAFKA_TOPIC, alert)
        alert_name = alert.get("labels", {}).get("alertname", "?")
        namespace  = alert.get("labels", {}).get("namespace", "?")
        log.info(f"[KAFKA] Produced → topic={KAFKA_TOPIC} alert={alert_name} ns={namespace}")

    _producer.flush()
    return {"status": "queued", "count": len(alerts)}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint — scraped by Prometheus every 15s."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
