# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Spike-only embedding adapter: batches texts per InvokeModel call and rides out Bedrock throttling.

The account's Cohere Embed v4 quota is about 20 requests a minute. COA's adapter embeds one text per
call and relies on botocore's 5 attempts, which is not enough with several build workers. This keeps
the same ``BedrockEmbedder`` and adds batching (up to 96 texts) and a long throttle backoff. It is
importable in spawn-started build workers because ``scripts/spike`` is on ``PYTHONPATH``.
"""

from __future__ import annotations

import random
import time

from botocore.exceptions import ClientError
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import PrivateAttr

from coa_common.embeddings import BedrockEmbedder

_THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}


class ThrottledBatchEmbedding(BaseEmbedding):
    embed_model_id: str
    embed_dimensions: int = 1024
    embed_region: str | None = None
    embed_batch_size: int = 96
    _embedder: BedrockEmbedder | None = PrivateAttr(default=None)

    def _e(self) -> BedrockEmbedder:
        if self._embedder is None:
            self._embedder = BedrockEmbedder(
                model_id=self.embed_model_id, dimensions=self.embed_dimensions, region=self.embed_region
            )
        return self._embedder

    @staticmethod
    def _retry(fn):
        deadline = time.time() + 900
        attempt = 0
        while True:
            try:
                return fn()
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in _THROTTLE_CODES or time.time() > deadline:
                    raise
                attempt += 1
                time.sleep(min(60.0, 3.0 * attempt) + random.uniform(0, 3))

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._retry(lambda: self._e().embed_query(query))

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._retry(lambda: self._e().embed_document(text))

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._retry(lambda: self._e().embed_documents(list(texts)))

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)
