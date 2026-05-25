#!/usr/bin/env python3
"""
AIOps Agent — Concurrent Load Test
Injects fault states into PCF, fires alerts via webhook, monitors via Redis locks.

Usage:
    python scripts/load_test.py              # run all scenarios
    python scripts/load_test.py --scenario 1 # run specific scenario (1-5)
    python scripts/load_test.py --dry-run    # print plan without executing
"""
import argparse
import json
import sys
import threading
import time
from datetime import datetime

import redis
import requests

# ── Config ────────────────────────────────────────────────────────────────────
WEBHOOK_URL = "http://172.16.100.234:8000/alarm"
PCF_URL     = "http://172.16.100.231:8000"
PCF_PATH    = "/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList"
REDIS_HOST  = "172.16.100.91"
REDIS_PORT  = 30379

# The real PCF namespace — alerts for this ns will hit the real PCF API
PCF_NS = "occnp2"

# ── Redis client ──────────────────────────────────────────────────────────────
_redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)

def _lock_key(namespace: str, alertname: str = "NfRegistrationFailure") -> str:
    return f"aiops:lock:{alertname}:{namespace}"

def clear_lock(namespace: str) -> None:
    key = _lock_key(namespace)
    if _redis.delete(key):
        log(f"  🔓 Cleared Redis lock: {key}")
    else:
        log(f"  ✓  No lock to clear for {namespace}")

def get_pcf_profile() -> dict:
    resp = requests.get(f"{PCF_URL}{PCF_PATH}", timeout=5)
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) else data

def put_pcf_profile(profile: dict) -> None:
    resp = requests.put(
        f"{PCF_URL}{PCF_PATH}",
        json=[profile],
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    resp.raise_for_status()

def inject_plmn(mcc: str, mnc: str) -> None:
    profile = get_pcf_profile()
    profile["plmnList"] = [{"mcc": mcc, "mnc": mnc}]
    put_pcf_profile(profile)
    log(f"  💉 PCF plmnList → [{mcc}/{mnc}]")

def inject_field_typo(wrong: str, correct: str) -> bool:
    """Rename correct field to wrong name to simulate PCF profile typo."""
    profile = get_pcf_profile()
    if correct not in profile:
        log(f"  ⚠  Field '{correct}' not found in profile (keys: {list(profile.keys())})")
        return False
    if wrong in profile:
        log(f"  ⚠  Field '{wrong}' already present — skipping injection")
        return False
    profile[wrong] = profile.pop(correct)
    put_pcf_profile(profile)
    log(f"  💉 PCF field: '{correct}' → '{wrong}' (typo injected)")
    return True

def restore_field(wrong: str, correct: str) -> None:
    profile = get_pcf_profile()
    if wrong in profile:
        profile[correct] = profile.pop(wrong)
        put_pcf_profile(profile)
        log(f"  🔧 PCF field restored: '{wrong}' → '{correct}'")

def send_alert(namespace: str, note: str = "") -> int:
    payload = {
        "groupKey": f"loadtest-{namespace}-{int(time.time()*1000)}",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "NfRegistrationFailure",
                "namespace": namespace,
                "NfType": "PCF",
            },
            "annotations": {"summary": note or f"load test alert ns={namespace}"},
        }],
    }
    t0 = time.time()
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=5)
    dur = time.time() - t0
    log(f"  📤 Alert → ns={namespace:30s}  HTTP={resp.status_code}  ({dur*1000:.0f}ms)  [{note}]")
    return resp.status_code

def wait_lock_released(namespace: str, timeout: int = 360) -> bool:
    key = _lock_key(namespace)
    # Phase 1: wait for the agent to acquire the lock (Kafka lag can be 1-5s)
    log(f"  ⏳ Waiting for agent to acquire lock ({key})...")
    acquire_deadline = time.time() + 20
    while time.time() < acquire_deadline:
        if _redis.exists(key):
            log(f"  🔒 Lock acquired — agent is processing...")
            break
        time.sleep(0.5)
    else:
        log(f"  ⚠  Lock never acquired within 20s — agent may not have started")
        return False
    # Phase 2: wait for the agent to release the lock
    log(f"  ⏳ Waiting for agent to finish (timeout={timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _redis.exists(key):
            log(f"  ✅ Lock released — agent done")
            return True
        time.sleep(3)
    log(f"  ⚠  Timed out waiting for lock release")
    return False

def separator(title: str) -> None:
    log("═" * 64)
    log(f"  {title}")
    log("═" * 64)

# ── Scenarios ─────────────────────────────────────────────────────────────────

def s1_plmn_mismatch() -> None:
    """
    Inject bad PLMN (999/99, not in whitelist) → fire alert → agent detects
    PLMN mismatch, calls update_pcf_plmn(510,011) via LLM tool call →
    verify_fix polls Prometheus until rate drops → ALERTS_PROCESSED(auto_fixed)++
    Expected metrics: aiops_alerts_processed_total{outcome="auto_fixed"} +1
                      aiops_fix_verified_total{result="success"} +1
    """
    separator("S1: PLMN Mismatch → Auto-Fix")
    clear_lock(PCF_NS)
    inject_plmn("999", "99")
    time.sleep(1)
    send_alert(PCF_NS, "S1 plmn-mismatch")
    wait_lock_released(PCF_NS, timeout=360)
    profile = get_pcf_profile()
    plmn = profile.get("plmnList", [])
    expected = [{"mcc": "510", "mnc": "011"}]
    log(f"  {'✅ PASS' if plmn == expected else '❌ FAIL'} — plmnList={plmn}")


def s2_no_action() -> None:
    """
    PCF is healthy (correct PLMN, no dropped fields, Prometheus rate low) →
    LLM calls no_action → agent routes to END without executing any fix.
    Expected metrics: aiops_alerts_processed_total{outcome="no_action"} +1
    """
    separator("S2: Healthy PCF → No Action")
    inject_plmn("510", "011")
    time.sleep(1)
    clear_lock(PCF_NS)
    send_alert(PCF_NS, "S2 no-action healthy")
    wait_lock_released(PCF_NS, timeout=120)
    log("  Check worker logs for: [GRAPH] → decide  route=end  (no_action)")


def s3_dedup() -> None:
    """
    Send 3 identical alerts within 600ms. Redis lock (TTL=300s) means only
    the first acquired lock → first alert processed; the other 2 hit the
    'lock exists → skip' path in handle_alert.
    Expected metrics: aiops_alerts_processed_total +1 (not +3)
                      Worker logs: 2× [DEDUP] Redis lock exists ... skipping
    """
    separator("S3: Dedup — 3 Rapid Identical Alerts (only 1 should process)")
    clear_lock(PCF_NS)
    inject_plmn("999", "99")
    time.sleep(1)

    results: list[int] = []
    lock = threading.Lock()

    def _send(i: int) -> None:
        time.sleep(i * 0.2)   # stagger: 0ms, 200ms, 400ms
        code = send_alert(PCF_NS, f"S3 dedup #{i+1}")
        with lock:
            results.append(code)

    threads = [threading.Thread(target=_send, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wait_lock_released(PCF_NS, timeout=360)
    log("  Check worker logs: expect 1× [AGENT] Starting, 2× [DEDUP] skipping")
    inject_plmn("510", "011")   # restore


def s4_field_typo() -> None:
    """
    Rename nfSetIdList → nfSetIdLists (known fixable typo) in PCF profile.
    NRF will emit WARN 'dropped/ignored [nfSetIdLists]' on next heartbeat.
    Agent: fetch_nrf_logs detects the typo → rag_lookup → LLM calls
    fix_profile_field(wrong='nfSetIdLists', correct='nfSetIdList') →
    execute_tool renames the field back.
    NOTE: NRF heartbeat interval ~60s — agent may need ES log to already exist.
          Run this scenario last or after a 60s wait post-injection.
    Expected metrics: aiops_alerts_processed_total{outcome="auto_fixed"} +1
                      aiops_rag_duration_seconds and aiops_rag_chunks_returned populated
    """
    separator("S4: Field Typo (nfSetIdLists) → Auto-Fix")
    clear_lock(PCF_NS)
    ok = inject_field_typo(wrong="nfSetIdLists", correct="nfSetIdList")
    if not ok:
        log("  ⚠  Skipping S4 — typo injection failed (field may not be at top level)")
        return
    log("  Waiting 65s for NRF heartbeat to generate WARN log in ES...")
    time.sleep(65)
    send_alert(PCF_NS, "S4 field-typo nfSetIdLists")
    wait_lock_released(PCF_NS, timeout=180)
    profile = get_pcf_profile()
    has_correct = "nfSetIdList" in profile
    has_wrong   = "nfSetIdLists" in profile
    log(f"  {'✅ PASS' if has_correct and not has_wrong else '❌ FAIL'} — "
        f"nfSetIdList={has_correct}  nfSetIdLists={has_wrong}")
    if has_wrong:
        restore_field("nfSetIdLists", "nfSetIdList")


def s5_concurrent_semaphore() -> None:
    """
    Fire 4 alerts for 4 different namespaces simultaneously.
    Worker semaphore MAX_CONCURRENT_AGENTS=3 → first 3 start immediately,
    4th blocks until one slot frees. Total agent throughput test.
    NS oai-cn5g-pcf: has real PCF data — will produce a real result.
    NS test-ns-{1,2,3}: ES will return 0 logs, PCF returns same profile →
    LLM will likely route to no_action (low rate, correct PLMN).
    Expected metrics: aiops_alerts_processed_total += 4 (mixed outcomes)
    """
    separator("S5: Concurrent — 4 Alerts, Semaphore Limit = 3")
    namespaces = [PCF_NS, "test-ns-alpha", "test-ns-beta", "test-ns-gamma"]
    for ns in namespaces:
        clear_lock(ns)

    inject_plmn("999", "99")
    time.sleep(1)

    def _fire(ns: str, i: int) -> None:
        time.sleep(i * 0.05)   # fire within 150ms window
        send_alert(ns, f"S5 concurrent #{i+1} ns={ns}")

    threads = [threading.Thread(target=_fire, args=(ns, i))
               for i, ns in enumerate(namespaces)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log(f"  All 4 alerts dispatched in {(time.time()-t0)*1000:.0f}ms")
    log("  Watching for PCF namespace to complete (others may finish first)...")
    wait_lock_released(PCF_NS, timeout=420)
    inject_plmn("510", "011")
    log("  Check worker logs: 4th alert should show delayed [AGENT] Starting")


# ── Entry point ───────────────────────────────────────────────────────────────
SCENARIOS = {
    1: ("PLMN Mismatch → Auto-Fix",          s1_plmn_mismatch),
    2: ("Healthy PCF → No Action",            s2_no_action),
    3: ("Dedup (3 rapid identical alerts)",   s3_dedup),
    4: ("Field Typo nfSetIdLists → Auto-Fix", s4_field_typo),
    5: ("Concurrent 4 alerts (semaphore=3)",  s5_concurrent_semaphore),
}

PLAN = """
╔══════════════════════════════════════════════════════════════╗
║          AIOps Agent — Concurrent Test Plan                  ║
╠══════════════════════════════════════════════════════════════╣
║ S1  PLMN Mismatch      inject 999/99 → expect auto-fix       ║
║ S2  No Action          healthy PCF → expect no_action        ║
║ S3  Dedup              3 rapid alerts → only 1 processed     ║
║ S4  Field Typo         nfSetIdLists → expect auto-fix        ║
║     (requires 65s NRF heartbeat wait)                        ║
║ S5  Concurrent         4 alerts simultaneous, semaphore=3    ║
╠══════════════════════════════════════════════════════════════╣
║ Grafana: {__name__=~"aiops_.*"}                              ║
║ Worker logs: kubectl logs -n aiops -l app=aiops-worker -f    ║
╚══════════════════════════════════════════════════════════════╝
"""

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIOps concurrent load test")
    parser.add_argument("--scenario", type=int, choices=SCENARIOS.keys(),
                        help="Run a single scenario (1-5); default: all")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without executing")
    args = parser.parse_args()

    print(PLAN)

    if args.dry_run:
        for n, (title, _) in SCENARIOS.items():
            print(f"  S{n}: {title}")
        sys.exit(0)

    if args.scenario:
        title, fn = SCENARIOS[args.scenario]
        log(f"Running single scenario: S{args.scenario} — {title}")
        fn()
    else:
        log("Running all scenarios sequentially...")
        for n, (title, fn) in SCENARIOS.items():
            fn()
            if n < len(SCENARIOS):
                log(f"  Cooling down 10s before next scenario...")
                time.sleep(10)

    log("")
    separator("Test suite complete — open Grafana to see metrics!")
