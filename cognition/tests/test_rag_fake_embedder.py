"""FakeEmbedder / FakeSparseEmbedder / FakeReranker 确定性（纯逻辑）。"""

from __future__ import annotations

import math

from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.reranker import FakeReranker
from cognition.rag.sparse import FakeSparseEmbedder


def test_dense_deterministic_and_normalized():
    emb = FakeEmbedder(dimension=32)
    a = emb.embed(["人工智能与机器学习"])[0]
    b = emb.embed(["人工智能与机器学习"])[0]
    assert a == b                              # 同输入同输出
    assert len(a) == 32
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-6)  # L2 归一


def test_dense_different_texts_differ():
    emb = FakeEmbedder(dimension=64)
    assert emb.embed(["猫"])[0] != emb.embed(["狗"])[0]


def test_sparse_deterministic_sorted_indices():
    sp = FakeSparseEmbedder()
    (idx1, val1) = sp.embed(["机器 学习 机器"])[0]
    (idx2, val2) = sp.embed(["机器 学习 机器"])[0]
    assert idx1 == idx2 and val1 == val2
    assert idx1 == sorted(idx1)                # 索引升序
    assert sum(val1) == 6.0                    # 字级 token 机×2 器×2 学 习 = 6


def test_reranker_overlap_scoring():
    rr = FakeReranker()
    scores = rr.score("机器学习", ["机器学习很有用", "今天天气不错"])
    assert scores[0] > scores[1]               # 词重叠高者分高
    assert all(0.0 <= s <= 1.0 for s in scores)
