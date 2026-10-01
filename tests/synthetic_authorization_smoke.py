"""Isolated runtime matrix. Requires app PG env and migration DSN in env.

Only synthetic fixture authorization is changed, and it is restored in finally.
Logs contain statuses/counts, never credentials or response bodies.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data/synthetic/ecommerce-demo-v1/manifest.yaml"


def sql(statement: str) -> str:
    dsn = urllib.parse.urlsplit(os.environ["M1_MIGRATION_DATABASE_URL"])
    database = dsn.path.lstrip("/")
    assert "m1_test" in database, "isolated database required"
    env = {**os.environ, "PGHOST": dsn.hostname or "", "PGPORT": str(dsn.port or 5432),
           "PGDATABASE": database, "PGUSER": urllib.parse.unquote(dsn.username or ""),
           "PGPASSWORD": urllib.parse.unquote(dsn.password or "")}
    result = subprocess.run(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
                            input=statement, env=env, text=True, capture_output=True)
    if result.returncode:
        category = "connection_limit" if "remaining connection slots" in result.stderr or "too many clients" in result.stderr else "database_operation"
        raise AssertionError("synthetic fixture " + category + " failed")
    return result.stdout.strip()


def http(port, path, data=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}" + path,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read()
            return response.status, body
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def main():
    assert "m1_test" in os.environ.get("PGDATABASE", ""), "isolated app env required"
    directory = ROOT / ".local/synthetic-authorization-smoke"
    directory.mkdir(parents=True, exist_ok=True)
    processes, handles, gateways = [], [], {}

    def stop_gateways():
        for process in gateways.values():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)
        gateways.clear()

    def launch(argv, env, name):
        log = (directory / (name + ".log")).open("wb")
        handles.append(log)
        process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    base_env = {k: v for k, v in os.environ.items() if k not in
                {"DATABASE_URL", "M1_MIGRATION_DATABASE_URL", "M1_APP_DATABASE_URL"}}
    base_env.update(APP_ENV="test", SYNTHETIC_MANIFEST=str(MANIFEST))
    launch([str(ROOT / "python/.venv/bin/python"), "-m", "app.server"],
           {**base_env, "PYTHONPATH": str(ROOT / "python"), "GRPC_ADDR": "127.0.0.1:15051",
            "AUTHORITY_HTTP_BASE": "http://127.0.0.1:8080"}, "python")
    next_port = 18080

    def gateway(user, shop=""):
        nonlocal next_port
        port = next_port
        next_port += 1
        process = launch([str(ROOT / ".local/smoke/gateway")],
                         {**base_env, "SYNTHETIC_USER_ID": user, "SYNTHETIC_SHOP_ID": shop,
                          "RAG_GRPC_ADDR": "127.0.0.1:15051", "HTTP_ADDR": f"127.0.0.1:{port}"},
                         "gateway-" + str(port))
        gateways[port] = process
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, "fixture gateway stopped (see restricted log)"
            try:
                if http(port, "/healthz")[0] == 200:
                    return port
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError("gateway startup timed out")

    def turn(port, query):
        status, body = http(port, "/v1/conversations", {})
        assert status == 201, "create conversation failed"
        conversation = json.loads(body)["id"]
        status, body = http(port, f"/v1/conversations/{conversation}/turns", {"text": query},
                            {"Idempotency-Key": "synthetic-authorization-smoke"})
        assert status == 202, "create turn failed"
        request_id = json.loads(body)["request_id"]
        result = {}
        for _ in range(100):
            status, body = http(port, f"/v1/turns/{request_id}")
            assert status == 200, "poll turn failed"
            result = json.loads(body)
            if result["status"] in {"DONE", "FAILED", "ASKING", "CANCELLED"}:
                break
            time.sleep(0.05)
        assert result["status"] in {"DONE", "ASKING"}, "synthetic execution did not succeed"
        assert result["is_mock"] and not result["customer_reply"]["can_copy"], "mock copy gate changed"
        return conversation, request_id, result

    east_where = "tenant_id=(SELECT id FROM tenant WHERE name='Synthetic ecommerce demo v1') AND external_id='demo-agent-east'"
    policy_acl = "tenant_id=(SELECT id FROM tenant WHERE name='Synthetic ecommerce demo v1') AND resource_type='document' AND resource_id=(SELECT id FROM document WHERE logical_key='syn-policy-a' AND tenant_id=(SELECT id FROM tenant WHERE name='Synthetic ecommerce demo v1')) AND subject_type='role' AND subject_id='operator'"
    tenant_filter = "tenant_id=(SELECT id FROM tenant WHERE name='Synthetic ecommerce demo v1')"
    parent_acl_filter = tenant_filter + " AND resource_type IN ('tenant','source')"
    policy_filter = tenant_filter + " AND logical_key='syn-policy-a'"
    original_meta = sql("SELECT meta_json::text FROM document WHERE " + policy_filter + ";")
    assert sql("SELECT count(*) FROM acl WHERE " + parent_acl_filter + ";") == "0", "clean synthetic parent ACL fixture required"
    try:
        east = gateway("demo-agent-east")
        west = gateway("demo-agent-west")
        admin = gateway("demo-admin-a")
        owner = gateway("demo-owner-a")
        tenant_b = gateway("demo-agent-central")
        viewer = gateway("demo-viewer-a")
        for user in ("demo-no-shop", "demo-revoked"):
            assert http(gateway(user), "/v1/session")[0] == 403, "empty/revoked scope not denied"
        assert http(viewer, "/v1/session")[0] == 200
        assert http(viewer, "/v1/conversations", {})[0] == 403, "viewer write not denied"
        conversation, request_id, result = turn(east, "配送和退货规则是什么？")
        docs = {e["documentId"] for e in result["evidence"]}
        assert "syn-policy-a" in docs and "syn-restricted-a" not in docs, "operator document ACL failed"
        for port in (admin, owner):
            _, _, privileged = turn(port, "配送和退货规则是什么？")
            assert "syn-restricted-a" in {e["documentId"] for e in privileged["evidence"]}, "privileged read grant failed"
        _, _, western = turn(west, "晴岚马克杯陶瓷规格")
        assert {e["documentId"] for e in western["evidence"]} == {"syn-products-west"}
        _, _, other = turn(tenant_b, "RS-12万用表直流电压")
        assert {e["documentId"] for e in other["evidence"]} == {"syn-products-b"}
        for port in (west, tenant_b):
            assert http(port, f"/v1/conversations/{conversation}")[0] == 404
            assert http(port, f"/v1/turns/{request_id}")[0] == 404
            assert http(port, f"/v1/turns/{request_id}/events")[0] == 404
            assert http(port, f"/v1/turns/{request_id}/cancel", {})[0] == 404
            assert http(port, f"/v1/conversations/{conversation}/turns", {"text": "配送"},
                        {"Idempotency-Key": "denied"})[0] in {403, 404}
        assert http(east, "/v1/conversations", {"shop_id": "demo-shop-west"})[0] == 403
        assert http(gateway("demo-agent-east", "demo-shop-west"), "/v1/session")[0] == 403
        assert http(gateway("demo-admin-a", "demo-shop-west"), "/v1/session")[0] == 200
        status, stream = http(east, f"/v1/turns/{request_id}/events")
        assert status == 200 and b"event: evidence" in stream and b"event: delta" in stream
        event_ids = [int(line[4:]) for line in stream.splitlines() if line.startswith(b"id: ")]
        assert event_ids and event_ids == sorted(set(event_ids))
        status, replay = http(east, f"/v1/turns/{request_id}/events", headers={"Last-Event-ID": str(event_ids[0])})
        assert status == 200 and all(int(line[4:]) > event_ids[0] for line in replay.splitlines() if line.startswith(b"id: "))
        status, body = http(east, f"/v1/conversations/{conversation}")
        detail = json.loads(body)
        assert status == 200 and detail["last_turn"]["evidence"] and detail["last_turn"]["citations"]
        assert {c["evidence_id"] for c in detail["last_turn"]["citations"]} <= {e["id"] for e in detail["last_turn"]["evidence"]}
        asking_conversation, asking_id, asking = turn(east, "那个怎么样？")
        assert asking["status"] == "ASKING"
        status, body = http(east, f"/v1/conversations/{asking_conversation}")
        parent = json.loads(body)["last_turn"]["turn_id"]
        status, body = http(east, f"/v1/conversations/{asking_conversation}/turns",
                            {"text": "保温杯容量", "parent_turn_id": parent}, {"Idempotency-Key": "child"})
        assert status == 202, "ASKING child failed"
        child_id = json.loads(body)["request_id"]
        for _ in range(100):
            _, body = http(east, f"/v1/turns/{child_id}")
            child = json.loads(body)
            if child["status"] == "DONE":
                break
            time.sleep(0.05)
        assert child["status"] == "DONE" and child["evidence"], "child evidence failed"
        print("PASS synthetic roles, tenant/shop isolation, document ACL, HTTP/SSE replay, refresh and ASKING")
        stop_gateways()

        for mutation, expected in (("role='viewer'", "permission_changed"),
                                   ("shop_ids=ARRAY['demo-shop-west']", "permission_changed"),
                                   ("revoked_at=now()", "identity_revoked")):
            before = gateway("demo-agent-east")
            old_conversation, old_request, _ = turn(before, "配送规则")
            sql("UPDATE app_user SET " + mutation + " WHERE " + east_where + ";")
            for path in ("/v1/session", f"/v1/conversations/{old_conversation}",
                         f"/v1/turns/{old_request}", f"/v1/turns/{old_request}/events"):
                status, body = http(before, path, headers={"Last-Event-ID": "1"})
                assert status == 403 and json.loads(body)["code"] == expected
            sql("UPDATE app_user SET role='operator',shop_ids=ARRAY['demo-shop-east'],revoked_at=NULL WHERE " + east_where + ";")
            after = gateway("demo-agent-east")
            for path in (f"/v1/conversations/{old_conversation}", f"/v1/turns/{old_request}", f"/v1/turns/{old_request}/events"):
                status, body = http(after, path)
                assert status == 403 and json.loads(body)["code"] == "execution_permission_changed"
            assert old_conversation not in {c["id"] for c in json.loads(http(after, "/v1/conversations")[1])["items"]}
            assert http(after, f"/v1/conversations/{old_conversation}/turns", {"text": "配送"},
                        {"Idempotency-Key": "stale-history"})[0] == 403
            stop_gateways()

        for gate in ("tenant", "source"):
            before = gateway("demo-agent-east")
            target = "d.tenant_id" if gate == "tenant" else "d.source_id"
            sql("INSERT INTO acl(tenant_id,resource_type,resource_id,subject_type,subject_id,permission) "
                "SELECT d.tenant_id,'" + gate + "'," + target + ", 'role','admin','read' FROM document d "
                "WHERE d.logical_key='syn-policy-a' AND d." + tenant_filter + ";")
            assert http(before, "/v1/session")[0] == 403, "parent ACL failed to invalidate revision"
            for user in ("demo-agent-east", "demo-owner-a"):
                _, _, denied = turn(gateway(user), "配送和退货规则")
                assert "syn-policy-a" not in {e["documentId"] for e in denied["evidence"]}, "parent ACL gate or owner bypass failed"
                if gate == "tenant":
                    assert not denied["evidence"], "tenant ACL did not narrow all resources"
            _, _, granted = turn(gateway("demo-admin-a"), "配送和退货规则")
            assert "syn-policy-a" in {e["documentId"] for e in granted["evidence"]}
            sql("DELETE FROM acl WHERE " + parent_acl_filter + ";")
            stop_gateways()
        before = gateway("demo-agent-east")
        sql("UPDATE document SET meta_json=jsonb_set(COALESCE(meta_json,'{}'::jsonb),'{disclosure_class}', '\"internal_only\"') WHERE " + policy_filter + ";")
        assert http(before, "/v1/session")[0] == 403
        _, _, denied = turn(gateway("demo-agent-east"), "配送规则")
        assert not denied["evidence"], "reclassified document still returned"
        sql("UPDATE document SET meta_json='" + original_meta.replace("'", "''") + "'::jsonb WHERE " + policy_filter + ";")
        stop_gateways()
        before = gateway("demo-agent-east")
        sql("UPDATE tenant SET status='disabled' WHERE name='Synthetic ecommerce demo v1';")
        assert http(before, "/v1/session")[0] == 403, "disabled tenant still authorized"
        sql("UPDATE tenant SET status='active' WHERE name='Synthetic ecommerce demo v1';")
        stop_gateways()
        print("PASS tenant/source ACL AND gates, owner constraints, document classification and tenant status invalidation")
        before = gateway("demo-agent-east")
        old_conversation, old_request, _ = turn(before, "配送规则")
        sql("DELETE FROM acl WHERE " + policy_acl + ";")
        assert http(before, "/v1/session")[0] == 403, "ACL revision not invalidated"
        after = gateway("demo-agent-east")
        _, _, result = turn(after, "配送规则")
        assert "syn-policy-a" not in {e["documentId"] for e in result["evidence"]}, "revoked ACL still returned"
        assert http(after, f"/v1/turns/{old_request}/events")[0] == 403
        print("PASS role/shop/revocation/ACL changes invalidate old session, history and replay")
        print(json.dumps({"status": "PASS", "real_service_acceptance": False,
                          "identity": "synthetic", "result_cache": "NOT_IMPLEMENTED"}))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for handle in handles:
            handle.close()
        sql("DELETE FROM acl WHERE " + parent_acl_filter + ";")
        sql("UPDATE tenant SET status='active' WHERE name='Synthetic ecommerce demo v1';")
        sql("UPDATE document SET meta_json='" + original_meta.replace("'", "''") + "'::jsonb WHERE " + policy_filter + ";")
        sql("UPDATE app_user SET role='operator',shop_ids=ARRAY['demo-shop-east'],revoked_at=NULL WHERE " + east_where + ";")
        sql("INSERT INTO acl(tenant_id,resource_type,resource_id,subject_type,subject_id,permission) SELECT d.tenant_id,'document',d.id,'role','operator','read' FROM document d JOIN tenant t ON t.id=d.tenant_id WHERE t.name='Synthetic ecommerce demo v1' AND d.logical_key='syn-policy-a' ON CONFLICT DO NOTHING;")


if __name__ == "__main__":
    main()
