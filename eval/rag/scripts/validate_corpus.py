#!/usr/bin/env python3
"""Validate the Pro-Agent offline RAG corpus without third-party packages."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
RAG_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = SCRIPT_PATH.parents[3]
CORPUS_ROOT = RAG_ROOT / "corpus"
MANIFEST_PATH = RAG_ROOT / "corpus_manifest.json"
SOURCE_MAP_PATH = RAG_ROOT / "source_map.json"

ALLOWED_STATUSES = {"implemented", "partial", "planned"}
REQUIRED_FRONT_MATTER = {
    "document_id",
    "title",
    "module",
    "version",
    "source_revision",
    "status",
    "source_files",
}
REQUIRED_SECTIONS = (
    "## 业务目标",
    "## 执行流程",
    "## 关键数据结构",
    "## 失败场景",
    "## 限制与消歧",
)


class ValidationError(ValueError):
    """Raised when an individual corpus artifact is malformed."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"缺少文件: {path.relative_to(REPO_ROOT)}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"JSON 非法: {path.relative_to(REPO_ROOT)}: {exc}") from exc


def parse_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"front matter 字符串非法: {raw}") from exc
    return value


def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValidationError(f"缺少 front matter: {path.relative_to(REPO_ROOT)}")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValidationError(f"front matter 未闭合: {path.relative_to(REPO_ROOT)}")

    header = text[4:marker]
    body = text[marker + 5 :]
    metadata: dict[str, Any] = {}
    active_list: str | None = None
    for line_no, line in enumerate(header.splitlines(), start=2):
        if line.startswith("  - "):
            if active_list is None:
                raise ValidationError(
                    f"front matter 游离列表项: {path.relative_to(REPO_ROOT)}:{line_no}"
                )
            metadata[active_list].append(parse_scalar(line[4:]))
            continue
        active_list = None
        if ":" not in line:
            raise ValidationError(
                f"front matter 行非法: {path.relative_to(REPO_ROOT)}:{line_no}"
            )
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key or key in metadata:
            raise ValidationError(
                f"front matter key 缺失或重复: {path.relative_to(REPO_ROOT)}:{line_no}"
            )
        if raw_value.strip():
            metadata[key] = parse_scalar(raw_value)
        else:
            metadata[key] = []
            active_list = key
    return metadata, body


def resolve_repo_file(raw_path: object, *, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValidationError(f"{label} 必须是非空仓库相对路径: {raw_path!r}")
    rel = Path(raw_path)
    if rel.is_absolute():
        raise ValidationError(f"{label} 不允许绝对路径: {raw_path}")
    resolved = (REPO_ROOT / rel).resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        raise ValidationError(f"{label} 越出仓库: {raw_path}")
    if not resolved.is_file():
        raise ValidationError(f"{label} 不存在: {raw_path}")
    return resolved


def validate() -> tuple[int, int, int]:
    errors: list[str] = []
    docs: dict[str, tuple[Path, dict[str, Any], str]] = {}
    body_chars = 0

    markdown_paths = sorted(CORPUS_ROOT.rglob("*.md"))
    for path in markdown_paths:
        try:
            metadata, body = parse_markdown(path)
            missing = REQUIRED_FRONT_MATTER - metadata.keys()
            if missing:
                raise ValidationError(
                    f"front matter 缺字段 {sorted(missing)}: {path.relative_to(REPO_ROOT)}"
                )
            document_id = metadata["document_id"]
            if not isinstance(document_id, str) or not document_id:
                raise ValidationError(f"document_id 非法: {path.relative_to(REPO_ROOT)}")
            if document_id in docs:
                previous = docs[document_id][0].relative_to(REPO_ROOT)
                raise ValidationError(
                    f"document_id 重复 {document_id}: {previous}, {path.relative_to(REPO_ROOT)}"
                )
            if metadata["status"] not in ALLOWED_STATUSES:
                raise ValidationError(
                    f"status 非法 {metadata['status']!r}: {path.relative_to(REPO_ROOT)}"
                )
            source_files = metadata["source_files"]
            if not isinstance(source_files, list) or not source_files:
                raise ValidationError(f"每篇文档至少需要一个源码来源: {document_id}")
            for source_file in source_files:
                resolve_repo_file(source_file, label=f"{document_id}.source_files")
            for section in REQUIRED_SECTIONS:
                if section not in body:
                    raise ValidationError(f"正文缺少 {section}: {document_id}")
            docs[document_id] = (path, metadata, body)
            body_chars += sum(1 for char in body if not char.isspace())
        except ValidationError as exc:
            errors.append(str(exc))

    manifest: dict[str, Any] = {}
    source_map: dict[str, Any] = {}
    try:
        loaded = load_json(MANIFEST_PATH)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("documents"), list):
            raise ValidationError("corpus_manifest.json 顶层必须包含 documents 数组")
        manifest = loaded
    except ValidationError as exc:
        errors.append(str(exc))
    try:
        loaded = load_json(SOURCE_MAP_PATH)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("documents"), dict):
            raise ValidationError("source_map.json 顶层必须包含 documents 对象")
        source_map = loaded
    except ValidationError as exc:
        errors.append(str(exc))

    manifest_docs: dict[str, dict[str, Any]] = {}
    fact_ids: set[str] = set()
    fact_count = 0
    if manifest:
        for index, item in enumerate(manifest["documents"]):
            label = f"manifest.documents[{index}]"
            try:
                if not isinstance(item, dict):
                    raise ValidationError(f"{label} 必须是对象")
                document_id = item.get("document_id")
                if not isinstance(document_id, str) or not document_id:
                    raise ValidationError(f"{label}.document_id 非法")
                if document_id in manifest_docs:
                    raise ValidationError(f"manifest document_id 重复: {document_id}")
                manifest_docs[document_id] = item

                status = item.get("status")
                if status not in ALLOWED_STATUSES:
                    raise ValidationError(f"manifest status 非法: {document_id}: {status!r}")
                raw_path = item.get("path")
                doc_path = resolve_repo_file(f"eval/rag/{raw_path}", label=f"{document_id}.path")
                if not doc_path.is_relative_to(CORPUS_ROOT):
                    raise ValidationError(f"manifest 文档不在 corpus 下: {raw_path}")

                source_files = item.get("source_files")
                if not isinstance(source_files, list) or not source_files:
                    raise ValidationError(f"manifest 每篇至少需要一个源码来源: {document_id}")
                for source_file in source_files:
                    resolve_repo_file(source_file, label=f"{document_id}.manifest.source_files")

                facts = item.get("facts")
                if not isinstance(facts, list):
                    raise ValidationError(f"facts 必须是数组: {document_id}")
                if status == "planned" and facts:
                    raise ValidationError(f"planned 文档不得包含已实现 facts: {document_id}")
                if status != "planned" and not facts:
                    raise ValidationError(f"非 planned 文档至少需要一个 fact: {document_id}")
                for fact in facts:
                    if not isinstance(fact, dict):
                        raise ValidationError(f"fact 必须是对象: {document_id}")
                    fact_id = fact.get("fact_id")
                    if not isinstance(fact_id, str) or not fact_id:
                        raise ValidationError(f"fact_id 非法: {document_id}")
                    if fact_id in fact_ids:
                        raise ValidationError(f"fact_id 重复: {fact_id}")
                    fact_ids.add(fact_id)
                    fact_count += 1
                    if not isinstance(fact.get("statement"), str) or not fact["statement"].strip():
                        raise ValidationError(f"fact statement 为空: {fact_id}")
                    if fact.get("confidence") != "verified":
                        raise ValidationError(f"fact confidence 必须为 verified: {fact_id}")
                    if not isinstance(fact.get("source_symbol"), str) or not fact["source_symbol"].strip():
                        raise ValidationError(f"fact source_symbol 为空: {fact_id}")
                    source_file = fact.get("source_file")
                    resolve_repo_file(source_file, label=f"{fact_id}.source_file")
                    if source_file not in source_files:
                        raise ValidationError(f"fact 来源未列入文档 source_files: {fact_id}")
            except ValidationError as exc:
                errors.append(str(exc))

    doc_ids = set(docs)
    manifest_ids = set(manifest_docs)
    for missing_id in sorted(doc_ids - manifest_ids):
        errors.append(f"Markdown 未被 manifest 引用: {missing_id}")
    for missing_id in sorted(manifest_ids - doc_ids):
        errors.append(f"manifest 引用的文档不存在或不可解析: {missing_id}")

    for document_id in sorted(doc_ids & manifest_ids):
        path, metadata, _ = docs[document_id]
        item = manifest_docs[document_id]
        expected_path = path.relative_to(RAG_ROOT).as_posix()
        if item.get("path") != expected_path:
            errors.append(
                f"manifest path 不一致: {document_id}: {item.get('path')!r} != {expected_path!r}"
            )
        for key in ("title", "module", "status"):
            if item.get(key) != metadata.get(key):
                errors.append(f"front matter 与 manifest 的 {key} 不一致: {document_id}")
        if item.get("source_files") != metadata.get("source_files"):
            errors.append(f"front matter 与 manifest 的 source_files 不一致: {document_id}")

    mapped_docs = source_map.get("documents", {}) if source_map else {}
    if set(mapped_docs) != manifest_ids:
        for missing_id in sorted(manifest_ids - set(mapped_docs)):
            errors.append(f"source_map 缺少文档: {missing_id}")
        for extra_id in sorted(set(mapped_docs) - manifest_ids):
            errors.append(f"source_map 存在未知文档: {extra_id}")
    for document_id in sorted(manifest_ids & set(mapped_docs)):
        mapping = mapped_docs[document_id]
        item = manifest_docs[document_id]
        if not isinstance(mapping, dict):
            errors.append(f"source_map 文档项必须是对象: {document_id}")
            continue
        expected_facts = [fact["fact_id"] for fact in item.get("facts", []) if isinstance(fact, dict)]
        if mapping.get("source_files") != item.get("source_files"):
            errors.append(f"source_map source_files 不一致: {document_id}")
        if mapping.get("fact_ids") != expected_facts:
            errors.append(f"source_map fact_ids 不一致: {document_id}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
    return len(docs), body_chars, fact_count


def main() -> None:
    documents, body_chars, facts = validate()
    print("Corpus validation passed")
    print(f"documents: {documents}")
    print(f"body_chars: {body_chars}")
    print(f"facts: {facts}")


if __name__ == "__main__":
    main()
