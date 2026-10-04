#!/usr/bin/env python3
"""Record honest MinerU/PDF readiness status; never fabricates parse output."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

PDF_ROOT = Path("/var/lib/ecommerce-rag/real-docs/real-docs-v1")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def completed_results(root: Path) -> dict[str, dict]:
    """Index converted artifacts by document id without reading document text."""
    task_records: dict[str, dict] = {}
    for meta_path in sorted((root / "task-artifacts").glob("*/meta.json")):
        record = read_json(meta_path)
        task_id = record.get("task_id")
        if isinstance(task_id, str) and record.get("status") == "PASS":
            task_records[task_id] = record
    results: dict[str, dict] = {}
    for meta_path in sorted((root / "parsed").glob("*/*/meta.json")):
        parsed = read_json(meta_path)
        document_id = parsed.get("document_id")
        if not isinstance(document_id, str):
            continue
        # A parsed artifact must carry its provider task id explicitly.  Never
        # infer it from the number of completed tasks: that would bind a
        # result to the wrong document as soon as two tasks exist.
        task_id = parsed.get("task_id")
        if not isinstance(task_id, str):
            continue
        task = task_records.get(task_id, {})
        if not task:
            continue
        results[document_id] = {**task, **parsed, "task_id": task_id}
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PDF_ROOT)
    args = parser.parse_args()
    mineru = shutil.which("mineru")
    pdfinfo = shutil.which("pdfinfo")
    manifest = read_json(args.root / "manifest.json")
    completed = completed_results(args.root)
    rows = []
    for path in sorted((args.root / "raw").glob("*.pdf")):
        header = path.open("rb").read(5)
        status = "NOT_RUN"
        reason = "mineru_unavailable"
        if header != b"%PDF-":
            status, reason = "FAILED", "invalid_pdf_header"
        elif mineru:
            status, reason = "NOT_RUN", "adapter_not_executed"
        source = next((item for item in manifest.get("sources", [])
                       if isinstance(item, dict) and item.get("file_name") == path.name), {})
        document_id = source.get("document_id")
        parsed = completed.get(document_id, {}) if isinstance(document_id, str) else {}
        if parsed.get("status") == "PASS" and parsed.get("chunk_count") is not None:
            status, reason = "PASS", None
        rows.append({
            "file_name": path.name, "size_bytes": path.stat().st_size,
            "sha256": sha256(path), "status": status, "reason": reason,
            "mineru_command": mineru, "pdfinfo_command": pdfinfo,
            "document_id": document_id, "task_id": parsed.get("task_id"),
            "result_sha256": parsed.get("result_sha256"),
            "result_size_bytes": parsed.get("result_size_bytes"),
            "page_count": len(parsed.get("page_numbers", [])) if isinstance(parsed.get("page_numbers"), list) else None,
            "page_numbers": parsed.get("page_numbers"),
            "table_count": parsed.get("table_count"), "image_count": parsed.get("image_count"),
            "element_count": parsed.get("element_count"), "chunk_count": parsed.get("chunk_count"),
            "warning_count": parsed.get("warning_count"),
            "chunk_rule_version": parsed.get("chunk_rule_version"),
            "artifact_root": parsed.get("artifact_root"),
        })
    report = {
        "report_version": "mineru-run-v1", "input_root": str(args.root / "raw"),
        "environment": {"mineru": mineru, "pdfinfo": pdfinfo},
        "real_service_acceptance": False, "documents": rows,
    }
    out = args.root / "reports" / "mineru-run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, out)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    counts = {state: sum(row["status"] == state for row in rows) for state in ("PASS", "FAILED", "NOT_RUN")}
    print(json.dumps({"output": str(out), "documents": len(rows), "counts": counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
