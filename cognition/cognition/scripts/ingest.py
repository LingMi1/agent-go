"""灌库脚本：读语料 → chunk → dense+sparse embed → upsert 到 Qdrant。

用法：
  # 离线自测（默认 Fake + :memory:，跑完做一次示例检索打印结果）
  uv run python -m cognition.scripts.ingest --file cognition/examples/sample_corpus.txt --kb-id demo
  # 真实（按 deploy/.env 的 provider 与 QDRANT_URL；需先起 Qdrant / 填 key）
  uv run python -m cognition.scripts.ingest --real --file <语料.txt> --kb-id <kb>

语料按空行分段，每段作为一篇文档。
"""

from __future__ import annotations

import argparse

from qdrant_client import QdrantClient

from cognition.config import get_settings
from cognition.rag.embeddings import FakeEmbedder
from cognition.rag.factory import build_embedder, build_sparse
from cognition.rag.ingest import ingest
from cognition.rag.retriever import Retriever
from cognition.rag.sparse import FakeSparseEmbedder
from cognition.rag.store import QdrantStore


def _load_docs(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    paras = [p.strip() for p in raw.split("\n\n") if p.strip()]
    return [{"text": p, "file_name": f"{path.rsplit('/', 1)[-1]}#{i}"} for i, p in enumerate(paras)]


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG 灌库脚本")
    ap.add_argument("--file", required=True)
    ap.add_argument("--kb-id", default="demo")
    ap.add_argument("--real", action="store_true", help="用 settings 的真实 provider + Qdrant")
    ap.add_argument("--query", default="什么是混合检索？", help="灌完做一次示例检索")
    args = ap.parse_args()

    settings = get_settings()
    docs = _load_docs(args.file)

    if args.real:
        embedder, sparse = build_embedder(settings), build_sparse(settings)
        store = QdrantStore.from_settings(settings)
    else:
        dim = 64
        embedder, sparse = FakeEmbedder(dim), FakeSparseEmbedder()
        store = QdrantStore(QdrantClient(location=":memory:"), settings.qdrant_collection, dim)

    n = ingest(docs, args.kb_id, store=store, embedder=embedder, sparse=sparse)
    print(f"[ingest] kb={args.kb_id} docs={len(docs)} chunks_upserted={n}")

    retriever = Retriever(store, embedder, sparse, top_k=3)
    hits = retriever.retrieve([args.query], kb_id=args.kb_id)
    print(f"[query] {args.query!r} -> {len(hits)} hits")
    for i, d in enumerate(hits, 1):
        print(f"  〔{i}〕({d['file_name']}, score={d['score']:.4f}) {d['text'][:60]}")


if __name__ == "__main__":
    main()
