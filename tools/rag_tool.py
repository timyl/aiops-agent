"""
RAG knowledge base for 3GPP NF Profile field validation.

Calls Alibaba Cloud Bailian knowledge base via the official Bailian SDK.
Required .env vars:
  ALIBABA_CLOUD_ACCESS_KEY_ID      - AK from Alibaba Cloud RAM console
  ALIBABA_CLOUD_ACCESS_KEY_SECRET  - SK from Alibaba Cloud RAM console
  BAILIAN_WORKSPACE_ID             - Business space ID from Bailian console (右上角 业务空间)
  BAILIAN_INDEX_ID                 - Knowledge base ID (知识库卡片上的 ID, e.g. jar5imuame)
"""

import os
from alibabacloud_bailian20231229 import models as bailian_models
from alibabacloud_bailian20231229.client import Client as BailianClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from dotenv import load_dotenv

load_dotenv()

_ACCESS_KEY_ID     = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
_ACCESS_KEY_SECRET = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
_WORKSPACE_ID      = os.getenv("BAILIAN_WORKSPACE_ID", "")
_INDEX_ID          = os.getenv("BAILIAN_INDEX_ID", "")

_client = None


def _get_client() -> BailianClient:
    global _client
    if _client is None:
        config = open_api_models.Config(
            access_key_id=_ACCESS_KEY_ID,
            access_key_secret=_ACCESS_KEY_SECRET,
        )
        config.endpoint = "bailian.cn-beijing.aliyuncs.com"
        _client = BailianClient(config)
    return _client


def query_knowledge(question: str, k: int = 3) -> list[str]:
    """Retrieve top-k relevant knowledge chunks from Bailian knowledge base."""
    if not _INDEX_ID:
        raise RuntimeError("BAILIAN_INDEX_ID not set in .env")
    if not _WORKSPACE_ID:
        raise RuntimeError("BAILIAN_WORKSPACE_ID not set in .env")

    client = _get_client()
    req = bailian_models.RetrieveRequest(
        index_id=_INDEX_ID,
        query=question,
        dense_similarity_top_k=k,
    )
    runtime = util_models.RuntimeOptions()
    resp = client.retrieve_with_options(_WORKSPACE_ID, req, {}, runtime)

    chunks = []
    if resp.body and resp.body.data and resp.body.data.nodes:
        for node in resp.body.data.nodes:
            text = getattr(node, "text", "") or getattr(node, "content", "")
            if text:
                chunks.append(text)
    return chunks


def get_field_info(field_name: str) -> list[str]:
    """Look up knowledge about a specific NF Profile field name."""
    return query_knowledge(
        f"field name {field_name} 3GPP NRF profile dropped ignored", k=3
    )


if __name__ == "__main__":
    print("=== Bailian RAG Tool Test ===")
    results = query_knowledge("nfSetIdLists dropped ignored NRF", k=3)
    print(f"Retrieved {len(results)} chunks:")
    for i, chunk in enumerate(results, 1):
        print(f"\n[{i}] {chunk[:200]}")