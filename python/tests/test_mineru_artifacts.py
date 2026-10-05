import hashlib
import json
import os
import sys
from pathlib import Path

# The tests are also invoked directly without installing the repository package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# isort: off
from scripts.process_mineru_result import process
from scripts import run_mineru_report as mineru_report
from scripts.run_mineru_report import completed_results, smoke_results
from scripts.validate_mineru_artifacts import validate
# isort: on

GOLDEN_CONTENT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "mineru" / "golden" / "content_list.json"


def _build_integrity_fixture(tmp_path: Path, *, include_integrity: bool = True) -> Path:
    root = tmp_path / "real-docs"
    raw = root / "raw" / "fixture.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"%PDF-1.7\nsynthetic golden input\n")
    input_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps({"sources": [{
        "document_id": "golden-doc", "file_name": raw.name,
        "sha256": input_hash,
    }]}, ensure_ascii=False), encoding="utf-8")

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "content_list.json").write_bytes(GOLDEN_CONTENT.read_bytes())
    task_id = "golden-task"
    result_hash = hashlib.sha256(b"golden-result-archive").hexdigest()
    process_kwargs = {
        "root": artifact, "document_id": "golden-doc", "version_id": "v1",
        "output_root": root, "tenant_id": "lab", "shop_id": "shop",
        "task_id": task_id,
    }
    if include_integrity:
        process_kwargs.update({"input_pdf_sha256": input_hash, "result_sha256": result_hash})
    process(**process_kwargs)
    task_dir = root / "task-artifacts" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "meta.json").write_text(json.dumps({
        "task_id": task_id, "status": "PASS", "result_sha256": result_hash,
        "result_size_bytes": len(b"golden-result-archive"), "artifact_root": str(task_dir),
    }), encoding="utf-8")
    return root


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


def test_process_rejects_empty_elements_without_writing_empty_artifacts(tmp_path: Path):
    import pytest

    artifact = tmp_path / "empty"
    artifact.mkdir()
    (artifact / "content_list.json").write_text("[]", encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="parsed_elements_empty"):
        process(artifact, document_id="empty-doc", version_id="v1",
                output_root=output, tenant_id="lab", shop_id="shop")
    assert not output.exists()


def test_process_rejects_element_sets_that_produce_no_chunks(tmp_path: Path):
    import pytest

    artifact = tmp_path / "image-only"
    artifact.mkdir()
    (artifact / "content_list.json").write_text(json.dumps([{
        "type": "image", "image_refs": ["images/only.png"], "page_no": 1,
    }]), encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="parsed_chunks_empty"):
        process(artifact, document_id="image-doc", version_id="v1",
                output_root=output, tenant_id="lab", shop_id="shop")
    assert not output.exists()


def test_process_cleans_staging_when_artifact_write_fails(tmp_path: Path, monkeypatch):
    import pytest

    artifact = tmp_path / "write-failure"
    artifact.mkdir()
    (artifact / "content_list.json").write_text(json.dumps([{
        "type": "text", "text": "content", "page_no": 1,
    }]), encoding="utf-8")
    output = tmp_path / "output"
    original = __import__("scripts.process_mineru_result", fromlist=["_write_atomic"])._write_atomic

    def fail_once(path, data):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("scripts.process_mineru_result._write_atomic", fail_once)
    with pytest.raises(OSError, match="synthetic write failure"):
        process(artifact, document_id="write-doc", version_id="v1",
                output_root=output, tenant_id="lab", shop_id="shop")
    assert not list(output.glob(".mineru-stage-*"))
    assert not (output / "parsed").exists()
    assert not (output / "chunks").exists()
    monkeypatch.setattr("scripts.process_mineru_result._write_atomic", original)


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


def test_report_indexes_latest_smoke_failure_by_document_and_file(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "mineru-smoke.json").write_text(json.dumps({
        "documents": [{"file_name": "doc.pdf", "status": "FAILED", "error_code": "timeout"}],
    }), encoding="utf-8")
    (reports / "mineru-smoke-latest.json").write_text(json.dumps({
        "documents": [{"document_id": "doc", "file_name": "doc.pdf", "status": "FAILED",
                        "error_code": "parse_failed", "task_id": "task-2"}],
    }), encoding="utf-8")
    os.utime(reports / "mineru-smoke.json", (1, 1))
    os.utime(reports / "mineru-smoke-latest.json", (2, 2))
    by_document, by_file = smoke_results(tmp_path)
    assert by_document["doc"]["task_id"] == "task-2"
    assert by_file["doc.pdf"]["error_code"] == "parse_failed"


def test_run_report_surfaces_failed_smoke_attempt(tmp_path: Path, monkeypatch):
    raw = tmp_path / "raw" / "doc.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"%PDF-1.7\n")
    input_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps({"sources": [{
        "document_id": "doc", "file_name": "doc.pdf", "sha256": input_hash,
    }]}), encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "mineru-smoke-doc.json").write_text(json.dumps({"documents": [{
        "document_id": "doc", "file_name": "doc.pdf", "input_sha256": input_hash,
        "status": "FAILED", "task_id": "task-failed", "error_code": "parse_failed",
    }]}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_mineru_report.py", "--root", str(tmp_path)])
    assert mineru_report.main() == 0
    report = json.loads((reports / "mineru-run.json").read_text(encoding="utf-8"))
    row = report["documents"][0]
    assert row["status"] == "FAILED"
    assert row["reason"] == "mineru_smoke_failed:parse_failed"
    assert row["smoke_task_id"] == "task-failed"


def test_golden_mineru_artifacts_have_consistent_integrity_bindings(tmp_path: Path):
    report = validate(_build_integrity_fixture(tmp_path))
    assert report["summary"] == {"documents": 1, "PASS": 1, "LEGACY_UNVERIFIED": 0, "FAIL": 0}
    row = report["documents"][0]
    assert row["input_pdf_sha256"]
    assert row["chunk_count"] == 3
    assert row["errors"] == []


def test_mineru_integrity_validator_rejects_tampered_result_and_chunk(tmp_path: Path):
    root = _build_integrity_fixture(tmp_path)
    parsed = next((root / "parsed").glob("*/*/meta.json"))
    metadata = json.loads(parsed.read_text(encoding="utf-8"))
    metadata["result_sha256"] = "0" * 64
    parsed.write_text(json.dumps(metadata), encoding="utf-8")
    chunk_path = root / metadata["chunk_path"]
    chunk_path.write_text(chunk_path.read_text(encoding="utf-8") + "{\"document_id\":\"wrong\"}\n", encoding="utf-8")

    report = validate(root)
    row = report["documents"][0]
    assert row["status"] == "FAIL"
    assert "result_sha256_mismatch" in row["errors"]
    assert "chunk_document_id_mismatch:4" in row["errors"]
    assert "chunk_count_mismatch" in row["errors"]


def test_legacy_mineru_metadata_is_not_reported_as_full_pass(tmp_path: Path):
    report = validate(_build_integrity_fixture(tmp_path, include_integrity=False))
    row = report["documents"][0]
    assert row["status"] == "LEGACY_UNVERIFIED"
    assert "input_pdf_sha256_missing" in row["warnings"]
    assert "result_sha256_missing" in row["warnings"]
