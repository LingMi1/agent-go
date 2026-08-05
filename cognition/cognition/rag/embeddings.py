"""Dense embedding provider 抽象（I/O）+ 确定性 FakeEmbedder + 惰性真实实现。

- FakeEmbedder：由文本 token 做特征哈希 → 定长 L2 归一化向量。确定性、无依赖、不触网，
  供纯逻辑测试、`:memory:` 契约测试与 fake 端到端使用。
- FastembedEmbedder / OpenAICompatEmbedder：惰性 import（fastembed / httpx），生产按需切；
  不测试（人工验收）。fastembed 非硬依赖，用时 `uv add fastembed`。
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[一-鿿]")


def tokenize(text: str) -> list[str]:
    """拉丁词整体 + 中文按字切；小写。dense/sparse 共用，保证一致性。"""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _token_hash(token: str) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)


class EmbeddingProvider(ABC):
    """dense 向量 provider。"""

    dimension: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class FakeEmbedder(EmbeddingProvider):
    """确定性特征哈希 embedder（测试用）。同输入永远同输出、L2 归一。"""

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dimension
        for tok in tokenize(text):
            h = _token_hash(tok)
            idx = h % self.dimension
            sign = 1.0 if ((h >> 8) & 1) else -1.0
            v[idx] += sign
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class FastembedEmbedder(EmbeddingProvider):
    """本地 ONNX embedding（惰性 fastembed；首用会下模型）。"""

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5", dimension: int = 512) -> None:
        self.model = model
        self.dimension = dimension
        self._impl = None

    def _ensure(self):
        if self._impl is None:
            from fastembed import TextEmbedding  # 惰性：非硬依赖

            self._impl = TextEmbedding(model_name=self.model)
        return self._impl

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._ensure().embed(texts)]


class OpenAICompatEmbedder(EmbeddingProvider):
    """OpenAI 兼容 embedding API（如 SiliconFlow bge-m3）。惰性 httpx。"""

    def __init__(self, *, base_url: str, api_key: str, model: str, dimension: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [item["embedding"] for item in data]
