"""HTTP smoke for the opt-in synthetic local retrieval path."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


BASE = os.environ.get("M1_HTTP_BASE", "http://127.0.0.1:18080").rstrip("/")


def request(path: str, *, method: str = "GET", data: dict | None = None):
    body = json.dumps(data).encode() if data is not None else None
    request_headers = {"Content-Type": "application/json"} if body else {}
    if method == "POST" and "/turns" in path:
        request_headers["Idempotency-Key"] = "synthetic-http-smoke-1"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


expected_user = os.environ.get("SYNTHETIC_EXPECTED_USER", "demo-agent-east")
expected_tenant = os.environ.get("SYNTHETIC_EXPECTED_TENANT", "demo-tenant-a")
expected_role = os.environ.get("SYNTHETIC_EXPECTED_ROLE", "operator")
expected_shop = os.environ.get("SYNTHETIC_EXPECTED_SHOP", "demo-shop-east")
expected_doc = os.environ.get("SYNTHETIC_EXPECTED_DOC", "syn-policy-a")
query = os.environ.get("SYNTHETIC_QUERY", "配送和退货规则是什么？")
expected_session_status = int(os.environ.get("SYNTHETIC_EXPECTED_SESSION_STATUS", "200"))
expected_write_status = int(os.environ.get("SYNTHETIC_EXPECTED_WRITE_STATUS", "201"))


status, _ = request("/healthz")
assert status == 200
status, body = request("/v1/session")
assert status == expected_session_status, body
if status != 200:
    payload = json.loads(body)
    assert payload.get("code") in {"identity_revoked", "no_shop_authorized", "shop_not_authorized"}, payload
    print(f"PASS synthetic authorization smoke: {expected_user} -> {payload['code']}")
    raise SystemExit(0)
session = json.loads(body)
assert session["userId"] == expected_user, session
assert session["tenantId"] == expected_tenant, session
assert session["role"] == expected_role, session
assert session["shopId"] == expected_shop, session
status, body = request("/v1/conversations", method="POST", data={})
assert status == expected_write_status, body
if status != 201:
    payload = json.loads(body)
    assert payload.get("code") == "role_forbidden", payload
    print(f"PASS synthetic authorization smoke: {expected_user} write denied")
    raise SystemExit(0)
conversation_id = json.loads(body)["id"]
status, body = request(f"/v1/conversations/{conversation_id}/turns", method="POST",
                       data={"text": query})
assert status == 202, body
request_id = json.loads(body)["request_id"]
for _ in range(100):
    status, body = request(f"/v1/turns/{request_id}")
    result = json.loads(body)
    if result["status"] in {"DONE", "FAILED", "CANCELLED"}:
        break
    time.sleep(0.1)
assert result["status"] == "DONE", result
assert result["is_mock"] is True
assert result["customer_reply"]["can_copy"] is False
if expected_doc:
    assert result["evidence"], result
    assert result["evidence"][0]["documentId"] == expected_doc, result
    assert result["evidence"][0]["sourceRef"].startswith("local://"), result
status, stream = request(f"/v1/turns/{request_id}/events")
assert status == 200 and b"event: evidence" in stream and b"event: completed" in stream
event_ids = [int(line[4:]) for line in stream.splitlines() if line.startswith(b"id: ")]
assert event_ids and event_ids == sorted(set(event_ids))
status, body = request(f"/v1/conversations/{conversation_id}")
assert status == 200 and json.loads(body)["last_turn"]["citations"]
print("PASS synthetic HTTP/SSE smoke: local parser index, evidence, citation and mock copy gate")
