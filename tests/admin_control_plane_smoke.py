"""Exercise the synthetic admin API through isolated local Go/Python services."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def local_base(name: str, default: str) -> str:
    value = os.environ.get(name, default).rstrip("/")
    host = urllib.parse.urlsplit(value).hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"{name} must point to a loopback test service")
    return value


ADMIN = local_base("ADMIN_HTTP_BASE", "http://127.0.0.1:8082")
MEMBER = local_base("ADMIN_MEMBER_HTTP_BASE", "http://127.0.0.1:8083")
checks: list[str] = []


def request(base: str, path: str, data=None, headers=None):
    body = None
    if isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    elif data is not None:
        body = json.dumps(data).encode()
    merged = {"Accept": "application/json", **(headers or {})}
    if data is not None and not isinstance(data, (bytes, bytearray)):
        merged["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        return error.code, payload


def expect(status: int, expected: int, operation: str, payload=None):
    if status != expected:
        code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
        raise AssertionError(f"{operation}: HTTP {status}, expected {expected}, code={code}")


def get(base: str, path: str, expected: int = 200):
    status, payload = request(base, path)
    expect(status, expected, f"GET {path}", payload)
    return payload


def post(base: str, path: str, body, expected: int = 200):
    status, payload = request(base, path, body)
    expect(status, expected, f"POST {path}", payload)
    return payload


def multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes]]):
    boundary = "----ECRAdmin" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields:
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(), b"\r\n",
        ])
    for name, filename, content in files:
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n", content, b"\r\n",
        ])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_package(source_id: str, document_id: str, content: bytes):
    digest = hashlib.sha256(content).hexdigest()
    manifest = (
        "schema_version: 1\n"
        "pipeline_version: admin-smoke-v1\n"
        f"source:\n  id: {source_id}\n  name: Synthetic admin smoke source\n"
        "documents:\n"
        f"  - document_id: {document_id}\n"
        f"    title: Synthetic admin smoke guide {document_id}\n"
        "    path: docs/guide.md\n"
        "    format: markdown\n"
        "    disclosure_class: external_allowed\n"
        f"    sha256: {digest}\n"
    )
    raw, content_type = multipart(
        [("shop_id", "demo-shop-east"), ("manifest", manifest), ("paths", "docs/guide.md")],
        [("files", "guide.md", content)],
    )
    status, result = request(ADMIN, "/admin/v1/merchant/sources/import", raw,
                             {"Content-Type": content_type, "Accept": "application/json"})
    expect(status, 202, "synthetic package import", result)
    return result, raw, content_type, manifest


def wait_for_reviewable_job(job_id: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    last_state = None
    while time.monotonic() < deadline:
        job = get(ADMIN, f"/admin/v1/merchant/ingestion/{job_id}?shop_id=demo-shop-east")
        items = job.get("items", [])
        last_state = (job.get("status"), tuple(item.get("status") for item in items))
        if job.get("status") in {"failed", "cancelled"}:
            raise AssertionError(
                f"ingestion job {job_id} ended before review: status={job.get('status')} "
                f"error_code={job.get('error_code')}"
            )
        if (job.get("status") == "awaiting_review" and items
                and all(item.get("status") == "awaiting_review"
                        and item.get("version_status") == "ready" for item in items)):
            return job
        time.sleep(0.2)
    raise AssertionError(f"ingestion job {job_id} did not reach review state: {last_state}")


def review_and_publish(version: dict, permission_revision: str, stale_check: bool = False):
    version_id = version["version_id"]
    shop_query = "?shop_id=demo-shop-east"
    detail = get(ADMIN, f"/admin/v1/merchant/reviews/{version_id}{shop_query}")
    if not any("Synthetic admin smoke guide" in chunk["content"] for chunk in detail["content"]):
        raise AssertionError("review detail did not return the imported synthetic content")
    review = {
        "confirm": True, "decision": "approved", "disclosure_class": "external_allowed",
        "external_allowed": True, "expected_hash": version["source_hash"],
        "expected_fencing_epoch": version["fencing_epoch"],
        "permission_revision": permission_revision,
        "reason": "Approve the isolated synthetic admin smoke fixture",
    }
    post(ADMIN, f"/admin/v1/merchant/versions/{version_id}/review", review)
    current_session = get(ADMIN, "/admin/v1/session")
    operation = {
        "confirm": True, "tenant_id": current_session["tenant_id"],
        "shop_id": version["shop_id"], "document_id": version["document_id"],
        "version_hash": version["source_hash"],
        "permission_revision": current_session["permission_revision"],
        "expected_fencing_epoch": version["fencing_epoch"],
        "reason": "Publish the reviewed isolated synthetic fixture",
    }
    if stale_check:
        stale = {**operation, "expected_fencing_epoch": version["fencing_epoch"] + 5}
        status, _ = request(ADMIN, f"/admin/v1/merchant/versions/{version_id}/publish", stale)
        expect(status, 409, "stale version fencing confirmation")
    result = post(ADMIN, f"/admin/v1/merchant/versions/{version_id}/publish", operation)
    return result


def ask(query: str):
    created = post(MEMBER, "/v1/conversations", {}, expected=201)
    conversation_id = created["id"]
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    status, started = request(MEMBER, f"/v1/conversations/{conversation_id}/turns",
                              {"text": query}, headers)
    expect(status, 202, "start synthetic retrieval turn", started)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = get(MEMBER, f"/v1/turns/{started['request_id']}")
        if result["status"] in {"DONE", "ASKING", "FAILED", "CANCELLED"}:
            return result
        time.sleep(0.1)
    raise AssertionError("synthetic retrieval turn did not reach a terminal state")


def main():
    admin_session = get(ADMIN, "/admin/v1/session")
    if not admin_session["is_mock"] or "merchant_admin" not in admin_session["admin_roles"]:
        raise AssertionError("management API is not running under the synthetic admin identity")
    member_session = get(MEMBER, "/v1/session")
    if member_session["userId"] != "demo-agent-east":
        raise AssertionError("member API is not running under the expected synthetic member")

    overview = get(ADMIN, "/admin/v1/overview")
    if overview["components"]["worker"]["status"] != "healthy":
        raise AssertionError(
            "persistent ingestion worker is not healthy: "
            f"{overview['components']['worker']['status']}"
        )
    for path in (
        "/admin/v1/access/accounts", "/admin/v1/access/requests",
        "/admin/v1/access/audit", "/admin/v1/merchant/shops", "/admin/v1/merchant/sources",
        "/admin/v1/merchant/ingestion", "/admin/v1/merchant/reviews", "/admin/v1/merchant/versions",
    ):
        get(ADMIN, path)
    checks.append("admin_read_workspaces")

    for path in (
        "/admin/v1/overview", "/admin/v1/access/accounts", "/admin/v1/access/audit",
        "/admin/v1/merchant/shops", "/admin/v1/merchant/sources",
        "/admin/v1/merchant/ingestion", "/admin/v1/merchant/reviews",
        "/admin/v1/merchant/versions",
    ):
        status, denied = request(MEMBER, path)
        expect(status, 403, f"member cannot read management endpoint {path}", denied)
    status, denied = request(MEMBER, "/admin/v1/session")
    expect(status, 403, "member cannot read management session", denied)
    get(MEMBER, "/admin/v1/access/requests")
    checks.append("server_side_scope_denial_management_session_and_own_requests")

    rejected_role_status, rejected_role = request(MEMBER, "/admin/v1/access/requests", {
        "role": "knowledge_reviewer", "scope_kind": "shops", "shop_ids": ["demo-shop-east"],
        "reason": "Attempt a role that is not self-requestable",
    })
    if rejected_role_status != 400 or rejected_role.get("code") != "role_not_requestable":
        raise AssertionError("disabled administrative role was accepted as a self-request")
    checks.append("role_catalog_request_boundary")

    request_result = post(MEMBER, "/admin/v1/access/requests", {
        "role": "merchant_admin", "scope_kind": "shops", "shop_ids": ["demo-shop-east"],
        "reason": "Request isolated synthetic shop management access",
    }, expected=201)
    queued = get(ADMIN, "/admin/v1/access/requests?status=pending")
    if not any(item["id"] == request_result["id"] for item in queued["items"]):
        raise AssertionError("scoped manager did not see the new request")
    decision = post(ADMIN, f"/admin/v1/access/requests/{request_result['id']}/decision", {
        "confirm": True, "decision": "approved",
        "reason": "Approve the isolated synthetic permission request",
    })
    accounts = get(ADMIN, "/admin/v1/access/accounts?shop_id=demo-shop-east")
    agent = next(row for row in accounts["items"] if row["external_id"] == "demo-agent-east")
    grant = next(row for row in agent["grants"] if row["role"] == "merchant_admin" and row["status"] == "active")
    post(ADMIN, f"/admin/v1/access/grants/{grant['id']}/revoke", {
        "confirm": True, "reason": "Revoke the temporary isolated synthetic grant",
    })
    if decision.get("audit_result") != "success":
        raise AssertionError("permission decision did not return its audit result")
    checks.append("permission_request_approve_revoke_audit")

    source_id = "admin-smoke-" + uuid.uuid4().hex
    document_id = "guide-" + uuid.uuid4().hex
    first_content = (f"# Synthetic admin smoke guide {document_id}\n\n"
                     f"Synthetic admin smoke guide marker {document_id} describes the blue sample product.\n").encode()
    first, raw, content_type, manifest = upload_package(source_id, document_id, first_content)
    duplicate_status, duplicate = request(ADMIN, "/admin/v1/merchant/sources/import", raw,
                                           {"Content-Type": content_type, "Accept": "application/json"})
    expect(duplicate_status, 202, "idempotent repeated import", duplicate)
    if first["job_id"] != duplicate["job_id"]:
        raise AssertionError("repeated package import created a second job")
    first_job = wait_for_reviewable_job(first["job_id"])
    versions = get(ADMIN, "/admin/v1/merchant/reviews?shop_id=demo-shop-east")["items"]
    first_version = next(row for row in versions if row["document_id"] == document_id)
    if first_version["status"] != "ready" or first_job["items"][0]["version_id"] != first_version["version_id"]:
        raise AssertionError("review queue version does not match the completed ingestion item")
    review_and_publish(first_version, get(ADMIN, "/admin/v1/session")["permission_revision"], stale_check=True)
    retrieval_query = f"What does synthetic admin smoke guide marker {document_id} describe?"
    first_turn = ask(retrieval_query)
    first_document_ids = sorted({item["documentId"] for item in first_turn["evidence"]})
    if first_turn["status"] != "DONE" or document_id not in first_document_ids:
        raise AssertionError(
            "published version was not returned as authorized evidence: "
            f"status={first_turn['status']} expected_document_found={document_id in first_document_ids} "
            f"evidence_document_count={len(first_document_ids)}"
        )
    if not first_turn["is_mock"] or first_turn["customer_reply"]["can_copy"]:
        raise AssertionError("synthetic response was not marked mock and non-copyable")
    first_active_id = next(item["versionId"] for item in first_turn["evidence"] if item["documentId"] == document_id)
    checks.append("import_review_publish_idempotency_and_retrieval")

    second_content = (f"# Synthetic admin smoke guide {document_id}\n\n"
                      f"Synthetic admin smoke guide marker {document_id} describes the green sample product update.\n").encode()
    second, _, _, _ = upload_package(source_id, document_id, second_content)
    second_job = wait_for_reviewable_job(second["job_id"])
    versions = get(ADMIN, "/admin/v1/merchant/reviews?shop_id=demo-shop-east")["items"]
    second_version = next(row for row in versions if row["document_id"] == document_id and row["source_hash"] != first_version["source_hash"])
    if second_version["status"] != "ready" or second_job["items"][0]["version_id"] != second_version["version_id"]:
        raise AssertionError("second review queue version does not match the completed ingestion item")
    review_and_publish(second_version, get(ADMIN, "/admin/v1/session")["permission_revision"])
    second_turn = ask(retrieval_query)
    second_active_id = next(item["versionId"] for item in second_turn["evidence"] if item["documentId"] == document_id)
    if second_turn["status"] != "DONE" or second_active_id == first_active_id:
        raise AssertionError("publishing the newer document version did not replace active search evidence")
    checks.append("version_supersede_and_active_index_refresh")

    versions = get(ADMIN, "/admin/v1/merchant/versions?shop_id=demo-shop-east")["items"]
    old = next(item for item in versions if item["version_id"] == first_version["version_id"])
    current = get(ADMIN, "/admin/v1/session")
    rollback = {
        "confirm": True, "tenant_id": current["tenant_id"], "shop_id": "demo-shop-east",
        "document_id": document_id, "version_hash": old["source_hash"],
        "permission_revision": current["permission_revision"],
        "expected_fencing_epoch": old["fencing_epoch"],
        "reason": "Rollback to the prior isolated synthetic document version",
    }
    rollback_result = post(ADMIN, f"/admin/v1/merchant/versions/{old['version_id']}/rollback", rollback)
    rolled_back_turn = ask(retrieval_query)
    rolled_back_id = next(item["versionId"] for item in rolled_back_turn["evidence"] if item["documentId"] == document_id)
    if rollback_result.get("audit_result") != "success" or rolled_back_id != old["version_id"]:
        raise AssertionError("rollback did not restore the earlier indexed version")

    versions = get(ADMIN, "/admin/v1/merchant/versions?shop_id=demo-shop-east")["items"]
    current_version = next(item for item in versions if item["version_id"] == second_version["version_id"])
    current = get(ADMIN, "/admin/v1/session")
    revoke = {
        "confirm": True, "tenant_id": current["tenant_id"], "shop_id": "demo-shop-east",
        "document_id": document_id, "version_hash": current_version["source_hash"],
        "permission_revision": current["permission_revision"],
        "expected_fencing_epoch": current_version["fencing_epoch"],
        "reason": "Revoke the superseded isolated synthetic version",
    }
    revoke_result = post(ADMIN, f"/admin/v1/merchant/versions/{current_version['version_id']}/revoke", revoke)
    final_turn = ask(retrieval_query)
    final_active_id = next(item["versionId"] for item in final_turn["evidence"] if item["documentId"] == document_id)
    if revoke_result.get("status") != "revoked" or final_active_id != old["version_id"]:
        raise AssertionError("revocation did not keep the approved active version available")
    audits = get(ADMIN, "/admin/v1/access/audit?shop_id=demo-shop-east")["items"]
    if not any(row["action"] == "version.rollback" for row in audits) or not any(row["action"] == "version.revoke" for row in audits):
        raise AssertionError("version actions were not recorded in the audit stream")
    checks.append("rollback_revoke_fencing_and_audit")

    print(json.dumps({"status": "PASS", "checks": checks, "real_service_acceptance": False,
                      "profile": "synthetic_import_mock", "document_id": document_id,
                      "job_ids": [first["job_id"], second["job_id"]]}, sort_keys=True))


if __name__ == "__main__":
    main()
