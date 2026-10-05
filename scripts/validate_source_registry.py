#!/usr/bin/env python3
"""Validate the versioned crawler source registry without network access."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data/web/source-registry-v1.json"
STATUS = {"pending_review", "active", "paused", "blocked"}
KINDS = {"manufacturer", "platform_help", "regulator", "external_search", "synthetic_test"}
DISCOVERY = {"fixed_urls", "sitemap", "rss", "bounded_links"}
TERMS = {"pending", "approved", "rejected", "not_applicable"}
ROBOTS = {"pending", "allowed", "denied", "unavailable"}
POLICIES = {"pending_review", "raw_allowed", "summary_only", "no_persistence", "test_only"}
RESPONSIBILITY_STATUS = {"pending", "approved", "rejected"}
RESPONSIBILITY_EVIDENCE = {
    "terms_license",
    "robots_policy",
    "persistence_retention",
    "refresh_budget",
    "owner_approval",
}
SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _path_allowed(path: str, allowed: list[str]) -> bool:
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed)


def validate(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "registry": str(path), "source_count": 0,
                "errors": [f"registry_invalid:{type(exc).__name__}"], "real_service_acceptance": False}
    if not isinstance(payload, dict):
        errors.append("registry_not_object")
        sources: list[Any] = []
    else:
        if payload.get("registry_version") != "source-registry-v1":
            errors.append("registry_version_invalid")
        if payload.get("real_service_acceptance") is not False:
            errors.append("real_service_acceptance_must_be_false")
        contract = payload.get("responsibility_contract")
        if not isinstance(contract, dict):
            errors.append("responsibility_contract_missing")
        else:
            if contract.get("version") != "source-responsibility-v1":
                errors.append("responsibility_contract_version_invalid")
            if contract.get("gate_status") != "pending_review":
                errors.append("responsibility_contract_gate_must_be_pending_review")
            roles = contract.get("required_roles")
            if not isinstance(roles, list) or not roles or any(
                not isinstance(role, str) or not role.strip() for role in roles
            ):
                errors.append("responsibility_contract_roles_invalid")
        sources = payload.get("sources", [])
        if not isinstance(sources, list):
            errors.append("sources_must_be_array")
            sources = []
    seen: set[str] = set()
    for index, item in enumerate(sources):
        prefix = f"sources[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}_not_object")
            continue
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            errors.append(f"{prefix}_source_id_invalid")
        elif source_id in seen:
            errors.append(f"duplicate_source_id:{source_id}")
        else:
            seen.add(source_id)
        if item.get("status") not in STATUS:
            errors.append(f"{prefix}_status_invalid")
        if item.get("source_kind") not in KINDS:
            errors.append(f"{prefix}_source_kind_invalid")
        hosts = item.get("base_domains")
        if not isinstance(hosts, list) or any(not isinstance(host, str) or not host or "*" in host for host in hosts):
            errors.append(f"{prefix}_base_domains_invalid")
        for host in hosts if isinstance(hosts, list) else []:
            try:
                ipaddress.ip_address(host)
                errors.append(f"{prefix}_literal_ip_not_allowed")
            except ValueError:
                if host != host.lower().rstrip("."):
                    errors.append(f"{prefix}_domain_not_normalized")
        allowed = item.get("allowed_paths")
        denied = item.get("denied_paths")
        if (not isinstance(allowed, list) or not allowed
                or any(not isinstance(value, str) or not value.startswith("/") for value in allowed)):
            errors.append(f"{prefix}_allowed_paths_invalid")
            allowed = []
        if (not isinstance(denied, list)
                or any(not isinstance(value, str) or not value.startswith("/") for value in denied)):
            errors.append(f"{prefix}_denied_paths_invalid")
        if item.get("discovery_mode") not in DISCOVERY:
            errors.append(f"{prefix}_discovery_mode_invalid")
        fixed_urls = item.get("fixed_urls")
        if not isinstance(fixed_urls, list) or any(not isinstance(url, str) for url in fixed_urls):
            errors.append(f"{prefix}_fixed_urls_invalid")
            fixed_urls = []
        for url in fixed_urls:
            try:
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError
                if parsed.hostname.lower().rstrip(".") not in hosts:
                    errors.append(f"{prefix}_fixed_url_host_not_allowed")
                if not _path_allowed(parsed.path or "/", allowed):
                    errors.append(f"{prefix}_fixed_url_path_not_allowed")
            except ValueError:
                errors.append(f"{prefix}_fixed_url_invalid")
        terms = item.get("terms_review")
        robots = item.get("robots_policy")
        refresh = item.get("refresh_policy")
        if not isinstance(terms, dict) or terms.get("status") not in TERMS:
            errors.append(f"{prefix}_terms_review_invalid")
            terms = {}
        if not isinstance(robots, dict) or robots.get("status") not in ROBOTS:
            errors.append(f"{prefix}_robots_policy_invalid")
            robots = {}
        if not isinstance(refresh, dict):
            errors.append(f"{prefix}_refresh_policy_invalid")
            refresh = {}
        if item.get("content_policy") not in POLICIES:
            errors.append(f"{prefix}_content_policy_invalid")
        responsibility = item.get("responsibility")
        if not isinstance(responsibility, dict):
            errors.append(f"{prefix}_responsibility_missing")
            responsibility = {}
        if not isinstance(responsibility.get("owner"), str) or not responsibility.get("owner", "").strip():
            errors.append(f"{prefix}_responsibility_owner_invalid")
        if not isinstance(responsibility.get("reviewer_role"), str) or not responsibility.get("reviewer_role", "").strip():
            errors.append(f"{prefix}_responsibility_reviewer_role_invalid")
        if responsibility.get("status") not in RESPONSIBILITY_STATUS:
            errors.append(f"{prefix}_responsibility_status_invalid")
        evidence = responsibility.get("required_evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(value not in RESPONSIBILITY_EVIDENCE for value in evidence)
            or len(set(evidence)) != len(evidence)
        ):
            errors.append(f"{prefix}_responsibility_evidence_invalid")
        if item.get("status") == "active":
            if terms.get("status") != "approved" or robots.get("status") != "allowed":
                errors.append(f"{prefix}_active_without_review")
            if not fixed_urls and not hosts:
                errors.append(f"{prefix}_active_without_scope")
            if not isinstance(refresh.get("max_age_days"), int) or refresh["max_age_days"] <= 0:
                errors.append(f"{prefix}_active_without_refresh_age")
            if item.get("content_policy") in {"pending_review", "no_persistence"}:
                errors.append(f"{prefix}_active_content_policy_not_publishable")
            if responsibility.get("status") != "approved":
                errors.append(f"{prefix}_active_without_responsibility_approval")
    return {"status": "PASS" if not errors else "FAIL", "registry": str(path),
            "source_count": len(sources), "active_count": sum(item.get("status") == "active" for item in sources if isinstance(item, dict)),
            "errors": errors, "real_service_acceptance": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    report = validate(args.path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
