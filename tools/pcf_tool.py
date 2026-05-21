import logging
import os

import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception, before_sleep_log

load_dotenv()

log = logging.getLogger(__name__)

PCF_CONFIG_URL = os.getenv("PCF_CONFIG_URL", "http://172.16.100.231:8000")
PCF_PROFILE_PATH = os.getenv(
    "PCF_PROFILE_PATH",
    "/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList",
)
HEADERS = {"Content-Type": "application/json"}
_TIMEOUT = 5


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return False


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


@_retry
def get_pcf_profile() -> dict:
    url = f"{PCF_CONFIG_URL}{PCF_PROFILE_PATH}"
    resp = requests.get(url, headers=HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) else data


@_retry
def update_pcf_plmn(mcc: str, mnc: str) -> dict:
    profile = get_pcf_profile()
    profile["plmnList"] = [{"mcc": mcc, "mnc": mnc}]

    url = f"{PCF_CONFIG_URL}{PCF_PROFILE_PATH}"
    resp = requests.put(url, headers=HEADERS, json=[profile], timeout=_TIMEOUT)
    resp.raise_for_status()
    return get_pcf_profile()


@_retry
def fix_profile_field(wrong_name: str, correct_name: str) -> dict:
    profile = get_pcf_profile()
    if wrong_name not in profile:
        raise ValueError(f"Field '{wrong_name}' not found in PCF profile. "
                         f"Present keys: {list(profile.keys())}")
    profile[correct_name] = profile.pop(wrong_name)

    url = f"{PCF_CONFIG_URL}{PCF_PROFILE_PATH}"
    resp = requests.put(url, headers=HEADERS, json=[profile], timeout=_TIMEOUT)
    resp.raise_for_status()
    return get_pcf_profile()


def inject_bad_plmn() -> dict:
    return update_pcf_plmn(mcc="999", mnc="99")


def restore_correct_plmn() -> dict:
    return update_pcf_plmn(mcc="510", mnc="011")


if __name__ == "__main__":
    print("=== PCF Tool Test ===")
    profile = get_pcf_profile()
    print(f"nfStatus:  {profile.get('nfStatus')}")
    print(f"plmnList:  {profile.get('plmnList')}")
    print(f"nfInstanceId: {profile.get('nfInstanceId')}")
