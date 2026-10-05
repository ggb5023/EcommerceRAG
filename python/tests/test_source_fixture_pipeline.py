from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from scripts.build_source_fixtures import build
from scripts.run_source_parsers import run
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


def test_source_parser_fails_closed_on_unsafe_manifest_path(tmp_path: Path):
    root = tmp_path / "source-fixtures"
    manifest = build(root)
    manifest["fixtures"][0]["path"] = "../outside.md"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = run(root)

    row = report["reports"][0]
    assert row["parse_status"] == "FAILED"
    assert row["error_code"] == "unsafe_fixture_path"
