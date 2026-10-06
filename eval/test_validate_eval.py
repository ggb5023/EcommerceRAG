from __future__ import annotations

import hashlib
import json
from pathlib import Path

import validate_eval


def _write_metadata(path: Path, cases_path: Path, case_count: int = 1) -> None:
    path.write_text(
        json.dumps({
            "case_count": case_count,
            "sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "eval_set_version": "synthetic-m2-v1",
            "source_type": "synthetic",
        }),
        encoding="utf-8",
    )


def test_duplicate_case_key_is_reported_as_input_failure(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text('{"case_id":"first","case_id":"second"}\n', encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    _write_metadata(metadata, cases)

    parsed, _, errors = validate_eval.validate(cases, metadata)

    assert parsed == []
    assert any("case 1 invalid JSON" in error and "duplicate JSON key" in error for error in errors)


def test_non_object_case_is_reported_without_traceback(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text("[]\n", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    _write_metadata(metadata, cases)

    parsed, _, errors = validate_eval.validate(cases, metadata)

    assert parsed == []
    assert "case 1 must be a JSON object" in errors
