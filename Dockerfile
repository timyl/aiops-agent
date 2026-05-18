FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/   ./agent/
COPY tools/   ./tools/
COPY webhook/ ./webhook/

# Incident audit log directory (mount emptyDir or PVC here in K8s)
RUN mkdir -p /data

# Default: webhook receiver (override in worker Deployment spec)
CMD ["uvicorn", "webhook.server:app", "--host", "0.0.0.0", "--port", "8000"]
