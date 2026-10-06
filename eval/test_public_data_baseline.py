from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).parent
sys.path.insert(0, str(EVAL_DIR))

from run_public_data_baseline import InputIntegrityError, load_records

SCRIPT = EVAL_DIR / "run_public_data_baseline.py"


def test_public_manifest_duplicate_key_fails_input_integrity_without_report(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest_version":"a","manifest_version":"b"}', encoding="utf-8")
    output = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL input_integrity:")
    assert "duplicate JSON key" in result.stdout
    assert not output.exists()


def test_public_jsonl_duplicate_key_and_invalid_utf8_fail_closed(tmp_path: Path) -> None:
    duplicate = tmp_path / "records.jsonl"
    duplicate.write_text('{"product_id":"a","product_id":"b"}\n', encoding="utf-8")
    with pytest.raises(InputIntegrityError, match="duplicate JSON key"):
        load_records(duplicate)

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_bytes(b"{\xff\n")
    with pytest.raises(InputIntegrityError, match="not valid UTF-8"):
        load_records(invalid)


def test_public_csv_duplicate_header_and_extra_fields_fail_closed(tmp_path: Path) -> None:
    duplicate_header = tmp_path / "duplicate.csv"
    duplicate_header.write_text("product_id,product_id\na,b\n", encoding="utf-8")
    with pytest.raises(InputIntegrityError, match="duplicate fields"):
        load_records(duplicate_header)

    extra_fields = tmp_path / "extra.csv"
    extra_fields.write_text("product_id,relevance\na,1,unexpected\n", encoding="utf-8")
    with pytest.raises(InputIntegrityError, match="extra fields"):
        load_records(extra_fields)
