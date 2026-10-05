from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from scripts.build_source_fixtures import build
from scripts.run_source_parsers import main as run_parser_main, run
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
    assert {chunk["content_type"] for chunk in html["chunks"]} >= {"heading", "table", "image", "code"}


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
