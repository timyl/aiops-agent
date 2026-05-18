"""
RAG knowledge base for 3GPP NF Profile field validation.

Uses FAISS in-memory vector store with DashScope text-embedding-v2.
Knowledge is loaded at module import time; vectorstore built on first query.
"""

import os
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()

_KNOWLEDGE = [
    # ── Correct field names (3GPP TS 29.510) ────────────────────────────────
    "nfSetIdList: 3GPP TS 29.510 section 6.1.6.2.2 — correct field name. "
    "List of NF Set identifiers the NF belongs to. Type: array of strings. "
    "Example: ['pcfSet-A', 'pcfSet-B']. NRF validates and stores this field.",

    "nfServiceList: 3GPP TS 29.510 correct field name. "
    "Map of NF service instances exposed by this NF. Key: service name. "
    "NRF indexes this for service discovery.",

    "plmnList: 3GPP TS 29.510 correct field name. "
    "List of PLMN IDs the NF serves. Each entry: {mcc, mnc}. "
    "NRF validates mcc/mnc against its allowed list.",

    "fqdn: 3GPP TS 29.510 correct field name. "
    "Fully Qualified Domain Name of the NF instance.",

    "ipv4Addresses: 3GPP TS 29.510 correct field name. "
    "List of IPv4 addresses of the NF instance.",

    "nfStatus: 3GPP TS 29.510 correct field name. "
    "Registration status: REGISTERED | DEREGISTERED | UNDISCOVERABLE.",

    "nfType: 3GPP TS 29.510 correct field name. "
    "Type of NF: PCF | AMF | SMF | UDM | AUSF | NRF | UPF etc.",

    "nfInstanceId: 3GPP TS 29.510 correct field name. "
    "UUID identifying this NF instance. Globally unique.",

    "capacity: 3GPP TS 29.510 correct field name. "
    "Integer 0-65535. Static capacity of the NF. Used for load balancing.",

    "priority: 3GPP TS 29.510 correct field name. "
    "Integer 0-65535. Lower value = higher priority for NF selection.",

    # ── Common typos / invalid fields (Oracle NRF silently drops these) ──────
    "nfSetIdLists: INVALID — not a 3GPP TS 29.510 field. "
    "Common typo: extra 's' appended to nfSetIdList. "
    "Oracle NRF behavior: returns HTTP 200 but silently drops this field. "
    "NRF WARN log: 'The following attributes have been dropped/ignored [nfSetIdLists]'. "
    "Business impact: downstream NFs cannot discover this PCF by NF Set membership. "
    "Fix: rename field to nfSetIdList (remove trailing 's').",

    "nfServiceLists: INVALID — not a 3GPP TS 29.510 field. "
    "Common typo: extra 's' on nfServiceList. "
    "Oracle NRF silently drops with WARN log. Fix: use nfServiceList.",

    "plmnLists: INVALID — not a 3GPP TS 29.510 field. "
    "Common typo: extra 's' on plmnList. "
    "Oracle NRF silently drops with WARN log. Fix: use plmnList.",

    "ipv4Address: INVALID — not a 3GPP TS 29.510 field. "
    "Singular form; correct field is ipv4Addresses (plural). "
    "NRF may silently drop this. Fix: use ipv4Addresses.",

    "scpInfo: vendor-specific field, not in 3GPP TS 29.510 core NF profile schema. "
    "Some vendor implementations include this; Oracle NRF may drop it with WARN log. "
    "Not safe to auto-fix — could be intentional vendor extension.",

    "servingScope: vendor-specific or operator-defined field. "
    "Not in 3GPP TS 29.510 standard NF profile. Oracle NRF may silently drop. "
    "Requires operator review before removal.",

    "lcHSupportInd: vendor-specific indicator field. "
    "Not a standard 3GPP TS 29.510 NF Profile field. Oracle NRF silently drops. "
    "Likely safe to remove if not used by any consumer NF.",

    "olcHSupportInd: vendor-specific indicator field. "
    "Not a standard 3GPP TS 29.510 NF Profile field. Oracle NRF silently drops. "
    "Likely safe to remove if not used by any consumer NF.",

    # ── Oracle NRF silent drop behavior ─────────────────────────────────────
    "Oracle NRF silent drop: when NRF receives an NF Profile PUT/PATCH request "
    "with unknown or non-standard fields, it returns HTTP 200 OK but omits those "
    "fields from storage. A WARN log is emitted: "
    "'The following attributes have been dropped/ignored [<fieldName>]'. "
    "The registration appears successful from the PCF perspective (nrf_rate stays normal), "
    "but the profile stored in NRF is incomplete. "
    "Detection: query NRF WARN logs, not Prometheus metrics.",

    "NRF registration failure modes: "
    "1. Hard rejection (400/403): invalid PLMN, schema error — PCF retries → nrf_rate spikes. "
    "2. Silent drop (200 + WARN log): unknown field names — nrf_rate stays normal, "
    "   profile stored without the dropped field — only detectable via NRF WARN logs. "
    "3. Network/TLS error: connection refused — PCF retries → nrf_rate spikes.",
]


@lru_cache(maxsize=1)
def _build_vectorstore():
    from langchain_community.vectorstores import FAISS
    from langchain_openai import OpenAIEmbeddings
    from langchain_core.documents import Document

    docs = [Document(page_content=t) for t in _KNOWLEDGE]

    embeddings = OpenAIEmbeddings(
        model="text-embedding-v2",       # DashScope supported model
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url=os.getenv("DASHSCOPE_API_HOST"),
        tiktoken_enabled=False,          # send raw strings, not token IDs
        check_embedding_ctx_length=False,
    )
    return FAISS.from_documents(docs, embeddings)


def query_knowledge(question: str, k: int = 3) -> list[str]:
    """Retrieve top-k relevant knowledge chunks via vector similarity search."""
    vs = _build_vectorstore()
    docs = vs.similarity_search(question, k=k)
    return [d.page_content for d in docs]


def get_field_info(field_name: str) -> list[str]:
    """Look up knowledge about a specific NF Profile field name."""
    return query_knowledge(f"field name {field_name} 3GPP NRF profile dropped ignored", k=3)
