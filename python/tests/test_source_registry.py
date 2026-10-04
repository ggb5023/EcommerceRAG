# ruff: noqa: I001
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.validate_source_registry import validate


ROOT = Path(__file__).resolve().parents[2]


def test_repository_registry_is_pending_and_valid():
    report = validate(ROOT / "data/web/source-registry-v1.json")
    assert report["status"] == "PASS", report
    assert report["source_count"] == 2
    assert report["active_count"] == 0
    assert report["real_service_acceptance"] is False


def test_active_source_requires_terms_robots_and_freshness(tmp_path: Path):
    payload = {
        "registry_version": "source-registry-v1",
        "real_service_acceptance": False,
        "sources": [{
            "source_id": "bad-active", "status": "active", "source_kind": "manufacturer",
            "base_domains": ["example.com"], "allowed_paths": ["/"], "denied_paths": [],
            "discovery_mode": "fixed_urls", "fixed_urls": ["https://example.com/guide"],
            "terms_review": {"status": "pending"}, "robots_policy": {"status": "pending"},
            "refresh_policy": {}, "content_policy": "pending_review",
        }],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate(path)
    assert report["status"] == "FAIL"
    assert "sources[0]_active_without_review" in report["errors"]


def test_registry_rejects_host_and_path_drift(tmp_path: Path):
    payload = {
        "registry_version": "source-registry-v1", "real_service_acceptance": False,
        "sources": [{
            "source_id": "test-source", "status": "pending_review", "source_kind": "manufacturer",
            "base_domains": ["Example.com"], "allowed_paths": ["/docs"], "denied_paths": [],
            "discovery_mode": "fixed_urls", "fixed_urls": ["https://other.example/guide"],
            "terms_review": {"status": "pending"}, "robots_policy": {"status": "pending"},
            "refresh_policy": {}, "content_policy": "pending_review",
        }],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate(path)
    assert report["status"] == "FAIL"
    assert "sources[0]_domain_not_normalized" in report["errors"]
    assert "sources[0]_fixed_url_host_not_allowed" in report["errors"]
