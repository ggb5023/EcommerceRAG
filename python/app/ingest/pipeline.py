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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar, Protocol


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
        # ``utf-8-sig`` consumes an optional BOM only at the decoded stream
        # boundary.  The uploaded bytes and their source hash remain
        # unchanged, while tenant/shop headers still participate in scope
        # filtering for files exported by spreadsheet tools.
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            headers: list[str] | None = None
            previous_line_end = 0
            record_number = 0
            for raw_row in reader:
                line_end = reader.line_num
                # Empty physical rows do not create records or alter the
                # source range of the next record.
                if not raw_row or all(not value.strip() for value in raw_row):
                    continue
                if headers is None:
                    headers = list(raw_row)
                    if any(not header.strip() for header in headers):
                        raise ValueError("csv_header_empty")
                    if len(set(headers)) != len(headers):
                        raise ValueError("csv_header_duplicate")
                    previous_line_end = line_end
                    continue
                if len(raw_row) != len(headers):
                    raise ValueError(f"csv_column_mismatch:row={line_end}")
                record_number += 1
                # csv.reader exposes the physical end line.  Count embedded
                # newlines in fields to recover the physical start line for
                # quoted multiline records without rewriting source bytes.
                physical_lines = max((value.count("\n") for value in raw_row), default=0) + 1
                line_start = line_end - physical_lines + 1
                if line_start <= previous_line_end:
                    line_start = previous_line_end + 1
                previous_line_end = line_end
                row = dict(zip(headers, raw_row))
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
                    "\n".join(values), {"line_start": line_start, "line_end": line_end,
                                        "record_number": record_number},
                    element_type="table", table_body=[values],
                ))
        return elements


class DOCXParser:
    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]:
        try:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError:
            return self._parse_minimal_docx(path, document_id=document_id, version_id=version_id, metadata=metadata)
        document = Document(path)
        elements: list[ParsedElement] = []
        heading: list[str] = []
        table_index = 0
        for block_index, child in enumerate(document.element.body.iterchildren(), 1):
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = Paragraph(child, document)
                content = paragraph.text.strip()
                if not content:
                    continue
                style = paragraph.style.name if paragraph.style else ""
                level = self._heading_level(style)
                position = {"block_index": block_index}
                if level is not None:
                    del heading[level - 1:]
                    heading.append(content)
                    elements.append(_common(
                        document_id, version_id, metadata, content, content, position,
                        heading=tuple(heading), element_type="heading", text_level=level,
                    ))
                else:
                    elements.append(_common(document_id, version_id, metadata,
                                            metadata.get("title", document_id), content,
                                            position, heading=tuple(heading)))
            elif tag == "tbl":
                table_index += 1
                element = self._table_element(
                    Table(child, document), document_id=document_id,
                    version_id=version_id, metadata=metadata,
                    heading=tuple(heading), table_index=table_index,
                    block_index=block_index,
                )
                if element is not None:
                    elements.append(element)
        return elements

    @staticmethod
    def _heading_level(style: str) -> int | None:
        match = re.search(r"heading\s*(\d+)", style.lower())
        return int(match.group(1)) if match else None

    @staticmethod
    def _table_element(table: Any, *, document_id: str, version_id: str,
                       metadata: dict[str, Any], heading: tuple[str, ...],
                       table_index: int, block_index: int) -> ParsedElement | None:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        if not rows:
            return None
        repeated_header = bool(rows[0]) and any(row == rows[0] for row in rows[1:])
        return _common(
            document_id, version_id, metadata, metadata.get("title", document_id),
            "\n".join(" | ".join(row) for row in rows),
            {"block_index": block_index, "table": table_index}, heading=heading,
            element_type="table", table_body=rows,
            warning="repeated_table_header" if repeated_header else None,
        )

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
        heading: list[str] = []
        table_index = 0
        body = root.find("./w:body", ns)
        if body is None:
            return elements
        for block_index, block in enumerate(list(body), 1):
            tag = block.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = "".join(node.text or "" for node in block.findall(".//w:t", ns)).strip()
                if not text:
                    continue
                style_node = block.find("./w:pPr/w:pStyle", ns)
                style = style_node.attrib.get("{" + ns["w"] + "}val", "") if style_node is not None else ""
                level = DOCXParser._heading_level(style)
                if level is not None:
                    del heading[level - 1:]
                    heading.append(text)
                    elements.append(_common(
                        document_id, version_id, metadata, text, text,
                        {"block_index": block_index}, heading=tuple(heading),
                        element_type="heading", text_level=level,
                    ))
                else:
                    elements.append(_common(
                        document_id, version_id, metadata, metadata.get("title", document_id), text,
                        {"block_index": block_index}, heading=tuple(heading),
                    ))
            elif tag == "tbl":
                table_index += 1
                rows = [["".join(node.text or "" for node in cell.findall(".//w:t", ns))
                         for cell in row.findall("./w:tc", ns)]
                        for row in block.findall("./w:tr", ns)]
                if rows:
                    repeated_header = bool(rows[0]) and any(row == rows[0] for row in rows[1:])
                    elements.append(_common(
                        document_id, version_id, metadata, metadata.get("title", document_id),
                        "\n".join(" | ".join(row) for row in rows),
                        {"block_index": block_index, "table": table_index}, heading=tuple(heading),
                        element_type="table", table_body=rows,
                        warning="repeated_table_header" if repeated_header else None,
                    ))
        return elements


class _HTMLDocumentParser(HTMLParser):
    """Parse a bounded local HTML snapshot into the common element contract.

    The parser deliberately accepts bytes that have already passed the crawler
    source policy. It never resolves URLs, fetches images, or interprets page
    instructions. Positions are source line/column pairs from ``HTMLParser``.
    """

    _HEADINGS: ClassVar[dict[str, int]] = {f"h{level}": level for level in range(1, 7)}
    _BLOCKS: ClassVar[set[str]] = {"p", "li", "pre", "blockquote"}
    _IGNORED: ClassVar[set[str]] = {"script", "style", "noscript", "template"}

    def __init__(self, *, document_id: str, version_id: str, metadata: dict[str, Any]):
        super().__init__(convert_charrefs=True)
        self.document_id = document_id
        self.version_id = version_id
        self.metadata = metadata
        self.elements: list[ParsedElement] = []
        self.heading_stack: list[tuple[int, str]] = []
        self.active: dict[str, Any] | None = None
        self.table: dict[str, Any] | None = None
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.ignored_depth = 0

    def _position(self) -> dict[str, int]:
        line, column = self.getpos()
        return {"line": line, "column": column}

    @staticmethod
    def _text(parts: list[str], *, preserve: bool = False) -> str:
        value = "".join(parts)
        return value if preserve else " ".join(value.split())

    def _emit_active(self) -> None:
        active = self.active
        self.active = None
        if not active:
            return
        content = self._text(active["parts"], preserve=active["tag"] == "pre")
        if not content:
            return
        start = active["start"]
        end = self._position()
        element_type = active["element_type"]
        level = active.get("level")
        heading = tuple(item[1] for item in self.heading_stack)
        if level is not None:
            heading = heading + (content,)
        self.elements.append(_common(
            self.document_id,
            self.version_id,
            self.metadata,
            content if element_type == "heading" else self.metadata.get("title", self.document_id),
            content,
            {"line_start": start["line"], "line_end": end["line"],
             "column_start": start["column"], "column_end": end["column"]},
            heading=heading,
            element_type=element_type,
            text_level=level,
        ))
        if level is not None:
            while self.heading_stack and self.heading_stack[-1][0] >= level:
                self.heading_stack.pop()
            self.heading_stack.append((level, content))

    def _emit_table(self) -> None:
        table = self.table
        self.table = None
        self.current_row = None
        self.current_cell = None
        if not table:
            return
        rows = [row for row in table["rows"] if any(cell.strip() for cell in row)]
        if not rows:
            return
        start = table["start"]
        end = self._position()
        body = "\n".join(" | ".join(cell for cell in row) for row in rows)
        self.elements.append(_common(
            self.document_id,
            self.version_id,
            self.metadata,
            self.metadata.get("title", self.document_id),
            body,
            {"line_start": start["line"], "line_end": end["line"],
             "column_start": start["column"], "column_end": end["column"]},
            heading=tuple(item[1] for item in self.heading_stack),
            element_type="table",
            table_body=rows,
        ))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._IGNORED:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag == "br":
            if self.active:
                self.active["parts"].append("\n")
            return
        if tag == "img":
            values = dict(attrs)
            source = values.get("src") or ""
            alt = (values.get("alt") or "").strip()
            content = alt or source
            if content:
                position = self._position()
                self.elements.append(_common(
                    self.document_id,
                    self.version_id,
                    self.metadata,
                    self.metadata.get("title", self.document_id),
                    content,
                    {"line_start": position["line"], "line_end": position["line"],
                     "column_start": position["column"], "column_end": position["column"]},
                    heading=tuple(item[1] for item in self.heading_stack),
                    element_type="image",
                    image_refs=(source,) if source else (),
                    warning="image_reference_only",
                ))
            return
        if tag == "table" and self.table is None and self.active is None:
            self.table = {"start": self._position(), "rows": []}
            return
        if self.table is not None:
            if tag == "tr":
                self.current_row = []
                return
            if tag in {"td", "th"} and self.current_row is not None:
                self.current_cell = []
                return
            return
        if self.active is not None:
            return
        if tag in self._HEADINGS or tag in self._BLOCKS:
            self.active = {
                "tag": tag,
                "parts": [],
                "start": self._position(),
                "element_type": "heading" if tag in self._HEADINGS else ("code" if tag == "pre" else "list" if tag == "li" else "text"),
                "level": self._HEADINGS.get(tag),
            }

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if self.table is not None:
            if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
                self.current_row.append(self._text(self.current_cell))
                self.current_cell = None
                return
            if tag == "tr" and self.current_row is not None:
                self.table["rows"].append(self.current_row)
                self.current_row = None
                return
            if tag == "table":
                self._emit_table()
                return
            return
        if self.active is not None and tag == self.active["tag"]:
            self._emit_active()

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        if self.table is not None and self.current_cell is not None:
            self.current_cell.append(data)
        elif self.active is not None:
            self.active["parts"].append(data)

    def finish(self) -> list[ParsedElement]:
        if self.table is not None:
            self._emit_table()
        if self.active is not None:
            self._emit_active()
        return self.elements


class HTMLParserAdapter:
    """Parse a local crawler HTML snapshot into shared parsed elements."""

    def parse(self, path: Path, *, document_id: str, version_id: str, metadata: dict[str, Any]) -> list[ParsedElement]:
        text = path.read_text(encoding="utf-8")
        parser = _HTMLDocumentParser(document_id=document_id, version_id=version_id, metadata=metadata)
        parser.feed(text)
        parser.close()
        return parser.finish()

def parser_for(format_name: str) -> Parser:
    return {"markdown": MarkdownParser(), "csv": CSVParser(), "docx": DOCXParser(),
            "html": HTMLParserAdapter(), "crawler_html": HTMLParserAdapter()}[format_name]


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


def validate_chunk_document_versions(chunks: Iterable[Chunk]) -> list[Chunk]:
    """Validate immutable chunk identity before an index can expose evidence.

    Multiple versions of one document may coexist for an explicit activation
    switch, but every chunk in a given document version must keep one shop
    binding and every chunk ID must be unique in the index.
    """
    materialized = list(chunks)
    seen_chunk_ids: set[str] = set()
    version_scopes: dict[tuple[str, str, str], str] = {}
    for chunk in materialized:
        if not chunk.document_id or not chunk.version_id or not chunk.tenant_id or not chunk.shop_id:
            raise ValueError("document_version_binding_invalid")
        if chunk.chunk_id in seen_chunk_ids:
            raise ValueError(f"duplicate chunk_id:{chunk.chunk_id}")
        seen_chunk_ids.add(chunk.chunk_id)
        version_key = (chunk.tenant_id, chunk.document_id, chunk.version_id)
        previous_shop = version_scopes.get(version_key)
        if previous_shop is not None and previous_shop != chunk.shop_id:
            raise ValueError(f"document_version_scope_mismatch:{chunk.document_id}:{chunk.version_id}")
        version_scopes[version_key] = chunk.shop_id
    return materialized


class LocalIndex:
    """Keyword/deterministic-vector compatible local index; no external model."""
    def __init__(self, chunks: Iterable[Chunk] = ()) -> None:
        self.chunks = validate_chunk_document_versions(chunks)
        self._active_versions: dict[tuple[str, str], str] = {}
        for chunk in self.chunks:
            self._active_versions.setdefault((chunk.tenant_id, chunk.document_id), chunk.version_id)

    def add(self, chunks: Iterable[Chunk]) -> None:
        additions = validate_chunk_document_versions(chunks)
        validate_chunk_document_versions([*self.chunks, *additions])
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
        return [{"document_id": chunk.document_id, "document_version_id": chunk.version_id,
                 "version_id": chunk.version_id, "chunk_id": chunk.chunk_id,
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
