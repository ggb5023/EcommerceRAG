from __future__ import annotations

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
