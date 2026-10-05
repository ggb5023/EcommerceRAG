from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ruff: noqa: I001
from scripts.audit_crawler_sources import audit



def test_registry_audit_is_local_and_keeps_public_crawl_disabled():
    report = audit(ROOT / "data/web/source-registry-v1.json")
    assert report["status"] == "PASS", report
    assert report["official_source_count"] == 3
    assert report["active_count"] == 0
    assert report["network_requests"] == 0
    assert report["public_crawl"] == "NOT_RUN"
    assert report["release_gate"] == "PENDING_REVIEW"
    assert report["real_service_acceptance"] is False


def test_audit_rejects_approved_or_unbounded_source(tmp_path: Path):
    payload = json.loads((ROOT / "data/web/source-registry-v1.json").read_text(encoding="utf-8"))
    source = payload["sources"][0]
    source["status"] = "active"
    source.pop("crawl_limits")
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = audit(path)
    assert report["status"] == "FAIL"
    assert "raspberrypi-official-docs:status_must_be_pending_review" in report["errors"]
    assert "raspberrypi-official-docs:crawl_limits_missing" in report["errors"]
