import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.process_mineru_result import process
from scripts.run_mineru_report import completed_results


def test_process_mineru_result_is_repeatable_and_keeps_raw_input_hash(tmp_path: Path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    content = [
        {"type": "heading", "text": "规格", "text_level": 1, "page_no": 3},
        {"type": "table", "table_body": [["型号", "容量"]], "text": "Pico | 2MB", "page_no": 3},
        {"type": "image", "image_refs": ["images/1.png"], "page_no": 4},
        {"type": "unknown", "text": "带警告的内容", "page_no": 4},
    ]
    raw = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    (artifact / "content_list.json").write_bytes(raw)
    output = tmp_path / "output"

    first = process(artifact, document_id="real-doc", version_id="v1",
                    output_root=output, tenant_id="lab", shop_id="shop", max_chars=20,
                    task_id="task-123")
    parsed = output / "parsed" / "real-doc" / "v1"
    chunk_file = output / "chunks" / "real-doc-v1.jsonl"
    first_bytes = {path.name: path.read_bytes() for path in (parsed / "layout.json", parsed / "full.md", parsed / "meta.json", chunk_file)}

    second = process(artifact, document_id="real-doc", version_id="v1",
                     output_root=output, tenant_id="lab", shop_id="shop", max_chars=20,
                     task_id="task-123")
    second_bytes = {path.name: path.read_bytes() for path in (parsed / "layout.json", parsed / "full.md", parsed / "meta.json", chunk_file)}

    assert first == second
    assert first_bytes == second_bytes
    assert first["input_content_list_sha256"] == hashlib.sha256(raw).hexdigest()
    assert first["page_numbers"] == [3, 4]
    assert first["table_count"] == 1
    assert first["image_count"] == 1
    assert first["warning_count"] == 1
    assert json.loads((parsed / "meta.json").read_text())["real_service_acceptance"] is False
    assert json.loads((parsed / "meta.json").read_text())["task_id"] == "task-123"


def test_table_without_text_is_linearized_and_chunked(tmp_path: Path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "content_list.json").write_text(json.dumps([
        {"type": "table", "table_body": [["型号", "容量"], ["Pico", "2MB"]], "page_no": 2},
    ], ensure_ascii=False), encoding="utf-8")

    report = process(artifact, document_id="table-doc", version_id="v1",
                     output_root=tmp_path / "output", tenant_id="lab", shop_id="shop")
    assert report["chunk_count"] == 1
    chunk = json.loads((tmp_path / "output" / "chunks" / "table-doc-v1.jsonl").read_text().splitlines()[0])
    assert "型号 | 容量" in chunk["content"]
    assert chunk["source_position"]["page_no"] == 2


def test_process_accepts_mineru_uuid_prefixed_content_list(tmp_path: Path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "442fe179-31bd-4d99-bf1a-1a25abe6402b_content_list.json").write_text(
        json.dumps([{"type": "text", "text": "Pico datasheet", "page_no": 1}], ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact / "442fe179-31bd-4d99-bf1a-1a25abe6402b_content_list_v2.json").write_text("[]", encoding="utf-8")
    report = process(artifact, document_id="prefixed-doc", version_id="v1",
                     output_root=tmp_path / "output", tenant_id="lab", shop_id="shop")
    assert report["element_count"] == 1
    assert report["chunk_count"] == 1


def test_process_rejects_missing_invalid_and_duplicate_content_lists(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError, match="content_list_missing"):
        process(tmp_path / "missing", document_id="doc", version_id="v1",
                output_root=tmp_path / "out", tenant_id="lab", shop_id="shop")

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "content_list.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="content_list_invalid_json"):
        process(invalid, document_id="doc", version_id="v1",
                output_root=tmp_path / "out", tenant_id="lab", shop_id="shop")

    duplicate = tmp_path / "duplicate"
    (duplicate / "nested").mkdir(parents=True)
    for path in (duplicate / "content_list.json", duplicate / "nested" / "content_list.json"):
        path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="content_list_ambiguous"):
        process(duplicate, document_id="doc", version_id="v1",
                output_root=tmp_path / "out", tenant_id="lab", shop_id="shop")


def test_report_does_not_guess_task_id_when_multiple_tasks_exist(tmp_path: Path):
    task_root = tmp_path / "task-artifacts"
    for task_id in ("task-a", "task-b"):
        path = task_root / task_id
        path.mkdir(parents=True)
        (path / "meta.json").write_text(json.dumps({
            "task_id": task_id,
            "status": "PASS",
            "result_sha256": task_id,
        }), encoding="utf-8")

    parsed = tmp_path / "parsed" / "doc" / "v1"
    parsed.mkdir(parents=True)
    (parsed / "meta.json").write_text(json.dumps({
        "document_id": "doc",
        "status": "PASS",
        "chunk_count": 1,
    }), encoding="utf-8")

    assert completed_results(tmp_path) == {}
