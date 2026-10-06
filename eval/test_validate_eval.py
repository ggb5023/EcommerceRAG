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


def _review_case() -> dict:
    return {
        "case_id": "case-1",
        "query": "商品是什么材质？",
        "expected_doc_ids": ["doc-1"],
        "expected_answer_points": ["材质为棉"],
        "intent": "knowledge",
        "information_source": "own_knowledge",
        "authorization": {"tenant_id": "t", "shop_id": "s", "role": "operator"},
    }


def test_review_duplicate_key_is_rejected(tmp_path: Path) -> None:
    review = tmp_path / "review.json"
    review.write_text(
        '[{"case_id":"case-1","review_status":"approved",'
        '"review_status":"pending","review_notes":""}]',
        encoding="utf-8",
    )

    errors, _, _ = validate_eval.validate_review(review, [_review_case()])

    assert any("duplicate JSON key" in error for error in errors)


def test_review_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    review = tmp_path / "review.json"
    review.write_bytes(b"[\xff]")

    errors, _, _ = validate_eval.validate_review(review, [_review_case()])

    assert any("not valid UTF-8" in error for error in errors)


def test_review_root_and_row_shapes_are_rejected(tmp_path: Path) -> None:
    review = tmp_path / "review.json"
    review.write_text("{}", encoding="utf-8")
    errors, _, _ = validate_eval.validate_review(review, [_review_case()])
    assert errors == ["review file must contain a JSON array"]

    review.write_text("[null]", encoding="utf-8")
    errors, _, _ = validate_eval.validate_review(review, [_review_case()])
    assert "review row 1 is not an object" in errors


def test_review_status_must_be_string_and_allowed(tmp_path: Path) -> None:
    review = tmp_path / "review.json"
    review.write_text(
        '[{"case_id":"case-1","review_status":[],"review_notes":""}]',
        encoding="utf-8",
    )

    errors, counts, _ = validate_eval.validate_review(review, [_review_case()])

    assert "review row 1 review_status must be a string" in errors
    assert "review row 1 has invalid review_status" in errors
    assert counts["<invalid>"] == 1
