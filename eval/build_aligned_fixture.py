#!/usr/bin/env python3
"""Build an evaluation corpus from real local source fixtures.

The corpus is deliberately independent from ``synthetic_cases.jsonl``. Case
expectations are never copied into document content; an explicit reviewable
alignment file is required before any retrieval metric is calculated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path


def _json_chunk(chunk) -> dict[str, object]:
    return {
        "document_id": chunk.document_id,
        "document_version_id": chunk.version_id,
        "chunk_id": chunk.chunk_id,
        "content": chunk.content,
        "source_ref": chunk.source_ref,
        "tenant_id": chunk.tenant_id,
        "shop_id": chunk.shop_id,
        "disclosure_class": chunk.disclosure_class,
        "effective_from": chunk.effective_from,
        "effective_to": chunk.effective_to,
        "chunk_hash": chunk.chunk_hash,
        "source_position": chunk.source_position,
        "rule_version": chunk.rule_version,
    }


def build_documents(chunks: Iterable[object]) -> dict[str, dict[str, object]]:
    """Serialize chunks while keeping document policy metadata explicit.

    A mapping reviewer must be able to inspect disclosure and effective dates
    without inferring them from the first chunk.  Rejecting drift here keeps a
    document from carrying contradictory policy metadata.
    """
    documents: dict[str, dict[str, object]] = {}
    for chunk in chunks:
        chunk_metadata = getattr(chunk, "metadata", {})
        source_sha256 = chunk_metadata.get("sha256") if isinstance(chunk_metadata, dict) else None
        document = documents.setdefault(
            chunk.document_id,
            {
                "document_id": chunk.document_id,
                "document_version_id": chunk.version_id,
                "tenant_id": chunk.tenant_id,
                "shop_id": chunk.shop_id,
                "disclosure_class": chunk.disclosure_class,
                "effective_from": chunk.effective_from,
                "effective_to": chunk.effective_to,
                "source_type": "synthetic-local-source",
                "source_version": "ecommerce-demo-v1",
                "source_sha256": source_sha256,
                "chunks": [],
            },
        )
        if (
            document["disclosure_class"] != chunk.disclosure_class
            or document["effective_from"] != chunk.effective_from
            or document["effective_to"] != chunk.effective_to
            or document["source_sha256"] != source_sha256
        ):
            raise ValueError(f"inconsistent policy metadata for {chunk.document_id}")
        document["chunks"].append(_json_chunk(chunk))
    return documents


def main() -> int:
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent
    repo = root.parent
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo / "data/synthetic/ecommerce-demo-v1/manifest.yaml",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned"),
    )
    args = parser.parse_args()

    sys.path.insert(0, str(repo / "python"))
    from app.ingest.pipeline import load_manifest_index

    index = load_manifest_index(args.manifest)
    documents = build_documents(index.chunks)

    args.output.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        json.dumps(documents[key], ensure_ascii=False, sort_keys=True)
        for key in sorted(documents)
    ) + "\n"
    (args.output / "documents.jsonl").write_text(payload, encoding="utf-8")
    manifest = {
        "fixture_version": "synthetic-m2-v1-aligned-v2",
        "source_manifest": str(args.manifest),
        "source_manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "document_ids": sorted(documents),
        "document_count": len(documents),
        "chunk_count": len(index.chunks),
        "alignment_file": "alignment.json",
        "alignment_status": "PENDING_MAPPING",
        "derivation_note": "Parsed from the local synthetic source manifest; case expectations are not copied into content.",
        "real_service_acceptance": False,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    alignment = {
        "alignment_version": "synthetic-m2-v1-to-ecommerce-demo-v1-v1",
        "status": "PENDING_REVIEW",
        "source_corpus_manifest": "manifest.json",
        "case_to_source_documents": {},
        "review_notes": "Each mapping must be approved against source content, tenant/shop scope and business date before metrics run.",
        "real_service_acceptance": False,
    }
    (args.output / "alignment.json").write_text(
        json.dumps(alignment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "documents": len(documents),
                "chunks": len(index.chunks),
                "alignment_status": "PENDING_MAPPING",
                "real_service_acceptance": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
