from __future__ import annotations

from alignment_policy import assess_document


def _document(disclosure_class: str, *, effective_from: str = "2026-01-01", effective_to: str | None = None) -> dict:
    return {
        "document_id": "doc-1",
        "tenant_id": "tenant-a",
        "shop_id": "shop-a",
        "chunks": [{
            "chunk_id": "chunk-1",
            "content": "synthetic source",
            "tenant_id": "tenant-a",
            "shop_id": "shop-a",
            "disclosure_class": disclosure_class,
            "effective_from": effective_from,
            "effective_to": effective_to,
        }],
    }


AUTH = {"tenant_id": "tenant-a", "shop_id": "shop-a", "role": "operator"}


def test_operator_internal_only_is_denied() -> None:
    result = assess_document(_document("internal_only"), "2026-10-04", AUTH)
    assert result["status"] == "ACCESS_DENIED"


def test_operator_unclassified_is_blocked() -> None:
    result = assess_document(_document("unclassified"), "2026-10-04", AUTH)
    assert result["status"] == "UNCLASSIFIED_BLOCKED"


def test_expired_and_future_dates_are_rejected() -> None:
    expired = assess_document(_document("external_allowed", effective_to="2026-10-01"), "2026-10-04", AUTH)
    future = assess_document(_document("external_allowed", effective_from="2026-11-01"), "2026-10-04", AUTH)
    assert expired["status"] == "EXPIRED_OR_REVOKED"
    assert future["status"] == "NOT_YET_EFFECTIVE"


def test_external_allowed_in_scope_is_eligible() -> None:
    result = assess_document(_document("external_allowed"), "2026-10-04", AUTH)
    assert result["status"] == "ELIGIBLE"
    assert result["eligible_chunks"]
