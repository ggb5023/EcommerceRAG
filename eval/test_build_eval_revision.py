from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import build_eval_revision as revision


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    cases = tmp_path / "cases.jsonl"
    rows = [
        {
            "case_id": f"case-{i}",
            "expected_answer_points": ["old"],
            "source": {"source_version": "synthetic-m2-v1", "type": "synthetic"},
        }
        for i in range(60)
    ]
    cases.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"eval_set_version": "synthetic-m2-v1", "case_count": 60, "sha256": hashlib.sha256(cases.read_bytes()).hexdigest()}), encoding="utf-8")
    revision_map = tmp_path / "revision.json"
    revision_map.write_text(json.dumps({
        "source_eval_set_version": "synthetic-m2-v1",
        "target_eval_set_version": "synthetic-m2-v1-revision-1",
        "source_cases_sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
        "changes": [{"case_id": "case-4", "fields": {"expected_answer_points": ["narrow claim"]}}],
    }), encoding="utf-8")
    return cases, metadata, revision_map


def test_revision_changes_only_explicit_case_and_preserves_source(tmp_path: Path) -> None:
    cases, metadata, revision_map = _inputs(tmp_path)
    before = cases.read_bytes()
    output_cases, output_metadata = tmp_path / "out.jsonl", tmp_path / "out.metadata.json"
    report = revision.write_revision(cases, metadata, revision_map, output_cases, output_metadata)
    assert report["case_count"] == 60
    assert cases.read_bytes() == before
    rows = [json.loads(line) for line in output_cases.read_text().splitlines()]
    assert rows[4]["expected_answer_points"] == ["narrow claim"]
    assert rows[3]["expected_answer_points"] == ["old"]
    assert all(row["source"]["source_version"] == "synthetic-m2-v1-revision-1" for row in rows)
    assert json.loads(output_metadata.read_text())["sha256"] == hashlib.sha256(output_cases.read_bytes()).hexdigest()


@pytest.mark.parametrize("bad_change", [
    {"case_id": "unknown", "fields": {"expected_answer_points": ["x"]}},
    {"case_id": "case-4", "fields": {"query": "x"}},
    {"case_id": "case-4", "fields": {"expected_answer_points": []}},
])
def test_revision_rejects_unknown_case_or_field_or_empty_points(tmp_path: Path, bad_change: dict) -> None:
    cases, metadata, revision_map = _inputs(tmp_path)
    value = json.loads(revision_map.read_text())
    value["changes"] = [bad_change]
    revision_map.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        revision.build_revision(cases, metadata, revision_map)


def test_revision_rejects_source_hash_drift_and_source_overwrite(tmp_path: Path) -> None:
    cases, metadata, revision_map = _inputs(tmp_path)
    value = json.loads(revision_map.read_text())
    value["source_cases_sha256"] = "0" * 64
    revision_map.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="source_cases_sha256"):
        revision.build_revision(cases, metadata, revision_map)
    value["source_cases_sha256"] = hashlib.sha256(cases.read_bytes()).hexdigest()
    revision_map.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        revision.write_revision(cases, metadata, revision_map, cases, tmp_path / "metadata-out.json")
