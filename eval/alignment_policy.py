"""Shared disclosure and effective-date checks for the aligned M2 corpus."""
from __future__ import annotations

from datetime import date
from typing import Any

ALLOWED_DISCLOSURE_CLASSES = {"external_allowed", "internal_only", "unclassified"}
PRIVILEGED_ROLES = {"admin", "owner"}


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid date value: {value!r}")
    return date.fromisoformat(value)


def disclosure_class(value: Any) -> str | None:
    return value if value in ALLOWED_DISCLOSURE_CLASSES else None


def document_policy(document: dict[str, Any]) -> dict[str, Any]:
    """Return validated metadata without changing the input document."""
    errors: list[str] = []
    chunks = document.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return {"valid": False, "errors": ["chunks_missing_or_empty"], "classes": [], "chunks": []}
    normalized: list[dict[str, Any]] = []
    classes: set[str] = set()
    for index, chunk in enumerate(chunks, 1):
        if not isinstance(chunk, dict):
            errors.append(f"chunk_not_object:{index}")
            continue
        cls = disclosure_class(chunk.get("disclosure_class"))
        if cls is None:
            errors.append(f"disclosure_class_invalid:{index}")
        else:
            classes.add(cls)
        try:
            start = parse_date(chunk.get("effective_from"))
            end = parse_date(chunk.get("effective_to"))
        except ValueError:
            errors.append(f"effective_date_invalid:{index}")
            start = end = None
        if start and end and start >= end:
            errors.append(f"effective_date_range_invalid:{index}")
        if chunk.get("tenant_id") != document.get("tenant_id") or chunk.get("shop_id") != document.get("shop_id"):
            errors.append(f"chunk_scope_mismatch:{index}")
        normalized.append({"chunk": chunk, "class": cls, "start": start, "end": end})
    return {"valid": not errors, "errors": errors, "classes": sorted(classes), "chunks": normalized}


def assess_document(document: dict[str, Any], business_date: Any, authorization: dict[str, Any]) -> dict[str, Any]:
    policy = document_policy(document)
    if not policy["valid"]:
        return {"status": "INVALID_POLICY", "risk_reasons": ["invalid_source_policy"], "eligible_chunks": [], "policy_errors": policy["errors"]}
    try:
        current = parse_date(business_date)
    except ValueError:
        current = None
    if current is None:
        return {"status": "INVALID_BUSINESS_DATE", "risk_reasons": ["invalid_business_date"], "eligible_chunks": [], "policy_errors": []}
    if document.get("tenant_id") != authorization.get("tenant_id") or document.get("shop_id") != authorization.get("shop_id"):
        return {"status": "SCOPE_MISMATCH", "risk_reasons": ["scope_mismatch"], "eligible_chunks": [], "policy_errors": []}
    active: list[dict[str, Any]] = []
    for entry in policy["chunks"]:
        if entry["start"] and current < entry["start"]:
            continue
        if entry["end"] and current >= entry["end"]:
            continue
        active.append(entry)
    if not active:
        has_future = any(entry["start"] and current < entry["start"] for entry in policy["chunks"])
        status = "NOT_YET_EFFECTIVE" if has_future else "EXPIRED_OR_REVOKED"
        return {"status": status, "risk_reasons": [status.lower()], "eligible_chunks": [], "policy_errors": []}
    eligible: list[dict[str, Any]] = []
    reasons: set[str] = set()
    for entry in active:
        cls = entry["class"]
        if cls == "external_allowed" or (cls == "internal_only" and authorization.get("role") in PRIVILEGED_ROLES):
            eligible.append(entry["chunk"])
        elif cls == "internal_only":
            reasons.add("internal_only")
        elif cls == "unclassified":
            reasons.add("unclassified")
    if eligible:
        return {"status": "ELIGIBLE", "risk_reasons": sorted(reasons), "eligible_chunks": eligible, "policy_errors": []}
    if "unclassified" in reasons:
        status = "UNCLASSIFIED_BLOCKED"
    else:
        status = "ACCESS_DENIED"
    return {"status": status, "risk_reasons": sorted(reasons), "eligible_chunks": [], "policy_errors": []}
