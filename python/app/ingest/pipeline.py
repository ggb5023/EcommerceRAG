"""Deterministic local parsing, chunking, and authorization-aware retrieval.

This module is intentionally independent from the M1 gRPC fixture.  It provides
the same evidence shape needed by a future ingestion worker without claiming a
real embedding provider or production authorization service.
"""
from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedElement:
    document_id: str
    document_version_id: str
    title: str
    heading: tuple[str, ...]
    content: str
    source_position: dict[str, int | None]
    metadata: dict[str, Any]
    disclosure_class: str
    effective_from: str | None
    effective_to: str | None
    element_type: str = "text"


@dataclass(frozen=True)
class Chunk:
    document_id: str
    version_id: str
    chunk_id: str
    title: str
    heading: tuple[str, ...]
    content: str
    source_ref: str
    tenant_id: str
    shop_id: str
    disclosure_class: str
    effective_from: str | None
    effective_to: str | None
    section_seq: int
    chunk_index: int
    split_reason: str
    chunk_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Parser(Protocol):
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]: ...


def _common(document_id: str, version_id: str, metadata: dict[str, Any], title: str,
            content: str, position: dict[str, int | None], *, heading: tuple[str, ...] = (),
            element_type: str = "text") -> ParsedElement:
    return ParsedElement(document_id, version_id, title, heading, content.strip(), position,
                         dict(metadata), metadata["disclosure_class"], metadata.get("effective_from"),
                         metadata.get("effective_to"), element_type)


class MarkdownParser:
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]:
        text = path.read_text(encoding="utf-8")
        elements: list[ParsedElement] = []
        stack: list[tuple[int, str]] = []
        buffer: list[str] = []
        start_line = 1
        title = metadata.get("title", document_id)

        def flush(end_line: int) -> None:
            nonlocal buffer, start_line
            body = "\n".join(buffer).strip()
            if body:
                elements.append(_common(document_id, version_id, metadata, title, body,
                                        {"line_start": start_line, "line_end": end_line},
                                        heading=tuple(item[1] for item in stack)))
            buffer = []

        lines = text.splitlines()
        in_fence = False
        for number, line in enumerate(lines, 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                buffer.append(line)
                continue
            heading_match = None if in_fence else re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if heading_match:
                flush(number - 1)
                level, heading = len(heading_match.group(1)), heading_match.group(2)
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, heading))
                title = heading
                start_line = number
                continue
            if not buffer:
                start_line = number
            buffer.append(line)
        flush(len(lines))
        return elements


class CSVParser:
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]:
        elements: list[ParsedElement] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), 2):
                values = [f"{key}: {value}" for key, value in row.items() if value not in (None, "")]
                row_metadata = dict(metadata)
                row_tenant = row.get("tenant_id")
                row_shop = row.get("shop_id")
                if row_tenant:
                    row_metadata["tenant_id"] = row_tenant
                if row_shop:
                    row_metadata["shop_id"] = row_shop
                if row.get("fact_type"):
                    row_metadata["fact_type"] = row["fact_type"]
                elements.append(_common(document_id, version_id, row_metadata, metadata.get("title", document_id),
                                        "\n".join(values), {"line_start": row_number, "line_end": row_number},
                                        element_type="row"))
        return elements


class DOCXParser:
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]:
        try:
            from docx import Document
        except ImportError:
            return self._parse_minimal_docx(path, document_id=document_id, version_id=version_id, metadata=metadata)
        document = Document(path)
        elements: list[ParsedElement] = []
        heading: list[str] = []
        for index, paragraph in enumerate(document.paragraphs, 1):
            content = paragraph.text.strip()
            if not content:
                continue
            style = paragraph.style.name.lower() if paragraph.style else ""
            match = re.search(r"heading\s*(\d+)", style)
            if match:
                level = int(match.group(1))
                del heading[level - 1:]
                heading.append(content)
            else:
                elements.append(_common(document_id, version_id, metadata, metadata.get("title", document_id), content,
                                        {"line_start": index, "line_end": index}, heading=tuple(heading)))
        for table_index, table in enumerate(document.tables, 1):
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
            if rows:
                elements.append(_common(document_id, version_id, metadata, metadata.get("title", document_id),
                                        "\n".join(rows), {"line_start": None, "line_end": None, "table": table_index},
                                        heading=tuple(heading), element_type="table"))
        return elements

    @staticmethod
    def _parse_minimal_docx(path: Path, *, document_id: str, version_id: str,
                            metadata: dict[str, Any]) -> list[ParsedElement]:
        """Fallback parser for basic WordprocessingML without optional dependencies."""
        from xml.etree import ElementTree
        from zipfile import ZipFile
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        elements: list[ParsedElement] = []
        for index, paragraph in enumerate(root.findall(".//w:body/w:p", ns), 1):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
            if text:
                elements.append(_common(document_id, version_id, metadata, metadata.get("title", document_id), text,
                                        {"line_start": index, "line_end": index}))
        for table_index, table in enumerate(root.findall(".//w:body/w:tbl", ns), 1):
            rows = []
            for row in table.findall("./w:tr", ns):
                rows.append(" | ".join("".join(node.text or "" for node in cell.findall(".//w:t", ns))
                                      for cell in row.findall("./w:tc", ns)))
            if rows:
                elements.append(_common(document_id, version_id, metadata, metadata.get("title", document_id),
                                        "\n".join(rows), {"line_start": None, "line_end": None, "table": table_index},
                                        element_type="table"))
        return elements


def parser_for(format_name: str) -> Parser:
    return {"markdown": MarkdownParser(), "csv": CSVParser(), "docx": DOCXParser()}[format_name]


def chunk_elements(elements: Iterable[ParsedElement], *, max_chars: int = 700) -> list[Chunk]:
    chunks: list[Chunk] = []
    section_seq = 0
    for element in elements:
        if not element.content:
            continue
        section_seq += 1
        pieces = [element.content[i:i + max_chars] for i in range(0, len(element.content), max_chars)]
        for index, piece in enumerate(pieces):
            reason = "hard_split" if len(pieces) > 1 else ("row_boundary" if element.element_type == "row" else "element_boundary")
            chunk_hash = digest(f"{element.document_id}\0{element.document_version_id}\0{element.source_position}\0{piece}")
            chunks.append(Chunk(element.document_id, element.document_version_id, "chunk-" + chunk_hash[:20],
                                element.title, element.heading, piece, f"local://{element.document_id}/{chunk_hash}",
                                element.metadata["tenant_id"], element.metadata["shop_id"], element.disclosure_class,
                                element.effective_from, element.effective_to, section_seq, index, reason, chunk_hash,
                                element.metadata))
    return chunks


class LocalIndex:
    """Keyword/deterministic-vector compatible local index; no external model."""
    def __init__(self, chunks: Iterable[Chunk] = ()) -> None:
        self.chunks = list(chunks)
        self._active_versions: dict[tuple[str, str], str] = {}
        for chunk in self.chunks:
            self._active_versions.setdefault((chunk.tenant_id, chunk.document_id), chunk.version_id)

    def add(self, chunks: Iterable[Chunk]) -> None:
        additions = list(chunks)
        self.chunks.extend(additions)
        for chunk in additions:
            self._active_versions.setdefault((chunk.tenant_id, chunk.document_id), chunk.version_id)

    def activate(self, tenant_id: str, document_id: str, version_id: str) -> None:
        if not any(chunk.tenant_id == tenant_id and chunk.document_id == document_id and chunk.version_id == version_id
                   for chunk in self.chunks):
            raise ValueError("cannot activate an unknown document version")
        self._active_versions[(tenant_id, document_id)] = version_id

    @staticmethod
    def _tokens(value: str) -> set[str]:
        normalized = value.lower()
        tokens = set(re.findall(r"[a-z0-9_]+", normalized))
        for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
            tokens.update(run[i:j] for size in (2, 3, 4) for i in range(len(run))
                          for j in (i + size,) if j <= len(run))
        return tokens

    def search(self, query: str, *, tenant_id: str, shop_id: str | None = None,
               allowed_shop_ids: set[str] = frozenset(), role: str = "viewer",
               business_date: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        q = self._tokens(query)
        today = date.fromisoformat(business_date) if business_date else None
        results: list[tuple[float, Chunk]] = []
        for chunk in self.chunks:
            if chunk.tenant_id != tenant_id or (shop_id and chunk.shop_id != shop_id):
                continue
            if self._active_versions.get((chunk.tenant_id, chunk.document_id)) != chunk.version_id:
                continue
            if not shop_id and chunk.shop_id not in allowed_shop_ids:
                continue
            if chunk.disclosure_class != "external_allowed" and role not in {"admin", "owner"}:
                continue
            if today:
                start = date.fromisoformat(chunk.effective_from) if chunk.effective_from else None
                end = date.fromisoformat(chunk.effective_to) if chunk.effective_to else None
                if (start and today < start) or (end and today >= end):
                    continue
            searchable = chunk.content + " " + " ".join(chunk.heading)
            if chunk.metadata.get("fact_type"):
                fact_type = str(chunk.metadata["fact_type"])
                searchable += " " + fact_type + " " + {"stock": "库存", "price": "价格", "order_status": "订单"}.get(fact_type, "")
            overlap = len(q & self._tokens(searchable))
            if overlap:
                results.append((overlap / max(len(q), 1), chunk))
        results.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [{"document_id": chunk.document_id, "version_id": chunk.version_id, "chunk_id": chunk.chunk_id,
                 "source_ref": chunk.source_ref, "content": chunk.content, "score": score, "rank": rank,
                 "citation_index": rank, "disclosure_class": chunk.disclosure_class, "is_mock": True,
                 "tenant_id": chunk.tenant_id, "shop_id": chunk.shop_id, "metadata": chunk.metadata}
                for rank, (score, chunk) in enumerate(results[:limit], 1)]


def load_manifest_index(manifest_path: Path, *, version_id: str | None = None) -> LocalIndex:
    from .cli import validate_manifest
    result = validate_manifest(manifest_path)
    if not result.ok:
        raise ValueError("; ".join(result.errors))
    chunks: list[Chunk] = []
    for document_id, info in result.files.items():
        # ACL manifests are control-plane fixtures, never customer evidence.
        if info["format"] == "yaml":
            continue
        metadata = {**info, "title": document_id}
        current_version = version_id or "v-" + digest(info["sha256"] + result.manifest["pipeline_version"])[:16]
        elements = parser_for(info["format"]).parse(result.root / info["path"], document_id=document_id,
                                                      version_id=current_version, metadata=metadata)
        chunks.extend(chunk_elements(elements))
    return LocalIndex(chunks)
