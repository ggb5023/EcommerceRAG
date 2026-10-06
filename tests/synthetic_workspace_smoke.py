"""Read real isolated synthetic Go/Python results; never print response bodies."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("SYNTHETIC_HTTP_BASE", "http://127.0.0.1:8081").rstrip("/")
assert urllib.parse.urlsplit(BASE).hostname in {"127.0.0.1", "localhost"}


def http(path, data=None, key=None, headers=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
                                    headers={"Content-Type": "application/json", "Idempotency-Key": key or uuid.uuid4().hex, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def get(path):
    status, body = http(path)
    assert status == 200, "GET failed: " + str(status)
    return json.loads(body)


def start(query, conversation=None, parent=None, key=None):
    if not conversation:
        status, body = http("/v1/conversations", {})
        assert status == 201, "conversation creation failed"
        conversation = json.loads(body)["id"]
    status, body = http(f"/v1/conversations/{conversation}/turns", {"text": query, **({"parent_turn_id": parent} if parent else {})}, key)
    assert status in {200, 202}, "turn creation failed: " + str(status)
    turn = json.loads(body)
    assert turn["conversation_id"] == conversation and turn["turn_id"] != turn["request_id"]
    assert turn["execution_no"] == 1
    return conversation, turn


def wait(request_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = get("/v1/turns/" + request_id)
        if result["status"] in {"DONE", "ASKING", "FAILED", "CANCELLED"}:
            return result
        time.sleep(0.05)
    raise AssertionError("execution timeout")


def main():
    session = get("/v1/session")
    assert session["profile"] == "synthetic_import_mock" and session["is_mock"]
    report = {"profile": session["profile"], "real_service_acceptance": False, "checks": []}
    answers = set()
    scenarios = [
        ("specification", "SKU-CUP-480 保温杯容量和材质？", "syn-products-a", "304"),
        ("policy", "配送需要几个工作日？", "syn-policy-a", "2-5"),
        ("faq", "低款和高款收纳箱如何选？", "syn-faq-a", "32cm"),
        ("care", "竹纤维毛巾怎么护理？", "syn-faq-a", "低温"),
        ("synthetic_fact", "SKU-CUP-480 库存快照是多少？", "syn-public-facts-a", "非实时库存"),
        ("unanswerable", "月球玄武岩密度是多少？", None, "暂时没有"),
    ]
    version_bound_evidence = 0
    for name, query, expected, point in scenarios:
        conversation, turn = start(query)
        result = wait(turn["request_id"])
        assert result["status"] == "DONE", name + " execution failed"
        assert result["is_mock"] and not result["customer_reply"]["can_copy"]
        assert result["conversation_id"] == conversation and result["turn_id"] == turn["turn_id"]
        assert point in result["answer"], name + " answer point missing"
        docs = {e["documentId"] for e in result["evidence"]}
        assert all(e.get("versionId") for e in result["evidence"]), name + " evidence version missing"
        version_bound_evidence += sum(bool(e.get("versionId")) for e in result["evidence"])
        assert "syn-facts-a" not in docs and "syn-restricted-a" not in docs
        if expected:
            assert expected in docs, name + " expected document missing"
            assert result["citations"], name + " citation missing"
        else:
            assert not docs and not result["citations"], "unanswerable must not fabricate citations"
        assert all(c["evidence_id"] in {e["id"] for e in result["evidence"]} for c in result["citations"])
        refreshed = get("/v1/conversations/" + conversation)["last_turn"]
        assert refreshed == result, name + " persisted result drift"
        status, raw = http("/v1/turns/" + turn["request_id"] + "/events")
        assert status == 200
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
        assert events and events[-1]["type"] == "completed"
        assert all(e["request_id"] == turn["request_id"] and e["turn_id"] == turn["turn_id"] for e in events)
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        status, replay = http("/v1/turns/" + turn["request_id"] + "/events", headers={"Last-Event-ID": "2"})
        assert status == 200 and all(int(line[4:]) > 2 for line in replay.splitlines() if line.startswith(b"id: "))
        answers.add(result["answer"])
        report["checks"].append({"name": name, "status": "PASS", "evidence_count": len(result["evidence"]),
                                  "version_bound_evidence_count": sum(bool(e.get("versionId")) for e in result["evidence"]),
                                  "citation_count": len(result["citations"])})
    report["checks"].append({
        "name": "document_version_binding",
        "status": "PASS",
        "version_bound_evidence_count": version_bound_evidence,
    })
    assert len(answers) == len(scenarios), "different queries returned identical answers"
    conversation, asking = start("那个怎么样？")
    result = wait(asking["request_id"])
    assert result["status"] == "ASKING" and result["clarification"] and not result["evidence"]
    assert get("/v1/conversations/" + conversation)["last_turn"] == result
    _, child = start("SKU-CUP-480 保温杯容量？", conversation, result["turn_id"])
    child_result = wait(child["request_id"])
    assert child_result["status"] == "DONE" and child_result["parent_turn_id"] == result["turn_id"]
    assert get("/v1/conversations/" + conversation)["last_turn"]["parent_turn_id"] == result["turn_id"]
    report["checks"].append({"name": "asking_parent_child_refresh", "status": "PASS"})
    key = uuid.uuid4().hex
    conversation, turn = start("低款和高款收纳箱如何选？", key=key)
    _, replay = start("低款和高款收纳箱如何选？", conversation, key=key)
    assert replay["request_id"] == turn["request_id"] and replay["status"] != "EXISTING"
    assert http(f"/v1/conversations/{conversation}/turns", {"text": "配送规则"}, key)[0] == 409
    status, body = http("/v1/turns/" + turn["request_id"] + "/cancel", {})
    assert status == 200 and json.loads(body)["status"] == "CANCEL_REQUESTED", "cancel did not reach active execution"
    assert wait(turn["request_id"])["status"] == "CANCELLED"
    assert http("/v1/turns/" + turn["request_id"] + "/cancel", {})[0] == 200
    report["checks"].append({"name": "idempotency_cancel_terminal", "status": "PASS"})
    assert http("/v1/conversations", {"shop_id": "demo-shop-central"})[0] == 403
    assert http("/v1/conversations/0")[0] == 404
    status, body = http(f"/v1/conversations/{conversation}/turns", {"text": ""})
    assert status == 400 and json.loads(body)["code"] == "invalid_query"
    report["checks"].append({"name": "authorization_structured_errors", "status": "PASS"})
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
