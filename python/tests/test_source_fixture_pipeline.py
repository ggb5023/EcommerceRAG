from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from scripts.build_source_fixtures import build
from scripts.run_source_parsers import main as run_parser_main, run
from scripts.run_chunk_report import main as run_chunk_report_main
from scripts.validate_source_fixtures import validate


def test_generated_html_fixture_joins_parser_and_chunk_reports(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    manifest = build(root)
    assert len(manifest["fixtures"]) == 18
    assert validate(root)["status"] == "PASS"
    report = run(root)
    assert report["summary"] == {
        "fixtures": 18,
        "passed": 11,
        "expected_failures": 7,
        "mismatches": 0,
    }
    html = next(item for item in report["reports"] if item["fixture_id"] == "crawler-html-v1")
    assert html["parse_status"] == "PASS"
    assert html["element_count"] >= 6
    assert html["source_position_coverage"] == html["element_count"]
    manifest_html = next(item for item in manifest["fixtures"] if item["fixture_id"] == "crawler-html-v1")
    assert html["source_path"] == manifest_html["path"]
    assert html["source_size_bytes"] == manifest_html["size_bytes"]
    assert html["source_sha256"] == manifest_html["sha256"]
    assert {chunk["content_type"] for chunk in html["chunks"]} >= {"heading", "table", "image", "code"}
    assert all(chunk["source_sha256"] == html["source_sha256"] for chunk in html["chunks"])
    assert all(chunk["source_path"] == html["source_path"] for chunk in html["chunks"])


def test_fixture_validator_rejects_unsafe_and_duplicate_manifest_paths(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    manifest = build(root)
    manifest["fixtures"][0]["path"] = "../outside.md"
    manifest["fixtures"][1]["path"] = manifest["fixtures"][2]["path"]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = validate(root)

    assert report["status"] == "FAIL"
    assert "unsafe_path:md-faq-short-v1" in report["errors"]
    assert any(error.startswith("duplicate_path:") for error in report["errors"])


def test_fixture_validator_rejects_intermediate_directory_symlink(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    manifest = build(root)
    (root / "alias").symlink_to(root / "markdown", target_is_directory=True)
    manifest["fixtures"][0]["path"] = "alias/md-faq-short-v1.md"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = validate(root)

    assert report["status"] == "FAIL"
    assert "unsafe_path:md-faq-short-v1" in report["errors"]


def test_fixture_validator_rejects_invalid_manifest_without_raising(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    root.mkdir()
    (root / "manifest.json").write_text("{not-json", encoding="utf-8")

    report = validate(root)

    assert report["status"] == "FAIL"
    assert report["errors"] == ["manifest_invalid:JSONDecodeError"]


def test_fixture_validator_rejects_untyped_ids_and_tags_without_raising(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    manifest = build(root)
    manifest["fixtures"][0]["fixture_id"] = {"not": "a string"}
    manifest["fixtures"][1]["scenario_tags"] = "not-a-list"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = validate(root)

    assert report["status"] == "FAIL"
    assert "invalid_fixture_id:0" in report["errors"]
    assert "invalid_scenario_tags:md-policy-long-v1" in report["errors"]


def test_source_parser_fails_closed_on_unsafe_manifest_path(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    manifest = build(root)
    manifest["fixtures"][0]["path"] = "../outside.md"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = run(root)

    row = report["reports"][0]
    assert row["parse_status"] == "FAILED"
    assert row["error_code"] == "unsafe_fixture_path"


def test_source_parser_reports_invalid_manifest_shape(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    root.mkdir()
    (root / "manifest.json").write_text("{not-json", encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="manifest_invalid:JSONDecodeError"):
        run(root)


def test_source_parser_accepts_only_manifest_fixture_arrays(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"fixtures": {}}), encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="fixtures_must_be_array"):
        run(root)


def test_source_parser_cli_writes_metadata_only_manifest_failure(tmp_path: Path, monkeypatch):
    root = tmp_path / "source-fixtures"
    root.mkdir()
    (root / "manifest.json").write_text("[]", encoding="utf-8")
    output = tmp_path / "parse-report.json"
    monkeypatch.setattr("sys.argv", ["run_source_parsers.py", "--root", str(root), "--output", str(output)])

    assert run_parser_main() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert report["error_code"] == "manifest_not_object"
    assert report["real_service_acceptance"] is False
    assert report["reports"] == []


def _write_parse_report(root: Path) -> dict:
    report = run(root)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "parse-report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    return report


def test_chunk_report_binds_every_chunk_to_current_source_bytes(tmp_path: Path, monkeypatch):
    root = tmp_path / "source-fixtures"
    build(root)
    _write_parse_report(root)
    monkeypatch.setattr("sys.argv", ["run_chunk_report.py", "--root", str(root)])

    assert run_chunk_report_main() == 0
    report = json.loads((root / "reports" / "chunk-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["chunk_count"] == 45
    assert report["real_service_acceptance"] is False


def test_chunk_report_rejects_tampered_fixture_source_hash(tmp_path: Path, monkeypatch):
    root = tmp_path / "source-fixtures"
    build(root)
    report = _write_parse_report(root)
    passed = next(item for item in report["reports"] if item["parse_status"] == "PASS")
    fixture_id = passed["fixture_id"]
    passed["source_sha256"] = "0" * 64
    (root / "reports" / "parse-report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_chunk_report.py", "--root", str(root)])

    assert run_chunk_report_main() == 1
    output = json.loads((root / "reports" / "chunk-report.json").read_text(encoding="utf-8"))
    assert output["status"] == "FAIL"
    assert f"source_sha256_mismatch:{fixture_id}" in output["errors"]
    assert output["real_service_acceptance"] is False


def test_chunk_report_rejects_chunk_source_binding_drift(tmp_path: Path, monkeypatch):
    root = tmp_path / "source-fixtures"
    build(root)
    report = _write_parse_report(root)
    passed = next(item for item in report["reports"] if item["parse_status"] == "PASS")
    fixture_id = passed["fixture_id"]
    passed["chunks"][0]["source_path"] = "../outside.md"
    (root / "reports" / "parse-report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_chunk_report.py", "--root", str(root)])

    assert run_chunk_report_main() == 1
    output = json.loads((root / "reports" / "chunk-report.json").read_text(encoding="utf-8"))
    assert output["status"] == "FAIL"
    assert f"chunk_source_mismatch:{fixture_id}:0" in output["errors"]


def test_chunk_report_rejects_source_symlink_alias(tmp_path: Path, monkeypatch):
    root = tmp_path / "source-fixtures"
    build(root)
    report = _write_parse_report(root)
    passed = next(item for item in report["reports"] if item["parse_status"] == "PASS")
    fixture_id = passed["fixture_id"]
    original = root / passed["source_path"]
    alias = root / "alias-source.md"
    alias.symlink_to(original)
    passed["source_path"] = alias.relative_to(root).as_posix()
    (root / "reports" / "parse-report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_chunk_report.py", "--root", str(root)])

    assert run_chunk_report_main() == 1
    output = json.loads((root / "reports" / "chunk-report.json").read_text(encoding="utf-8"))
    assert output["status"] == "FAIL"
    assert f"source_path_unsafe:{fixture_id}" in output["errors"]


def test_chunk_report_rejects_tampered_summary_counts(tmp_path: Path, monkeypatch):
    root = tmp_path / "source-fixtures"
    build(root)
    report = _write_parse_report(root)
    report["summary"]["passed"] += 1
    (root / "reports" / "parse-report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_chunk_report.py", "--root", str(root)])

    assert run_chunk_report_main() == 1
    output = json.loads((root / "reports" / "chunk-report.json").read_text(encoding="utf-8"))
    assert output["status"] == "FAIL"
    assert "parse_report_summary_mismatch:passed" in output["errors"]


def test_chunk_report_rejects_real_service_acceptance_flag(tmp_path: Path, monkeypatch):
    root = tmp_path / "source-fixtures"
    build(root)
    report = _write_parse_report(root)
    report["real_service_acceptance"] = True
    (root / "reports" / "parse-report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_chunk_report.py", "--root", str(root)])

    assert run_chunk_report_main() == 1
    output = json.loads((root / "reports" / "chunk-report.json").read_text(encoding="utf-8"))
    assert output["status"] == "FAIL"
    assert "real_service_acceptance_must_be_false" in output["errors"]
