import os
import requests
from dotenv import load_dotenv

load_dotenv()

PCF_CONFIG_URL = os.getenv("PCF_CONFIG_URL", "http://172.16.100.231:8000")
PCF_PROFILE_PATH = os.getenv(
    "PCF_PROFILE_PATH",
    "/PCF/nf-common-component/v1/nrf-client-nfmanagement/nfProfileList",
)
HEADERS = {"Content-Type": "application/json"}


def get_pcf_profile() -> dict:
    """GET current PCF NRF registration profile."""
    url = f"{PCF_CONFIG_URL}{PCF_PROFILE_PATH}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # API returns a list, profile is the first element
    return data[0] if isinstance(data, list) else data


def update_pcf_plmn(mcc: str, mnc: str) -> dict:
    """
    Update PCF plmnList to the given MCC/MNC.
    Reads the full current profile first, patches plmnList, then PUTs back.
    Returns the updated profile on success.
    """
    profile = get_pcf_profile()
    profile["plmnList"] = [{"mcc": mcc, "mnc": mnc}]

    url = f"{PCF_CONFIG_URL}{PCF_PROFILE_PATH}"
    resp = requests.put(url, headers=HEADERS, json=[profile], timeout=10)
    resp.raise_for_status()

    # Verify by reading back
    updated = get_pcf_profile()
    return updated


def fix_profile_field(wrong_name: str, correct_name: str) -> dict:
    """
    Rename a field in the PCF NF profile.
    GET full profile → rename field → PUT back.
    Returns updated profile.
    """
    profile = get_pcf_profile()
    if wrong_name in profile:
        profile[correct_name] = profile.pop(wrong_name)
    else:
        raise ValueError(f"Field '{wrong_name}' not found in current PCF profile. "
                         f"Present keys: {list(profile.keys())}")

    url = f"{PCF_CONFIG_URL}{PCF_PROFILE_PATH}"
    resp = requests.put(url, headers=HEADERS, json=[profile], timeout=10)
    resp.raise_for_status()
    return get_pcf_profile()


def inject_bad_plmn() -> dict:
    """Inject a wrong PLMN to simulate NRF registration failure (for testing)."""
    return update_pcf_plmn(mcc="999", mnc="99")


def restore_correct_plmn() -> dict:
    """Restore the correct PLMN (510/011) that NRF accepts."""
    return update_pcf_plmn(mcc="510", mnc="011")


if __name__ == "__main__":
    print("=== PCF Tool Test ===")
    profile = get_pcf_profile()
    print(f"nfStatus:  {profile.get('nfStatus')}")
    print(f"plmnList:  {profile.get('plmnList')}")
    print(f"nfInstanceId: {profile.get('nfInstanceId')}")
