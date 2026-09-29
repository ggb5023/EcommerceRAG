"""HTTP/SSE smoke test for a running isolated M1 stack."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid


BASE = os.environ.get("M1_HTTP_BASE", "http://127.0.0.1:8080").rstrip("/")
if not BASE.startswith(("http://127.0.0.1", "http://localhost")):
    raise SystemExit("M1_HTTP_BASE must point to a local isolated gateway")


def request(path: str, *, method: str = "GET", data: dict | None = None,
            headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


status, body = request("/v1/session")
assert status == 200, body
session = json.loads(body)
assert session["is_mock"] is True
assert session["tenantId"] == "m1-tenant" and session["shopId"] == "shop-demo"

status, _ = request("/v1/conversations", method="POST", data={"shop_id": "other-shop"})
assert status == 403, f"unauthorized shop accepted: {status}"

status, body = request("/v1/conversations", method="POST", data={})
assert status == 201, body
conversation_id = json.loads(body)["id"]
idempotency_key = "m1-http-smoke-" + uuid.uuid4().hex
headers = {"Idempotency-Key": idempotency_key, "Content-Type": "application/json"}
status, body = request(f"/v1/conversations/{conversation_id}/turns", method="POST",
                       data={"text": "配送和退货规则是什么？"}, headers=headers)
assert status == 202, body
turn = json.loads(body)
request_id = turn["request_id"]

status, replay = request(f"/v1/conversations/{conversation_id}/turns", method="POST",
                         data={"text": "配送和退货规则是什么？"}, headers=headers)
assert status == 200 and json.loads(replay)["request_id"] == request_id
status, _ = request(f"/v1/conversations/{conversation_id}/turns", method="POST",
                    data={"text": "不同的问题"}, headers=headers)
assert status == 409, f"idempotency payload conflict was not rejected: {status}"

for _ in range(100):
    status, body = request(f"/v1/turns/{request_id}")
    assert status == 200, body
    result = json.loads(body)
    if result["status"] in {"DONE", "FAILED", "CANCELLED"}:
        break
    time.sleep(0.1)
assert result["status"] == "DONE", result
assert result["is_mock"] is True
assert result["customer_reply"]["can_copy"] is False
assert "标准配送" in result["answer"]
assert result["evidence"] and result["evidence"][0]["versionId"].startswith("m1-")
assert result["evidence"][0]["sourceRef"] == "fixture://m1/shipping"

status, body = request(f"/v1/turns/{request_id}/events", headers={"Last-Event-ID": "1"})
assert status == 200, body
event_ids = [int(line[4:]) for line in body.splitlines() if line.startswith(b"id: ")]
assert event_ids and event_ids[0] > 1, event_ids
assert event_ids == sorted(set(event_ids)), event_ids

print("PASS M1 HTTP/SSE smoke: mock session, shop rejection, idempotency, Evidence, mock copy gate, replay")
