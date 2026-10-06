#!/usr/bin/env python3
"""Validate the public-data source manifest without network access.

The validator deliberately accepts a pending, not-downloaded source.  That is
the expected state until a revision, source terms, and restricted storage have
been recorded.  It never fills a revision or SHA-256 and never reads the
synthetic business evaluation set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

REQUIRED_MANIFEST_FIELDS = {
    "manifest_version",
    "recorded_at",
    "real_service_acceptance",
    "download_policy",
    "storage_policy",
    "sources",
}
REQUIRED_SOURCE_FIELDS = {
    "source_id",
    "provider",
    "dataset",
    "canonical_url",
    "dataset_card_url",
    "original_source_terms_url",
    "original_source_terms_status",
    "status",
    "revision",
    "license_status",
    "download_status",
    "download_date",
    "expected_filename",
    "actual_filename",
    "sha256",
    "restricted_storage_location",
    "allowed_use",
    "forbidden_use",
    "real_service_acceptance",
}
DOWNLOAD_STATUSES = {"not_downloaded", "downloaded"}
TERMS_STATUSES = {
    "pending_direct_verification",
    "pending_dataset_card_and_original_terms",
    "pending_dataset_card_and_original_source_verification",
    "verified",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_SOURCE_IDS = {"amazon-esci", "hf-mcauley-amazon-reviews-2023"}
EXPECTED_ALLOWED_USE = {
    "amazon-esci": {"product retrieval", "product ranking", "recall@k", "mrr", "ndcg"},
    "hf-mcauley-amazon-reviews-2023": {
        "product metadata mapping",
        "review/product text parsing",
        "deduplication",
        "field coverage",
        "text retrieval experiments",
    },
}
REQUIRED_FORBIDDEN_USE = {
    "merchant policy truth",
    "current price, inventory, or order truth",
    "tenant/shop permissions",
    "customer reply correctness",
}


class InputIntegrityError(ValueError):
    """Raised when a public-source JSON input cannot be parsed safely."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputIntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(text: str, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except InputIntegrityError as error:
        raise InputIntegrityError(f"{label}: {error}") from error
    except json.JSONDecodeError as error:
        raise InputIntegrityError(f"{label} invalid JSON: {error}") from error


def load_json_value(path: Path, label: str) -> Any:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputIntegrityError(f"{label} is not valid UTF-8: {error}") from error
    except OSError as error:
        raise InputIntegrityError(f"{label} cannot be read: {error}") from error
    return strict_json_loads(text, label)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_manifest(payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["manifest must be a JSON object"]
    missing = REQUIRED_MANIFEST_FIELDS - payload.keys()
    errors.extend(f"manifest missing {field}" for field in sorted(missing))
    if payload.get("real_service_acceptance") is not False:
        errors.append("manifest real_service_acceptance must be false")
    if not _date(payload.get("recorded_at")):
        errors.append("manifest recorded_at must be an ISO date")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        return errors + ["manifest sources must be a non-empty list"]

    source_ids: set[str] = set()
    for index, source in enumerate(sources, 1):
        prefix = f"source {index}"
        if not isinstance(source, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = REQUIRED_SOURCE_FIELDS - source.keys()
        errors.extend(f"{prefix} missing {field}" for field in sorted(missing))
        source_id = source.get("source_id")
        if not _non_empty_string(source_id):
            errors.append(f"{prefix} source_id must be non-empty")
        elif source_id in source_ids:
            errors.append(f"duplicate source_id: {source_id}")
        else:
            source_ids.add(source_id)
        for field in ("canonical_url", "dataset_card_url"):
            value = source.get(field)
            if not _non_empty_string(value) or not value.startswith("https://"):
                errors.append(f"{prefix} {field} must be an https URL")
        terms_url = source.get("original_source_terms_url")
        if terms_url is not None and (not _non_empty_string(terms_url) or not terms_url.startswith("https://")):
            errors.append(f"{prefix} original_source_terms_url must be null or an https URL")
        if source.get("original_source_terms_status") not in TERMS_STATUSES:
            errors.append(f"{prefix} invalid original_source_terms_status")
        if source.get("real_service_acceptance") is not False:
            errors.append(f"{prefix} real_service_acceptance must be false")
        revision = source.get("revision")
        if revision is not None and not _non_empty_string(revision):
            errors.append(f"{prefix} revision must be null or a non-empty string")
        if source.get("version_or_revision") != revision and "version_or_revision" in source:
            errors.append(f"{prefix} version_or_revision disagrees with revision")
        status = source.get("download_status")
        if status not in DOWNLOAD_STATUSES:
            errors.append(f"{prefix} invalid download_status")
        if source.get("license_status") is None or not _non_empty_string(source.get("license_status")):
            errors.append(f"{prefix} license_status must be non-empty")
        allowed = source.get("allowed_use")
        forbidden = source.get("forbidden_use")
        if not isinstance(allowed, list) or not all(_non_empty_string(item) for item in allowed):
            errors.append(f"{prefix} allowed_use must be a list of strings")
        if not isinstance(forbidden, list) or not all(_non_empty_string(item) for item in forbidden):
            errors.append(f"{prefix} forbidden_use must be a list of strings")
        if isinstance(allowed, list) and isinstance(forbidden, list) and set(allowed) & set(forbidden):
            errors.append(f"{prefix} allowed_use overlaps forbidden_use")
        if source_id in EXPECTED_ALLOWED_USE and set(allowed or []) != EXPECTED_ALLOWED_USE[source_id]:
            errors.append(f"{prefix} allowed_use does not match the fixed source scope")
        if not REQUIRED_FORBIDDEN_USE <= set(forbidden or []):
            errors.append(f"{prefix} forbidden_use is missing a required business boundary")
        sha = source.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not SHA256.fullmatch(sha)):
            errors.append(f"{prefix} sha256 must be null or 64 lowercase hex characters")
        if status == "not_downloaded":
            if sha is not None:
                errors.append(f"{prefix} not_downloaded source must have null sha256")
            for field in ("download_date", "actual_filename"):
                if source.get(field) is not None:
                    errors.append(f"{prefix} not_downloaded source must have null {field}")
        else:
            if not _date(source.get("download_date")):
                errors.append(f"{prefix} downloaded source needs an ISO download_date")
            if not _non_empty_string(source.get("actual_filename")):
                errors.append(f"{prefix} downloaded source needs actual_filename")
            if not isinstance(sha, str) or not SHA256.fullmatch(sha):
                errors.append(f"{prefix} downloaded source needs a SHA-256")
            if revision is None:
                errors.append(f"{prefix} downloaded source needs revision")
            if not str(source.get("license_status", "")).startswith("verified"):
                errors.append(f"{prefix} downloaded source needs verified license_status")
            if source.get("original_source_terms_status") != "verified":
                errors.append(f"{prefix} downloaded source needs verified source terms")
            if not _non_empty_string(source.get("original_source_terms_url")):
                errors.append(f"{prefix} downloaded source needs original_source_terms_url")
        storage = source.get("restricted_storage_location")
        if not _non_empty_string(storage) or "outside_git" not in storage:
            errors.append(f"{prefix} storage must explicitly be outside_git restricted storage")
    if source_ids != REQUIRED_SOURCE_IDS:
        errors.append(f"source_id set must be {sorted(REQUIRED_SOURCE_IDS)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(__file__).resolve().with_name("public-data-sources.manifest.json")
    parser.add_argument("--manifest", type=Path, default=default)
    args = parser.parse_args()
    try:
        payload = load_json_value(args.manifest, "manifest")
    except InputIntegrityError as exc:
        print(f"FAIL input_integrity: {exc}")
        return 1
    errors = validate_manifest(payload)
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print(f"PASS public source manifest: {len(payload['sources'])} sources; sha256={file_sha256(args.manifest)}")
    for source in payload["sources"]:
        state = "READY" if source["download_status"] == "downloaded" else "NOT_RUN"
        print(f"{source['source_id']}: {state}; revision={source['revision'] or 'unverified'}; license={source['license_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
