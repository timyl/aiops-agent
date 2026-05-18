import os
import requests
from dotenv import load_dotenv

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://172.16.100.91:30504")


def query(promql: str) -> list[dict]:
    """Run an instant PromQL query, return list of {metric, value} dicts."""
    resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": promql},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["result"]


def get_nrf_registration_rate(namespace: str = "occnp2") -> dict:
    """Return NRF registration request increase over last 2 minutes."""
    results = query(
        f'increase(occnp_nrfclient_nw_conn_out_request_total'
        f'{{MessageType="AutonomousNfRegistration",namespace="{namespace}"}}[2m])'
    )
    if not results:
        return {"value": 0, "labels": {}}
    r = results[0]
    return {
        "value": float(r["value"][1]),
        "labels": r["metric"],
    }


def get_nrf_response_errors(namespace: str = "occnp2") -> list[dict]:
    """Return non-2xx NRF responses from PCF in the last 2 minutes."""
    results = query(
        f'increase(occnp_nrfclient_nw_conn_in_response_total'
        f'{{namespace="{namespace}"}}[2m])'
    )
    errors = []
    for r in results:
        labels = r["metric"]
        response_code = labels.get("ResponseCode", labels.get("response_code", ""))
        if response_code and not str(response_code).startswith("2"):
            errors.append({
                "response_code": response_code,
                "message_type": labels.get("MessageType", ""),
                "count": float(r["value"][1]),
            })
    return errors


def get_pcf_local_status(namespace: str = "occnp2") -> str:
    """
    Return PCF NRF-client's LOCAL state machine status.
    WARNING: This is PCF-side only. It does NOT confirm NRF has accepted registration.
    To verify actual registration, check nrf_rate is low AND no 501 errors in logs.
    Oracle encoding: 0 = PCF believes it is REGISTERED, non-zero = other states.
    """
    results = query(
        f'occnp_nrfclient_current_nf_status{{namespace="{namespace}"}}'
    )
    nfmgmt = [r for r in results
               if "nrf-client-nfmanagement" in r["metric"].get("microservice", "")]
    if not nfmgmt:
        return "UNKNOWN"
    val = int(float(nfmgmt[0]["value"][1]))
    return "PCF_LOCAL_REGISTERED" if val == 0 else "PCF_LOCAL_OTHER"


def is_registration_healthy(namespace: str = "occnp2") -> bool:
    """
    True only when NRF registration is actually working:
    - retry rate is low (< 3 in 2min means no failure loop)
    """
    rate = get_nrf_registration_rate(namespace)
    return rate["value"] < 3


if __name__ == "__main__":
    print("=== Prometheus Tool Test ===")
    rate = get_nrf_registration_rate()
    print(f"NRF registration rate (2min): {rate['value']:.1f}")

    errors = get_nrf_response_errors()
    print(f"NRF error responses: {errors}")

    status = get_nf_status()
    print(f"NF status: {status}")
