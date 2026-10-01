"""HTTP smoke for the opt-in synthetic local retrieval path."""
from __future__ import annotations

import json
import os
import time
import urllib.request


BASE = os.environ.get("M1_HTTP_BASE", "http://127.0.0.1:18080").rstrip("/")


def request(path: str, *, method: str = "GET", data: dict | None = None):
    body = json.dumps(data).encode() if data is not None else None
    request_headers = {"Content-Type": "application/json"} if body else {}
    if method == "POST" and "/turns" in path:
        request_headers["Idempotency-Key"] = "synthetic-http-smoke-1"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=request_headers)
    with urllib.request.urlopen(req, timeout=12) as response:
        return response.status, response.read()


status, _ = request("/healthz")
assert status == 200
status, body = request("/v1/conversations", method="POST", data={})
assert status == 201, body
conversation_id = json.loads(body)["id"]
status, body = request(f"/v1/conversations/{conversation_id}/turns", method="POST",
                       data={"text": "配送和退货规则是什么？"})
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
assert result["evidence"]
assert result["evidence"][0]["documentId"] == "syn-policy-a"
assert result["evidence"][0]["sourceRef"].startswith("local://")
print("PASS synthetic HTTP/SSE smoke: local parser index, evidence, citation and mock copy gate")
