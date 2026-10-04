"""Deterministic local parsing, chunking, and authorization-aware retrieval.

This module is intentionally independent from the M1 gRPC fixture.  It provides
the same evidence shape needed by a future ingestion worker without claiming a
real embedding provider or production authorization service.
"""
from __future__ import annotations

import csv
import hashlib
import json
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
    source_position: dict[str, Any]
    metadata: dict[str, Any]
    disclosure_class: str
    effective_from: str | None
    effective_to: str | None
    element_type: str = "text"
    table_body: Any = None
    table_caption: str | None = None
    image_refs: tuple[str, ...] = ()
    bbox: Any = None
    warning: str | None = None
    text_level: int | None = None
    page_no: int | None = None


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
    source_position: dict[str, Any] = field(default_factory=dict)
    rule_version: str = "structured-v2"
    content_type: str = "text"


class Parser(Protocol):
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]: ...


def _common(document_id: str, version_id: str, metadata: dict[str, Any], title: str,
            content: str, position: dict[str, Any], *, heading: tuple[str, ...] = (),
            element_type: str = "text", table_body: Any = None,
            table_caption: str | None = None, image_refs: Iterable[str] = (),
            bbox: Any = None, warning: str | None = None,
            text_level: int | None = None, page_no: int | None = None) -> ParsedElement:
    return ParsedElement(
        document_id=document_id, document_version_id=version_id, title=title,
        heading=heading, content=content.strip(), source_position=position,
        metadata=dict(metadata), disclosure_class=metadata["disclosure_class"],
        effective_from=metadata.get("effective_from"), effective_to=metadata.get("effective_to"),
        element_type=element_type, table_body=table_body, table_caption=table_caption,
        image_refs=tuple(image_refs), bbox=bbox, warning=warning,
        text_level=text_level, page_no=page_no,
    )


class MarkdownParser:
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]:
        text = path.read_text(encoding="utf-8")
        elements: list[ParsedElement] = []
        stack: list[tuple[int, str]] = []
        buffer: list[str] = []
        start_line = 1
        title = metadata.get("title", document_id)

        def flush(end_line: int, *, element_type: str = "text", **kwargs: Any) -> None:
            nonlocal buffer, start_line
            body = "\n".join(buffer).strip()
            if body:
                elements.append(_common(
                    document_id, version_id, metadata, title, body,
                    {"line_start": start_line, "line_end": end_line},
                    heading=tuple(item[1] for item in stack), element_type=element_type,
                    **kwargs,
                ))
            buffer = []

        def is_table_row(value: str) -> bool:
            stripped = value.strip()
            return stripped.startswith("|") and stripped.endswith("|")

        def is_table_separator(value: str) -> bool:
            cells = value.strip().strip("|").split("|")
            return bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)

        def table_body(rows: list[str]) -> list[list[str]]:
            return [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]

        lines = text.splitlines()
        in_fence = False
        fence_info = ""
        fence_start = 1
        fence_lines: list[str] = []
        index = 0
        while index < len(lines):
            number, line = index + 1, lines[index]
            fence_match = re.match(r"^\s*```\s*([^\s]*)?.*$", line)
            if not in_fence and fence_match:
                flush(number - 1)
                in_fence = True
                fence_info = fence_match.group(1) or ""
                fence_start = number
                fence_lines = []
                index += 1
                continue
            if in_fence:
                if line.strip().startswith("```"):
                    body = "\n".join(fence_lines).strip()
                    if body:
                        elements.append(_common(
                            document_id, version_id, metadata, title, body,
                            {"line_start": fence_start, "line_end": number},
                            heading=tuple(item[1] for item in stack), element_type="code",
                            warning=f"code_language:{fence_info}" if fence_info else None,
                        ))
                    in_fence = False
                    fence_lines = []
                else:
                    fence_lines.append(line)
                index += 1
                continue

            heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if heading_match:
                flush(number - 1)
                level, heading = len(heading_match.group(1)), heading_match.group(2)
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, heading))
                title = heading
                elements.append(_common(
                    document_id, version_id, metadata, title, heading,
                    {"line_start": number, "line_end": number},
                    heading=tuple(item[1] for item in stack), element_type="heading",
                    text_level=level,
                ))
                start_line = number + 1
                index += 1
                continue

            if is_table_row(line) and index + 1 < len(lines) and is_table_separator(lines[index + 1]):
                flush(number - 1)
                table_start = number
                rows = [line, lines[index + 1]]
                index += 2
                while index < len(lines) and is_table_row(lines[index]):
                    rows.append(lines[index])
                    index += 1
                parsed_rows = table_body(rows)
                elements.append(_common(
                    document_id, version_id, metadata, title,
                    "\n".join(" | ".join(row) for row in parsed_rows),
                    {"line_start": table_start, "line_end": index},
                    heading=tuple(item[1] for item in stack), element_type="table",
                    table_body=parsed_rows,
                ))
                buffer = []
                start_line = index + 1
                continue

            image_match = re.match(r"^\s*!\[([^]]*)\]\(([^)]+)\)\s*$", line)
            if image_match:
                flush(number - 1)
                alt_text, image_ref = image_match.groups()
                elements.append(_common(
                    document_id, version_id, metadata, title, alt_text or image_ref,
                    {"line_start": number, "line_end": number},
                    heading=tuple(item[1] for item in stack), element_type="image",
                    image_refs=(image_ref,), warning="image_reference_only",
                ))
                start_line = number + 1
                index += 1
                continue

            if re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line):
                if not buffer:
                    start_line = number
                buffer.append(line)
                next_line = lines[index + 1] if index + 1 < len(lines) else ""
                if not re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", next_line):
                    flush(number, element_type="list")
                index += 1
                continue

            if not buffer:
                start_line = number
            buffer.append(line)
            index += 1

        if in_fence:
            body = "\n".join(fence_lines).strip()
            if body:
                elements.append(_common(
                    document_id, version_id, metadata, title, body,
                    {"line_start": fence_start, "line_end": len(lines)},
                    heading=tuple(item[1] for item in stack), element_type="code",
                    warning="unterminated_code_fence",
                ))
        else:
            flush(len(lines))
        return elements


class CSVParser:
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]:
        elements: list[ParsedElement] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), 2):
                values = [f"{key}: {value}" for key, value in row.items() if value not in (None, "")]
                row_tenant = row.get("tenant_id")
                row_shop = row.get("shop_id")
                # A manifest document may point at a shared CSV (for example
                # the synthetic products file). Do not let rows belonging to
                # another tenant or shop inherit this document's identity.
                if row_tenant and row_tenant != metadata.get("tenant_id"):
                    continue
                if row_shop and row_shop != metadata.get("shop_id"):
                    continue
                row_metadata = dict(metadata)
                if row_tenant:
                    row_metadata["tenant_id"] = row_tenant
                if row_shop:
                    row_metadata["shop_id"] = row_shop
                if row.get("fact_type"):
                    row_metadata["fact_type"] = row["fact_type"]
                elements.append(_common(
                    document_id, version_id, row_metadata, metadata.get("title", document_id),
                    "\n".join(values), {"line_start": row_number, "line_end": row_number},
                    element_type="table", table_body=[values],
                ))
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
                elements.append(_common(
                    document_id, version_id, metadata, content, content,
                    {"line_start": index, "line_end": index},
                    heading=tuple(heading), element_type="heading", text_level=level,
                ))
            else:
                elements.append(_common(document_id, version_id, metadata, metadata.get("title", document_id), content,
                                        {"line_start": index, "line_end": index}, heading=tuple(heading)))
        for table_index, table in enumerate(document.tables, 1):
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
            if rows:
                elements.append(_common(document_id, version_id, metadata, metadata.get("title", document_id),
                                        "\n".join(rows), {"line_start": None, "line_end": None, "table": table_index},
                                        heading=tuple(heading), element_type="table", table_body=[row.split(" | ") for row in rows]))
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
                                        element_type="table", table_body=[row.split(" | ") for row in rows]))
        return elements


def parser_for(format_name: str) -> Parser:
    return {"markdown": MarkdownParser(), "csv": CSVParser(), "docx": DOCXParser()}[format_name]


def chunk_elements(elements: Iterable[ParsedElement], *, max_chars: int = 700) -> list[Chunk]:
    """Compatibility entry point for callers of the pre-structured slicer.

    The old name remains part of the local API, but all callers now receive the
    versioned structure-aware contract.  Keeping one implementation avoids
    producing two incompatible source-position and hash formats.
    """
    return chunk_elements_v2(elements, max_chars=max_chars)


CHUNK_RULE_VERSION = "structured-v2"
_SENTENCE_ENDINGS = frozenset("。！？；.!?;")
_CONTENT_TYPES = frozenset({"text", "heading", "table", "image", "list", "code"})


def _element_boundary_reason(element_type: str) -> str:
    """Give single-element chunks a stable reason that preserves structure."""
    normalized = "table" if element_type == "row" else element_type
    if normalized in _CONTENT_TYPES:
        return f"{normalized}_boundary"
    return "element_boundary"


def _sentence_ranges(content: str) -> list[tuple[int, int]]:
    """Return exact source ranges split at punctuation or paragraph breaks.

    Ranges include every original character.  In particular, no separator is
    inserted between Chinese sentences, so a chunk's offsets always index the
    element's original content.
    """
    if not content:
        return []
    ranges: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(content)
    while index < length:
        character = content[index]
        boundary: int | None = None
        if character == "\n":
            boundary = index + 1
            while boundary < length and content[boundary] == "\n":
                boundary += 1
        elif character in _SENTENCE_ENDINGS:
            # A period in a decimal is not a useful sentence boundary.  Other
            # punctuation is split even without whitespace for Chinese text.
            if not (character == "." and index > 0 and index + 1 < length
                    and content[index - 1].isdigit() and content[index + 1].isdigit()):
                boundary = index + 1
        if boundary is not None and boundary > start:
            ranges.append((start, boundary))
            start = boundary
            index = boundary
        else:
            index += 1
    if start < length:
        ranges.append((start, length))
    return ranges


def _split_structured_text(content: str, max_chars: int) -> list[tuple[str, str, int, int]]:
    """Split into exact source ranges, using hard splits only as a last resort."""
    ranges = _sentence_ranges(content)
    if not ranges:
        return []
    pieces: list[tuple[str, str, int, int]] = []
    current_start: int | None = None
    current_end = 0
    for unit_start, unit_end in ranges:
        unit_length = unit_end - unit_start
        if unit_length > max_chars:
            if current_start is not None:
                pieces.append((content[current_start:current_end], "sentence_boundary",
                               current_start, current_end))
                current_start = None
            for start in range(unit_start, unit_end, max_chars):
                end = min(start + max_chars, unit_end)
                pieces.append((content[start:end], "hard_split", start, end))
            continue
        if current_start is None:
            current_start, current_end = unit_start, unit_end
        elif unit_end - current_start <= max_chars:
            current_end = unit_end
        else:
            # A punctuation-only Chinese run has no whitespace boundary to
            # use once the budget is reached.  Keep the exact sentence range,
            # but label the budget-forced cut as hard_split for observability.
            reason = ("hard_split" if not re.search(r"\s", content)
                      else "sentence_boundary")
            pieces.append((content[current_start:current_end], reason,
                           current_start, current_end))
            current_start, current_end = unit_start, unit_end
    if current_start is not None:
        reason = "element_boundary" if len(pieces) == 0 else "sentence_boundary"
        pieces.append((content[current_start:current_end], reason, current_start, current_end))
    return pieces


def _chunk_metadata(element: ParsedElement) -> dict[str, Any]:
    metadata = dict(element.metadata)
    structured = {
        "table_body": element.table_body,
        "table_caption": element.table_caption,
        "image_refs": list(element.image_refs),
        "bbox": element.bbox,
        "warning": element.warning,
        "text_level": element.text_level,
        "page_no": element.page_no,
    }
    for key, value in structured.items():
        if value not in (None, [], ()):
            metadata.setdefault(key, value)
    return metadata


def chunk_elements_v2(elements: Iterable[ParsedElement], *, max_chars: int = 700,
                      overlap_chars: int = 0,
                      rule_version: str = CHUNK_RULE_VERSION) -> list[Chunk]:
    """Structure-aware deterministic chunks for parser/MinerU experiments.

    Parsed elements already represent section/table/row boundaries.  This
    function preserves those boundaries and only splits oversized text at
    sentence boundaries before falling back to a hard split.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be between 0 and max_chars - 1")
    chunks: list[Chunk] = []
    section_seq = 0
    for element in elements:
        if not element.content:
            continue
        section_seq += 1
        pieces = _split_structured_text(element.content, max_chars)
        for section_index, (_, base_reason, base_start, base_end) in enumerate(pieces):
            # Include overlap from the previous base range while preserving
            # exact offsets.  Overlap is scoped to one parsed element.
            start = max(0, base_start - overlap_chars) if section_index else base_start
            offset = base_end
            piece = element.content[start:offset]
            reason = (_element_boundary_reason(element.element_type)
                      if base_reason == "element_boundary" else base_reason)
            source_position = dict(element.source_position)
            source_position["char_start"] = start
            source_position["char_end"] = offset
            content_type = element.element_type if element.element_type in _CONTENT_TYPES else "text"
            metadata = _chunk_metadata(element)
            canonical = json.dumps({
                "document_id": element.document_id,
                "version_id": element.document_version_id,
                "source_position": source_position,
                "content": piece,
                "content_type": content_type,
                "rule_version": rule_version,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            chunk_hash = digest(canonical)
            chunks.append(Chunk(
                element.document_id, element.document_version_id, "chunk-" + chunk_hash[:20],
                element.title, element.heading, piece,
                f"local://{element.document_id}/{chunk_hash}",
                element.metadata["tenant_id"], element.metadata["shop_id"],
                element.disclosure_class, element.effective_from, element.effective_to,
                section_seq, section_index, reason, chunk_hash, metadata,
                source_position, rule_version, content_type,
            ))
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
               business_date: str | None = None, limit: int = 5,
               allowed_document_ids: set[str] | None = None,
               adjacent_window: int = 0) -> list[dict[str, Any]]:
        if adjacent_window not in (0, 1) or not 1 <= limit <= 5:
            raise ValueError("search supports 1-5 hits and at most one adjacent chunk per side")
        if not tenant_id or not allowed_shop_ids or (shop_id and shop_id not in allowed_shop_ids):
            return []
        q = self._tokens(query)
        today = date.fromisoformat(business_date) if business_date else None
        results: list[tuple[float, Chunk]] = []
        def permitted(chunk: Chunk) -> bool:
            if allowed_document_ids is not None and chunk.document_id not in allowed_document_ids:
                return False
            if chunk.tenant_id != tenant_id or (shop_id and chunk.shop_id != shop_id):
                return False
            if self._active_versions.get((chunk.tenant_id, chunk.document_id)) != chunk.version_id:
                return False
            if chunk.shop_id not in allowed_shop_ids:
                return False
            if chunk.disclosure_class != "external_allowed" and role not in {"admin", "owner"}:
                return False
            if today:
                start = date.fromisoformat(chunk.effective_from) if chunk.effective_from else None
                end = date.fromisoformat(chunk.effective_to) if chunk.effective_to else None
                if (start and today < start) or (end and today >= end):
                    return False
            return True

        for chunk in self.chunks:
            if not permitted(chunk):
                continue
            searchable = chunk.content + " " + " ".join(chunk.heading)
            if chunk.metadata.get("fact_type"):
                fact_type = str(chunk.metadata["fact_type"])
                searchable += " " + fact_type + " " + {"stock": "库存", "price": "价格", "order_status": "订单"}.get(fact_type, "")
            overlap = len(q & self._tokens(searchable))
            if overlap:
                results.append((overlap / max(len(q), 1), chunk))
        # Headings remain useful evidence, but a record/paragraph carrying the
        # answer should win an otherwise equal lexical score.  This keeps the
        # structured parser's standalone heading elements from outranking the
        # product or policy content they introduce.
        results.sort(key=lambda item: (-item[0], item[1].content_type == "heading", item[1].chunk_id))
        expanded: list[tuple[float, Chunk]] = []
        seen: set[str] = set()
        for score, hit in results[:limit]:
            candidates = [hit]
            if adjacent_window:
                candidates += sorted((c for c in self.chunks
                                      if (c.tenant_id, c.shop_id, c.document_id, c.version_id, c.section_seq)
                                      == (hit.tenant_id, hit.shop_id, hit.document_id, hit.version_id, hit.section_seq)
                                      and abs(c.chunk_index - hit.chunk_index) == 1 and permitted(c)),
                                     key=lambda c: c.chunk_index)
            for chunk in candidates:
                if chunk.chunk_id not in seen:
                    seen.add(chunk.chunk_id)
                    expanded.append((score, chunk))
        return [{"document_id": chunk.document_id, "version_id": chunk.version_id, "chunk_id": chunk.chunk_id,
                 "source_ref": chunk.source_ref, "content": chunk.content, "score": score, "rank": rank,
                 "citation_index": rank, "disclosure_class": chunk.disclosure_class, "is_mock": True,
                 "tenant_id": chunk.tenant_id, "shop_id": chunk.shop_id, "metadata": chunk.metadata}
                for rank, (score, chunk) in enumerate(expanded, 1)]


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
        chunks.extend(chunk_elements_v2(elements))
    return LocalIndex(chunks)
