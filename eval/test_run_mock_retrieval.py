import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("run_mock_retrieval.py")
SPEC = importlib.util.spec_from_file_location("run_mock_retrieval", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class MockRetrievalEvaluationTests(unittest.TestCase):
    def test_case_loader_rejects_duplicate_keys_and_non_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "duplicate.jsonl"
            duplicate.write_text('{"case_id":"a","case_id":"b"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                MODULE.load_cases(duplicate, None)

            non_object = root / "non-object.jsonl"
            non_object.write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "must be a JSON object"):
                MODULE.load_cases(non_object, None)

    def test_classifies_refusal_unauthorized_and_clarification(self):
        cases = [
            {"case_id": "r", "tags": ["unanswerable"]},
            {"case_id": "u", "tags": ["unauthorized"]},
            {"case_id": "c", "tags": ["multi_turn"]},
        ]
        self.assertEqual([MODULE.classify(case) for case in cases], ["refusal", "unauthorized", "clarification"])

    def test_integrity_reports_missing_documents_duplicate_ids_and_tag_conflict(self):
        cases = [
            {"case_id": "x", "tags": ["authorized", "unauthorized"], "expected_doc_ids": []},
            {"case_id": "x", "tags": [], "expected_doc_ids": []},
        ]
        _, _, issues = MODULE.evaluate_cases(cases)
        self.assertIn("missing_expected_docs:x", issues)
        self.assertIn("duplicate_or_missing_case_id:x", issues)

    def test_case_results_are_metadata_only(self):
        case = {
            "case_id": "synth-001",
            "query": "private query text",
            "tags": ["synthetic", "product_knowledge"],
            "expected_doc_ids": ["doc-1"],
            "authorization": {"scope": "tenant_shop"},
        }
        result = MODULE.case_results([case])[0]
        self.assertEqual(result["status"], "NOT_RUN")
        self.assertEqual(result["authorization_scope"], "tenant_shop")
        self.assertNotIn("query", result)
        self.assertNotIn("private query text", str(result))

    def test_missing_fixture_keeps_retrieval_not_run(self):
        case = {"case_id": "r", "tags": ["product_knowledge"], "expected_doc_ids": ["doc-1"]}
        result = MODULE.case_results([case])[0]
        self.assertEqual(result["status"], "NOT_RUN")
        self.assertEqual(result["hit_doc_count"], 0)

    def test_fixture_computes_hit_and_coverage(self):
        cases = [
            {"case_id": "r1", "tags": ["product_knowledge"], "expected_doc_ids": ["doc-1", "doc-2"]},
            {"case_id": "r2", "tags": ["product_knowledge"], "expected_doc_ids": ["doc-3"]},
        ]
        result = MODULE.case_results(cases, {"doc-1"})
        self.assertEqual(result[0]["status"], "PASS")
        self.assertEqual(result[1]["status"], "FAIL")
        self.assertEqual(result[0]["hit_doc_count"], 1)

    def test_run_metadata_is_deterministic_except_timestamp(self):
        cases = [{"case_id": "r", "tags": ["product_knowledge"], "expected_doc_ids": ["doc-1"]}]
        first = MODULE.case_results(cases, {"doc-1"})
        second = MODULE.case_results(cases, {"doc-1"})
        self.assertEqual(first, second)

    def test_detects_input_source_version_drift(self):
        cases = [{"case_id": "x", "source": {"source_version": "old", "type": "synthetic"}}]
        issues = MODULE.input_drift_issues(cases, {"eval_set_version": "current", "source_type": "synthetic"})
        self.assertEqual(issues, ["source_version_drift:x"])

    def test_authorization_checks_preserve_synthetic_scope_semantics(self):
        cases = [{
            "case_id": "u",
            "tags": ["unauthorized"],
            "authorization": {"tenant_id": "t", "shop_id": "shop-demo", "role": "operator"},
            "expected_doc_ids": ["syn-acl-shop"],
        }]
        self.assertEqual(MODULE.authorization_issues(cases), [])

    def test_authorization_requires_fields_and_target_for_unauthorized_case(self):
        cases = [{"case_id": "u", "tags": ["unauthorized"], "authorization": {}, "expected_doc_ids": []}]
        issues = MODULE.authorization_issues(cases)
        self.assertIn("incomplete_authorization:u", issues)
        self.assertIn("unauthorized_missing_target_evidence:u", issues)

    def test_semantic_checks_require_guidance_and_clarification_evidence(self):
        refusal = {"case_id": "r", "tags": ["unanswerable"], "expected_answer_points": []}
        clarification = {"case_id": "c", "tags": ["multi_turn"], "expected_answer_points": ["补充对象"]}
        issues = MODULE.semantic_issues([refusal, clarification])
        self.assertIn("missing_refusal_guidance:r", issues)
        self.assertIn("incomplete_clarification_evidence:c", issues)

    def test_fixture_reports_missing_expected_documents(self):
        cases = [{"case_id": "r", "tags": ["product_knowledge"], "expected_doc_ids": ["d1", "d2"]}]
        result = MODULE.case_results(cases, {"d1"})[0]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["hit_doc_count"], 1)

    def test_case_status_summary_contains_only_case_ids_for_failures(self):
        cases = [
            {"case_id": "ok", "tags": ["product_knowledge"], "expected_doc_ids": ["d1"]},
            {"case_id": "bad", "tags": ["product_knowledge"], "expected_doc_ids": ["d2"]},
        ]
        rows = MODULE.case_results(cases, {"d1"})
        counts = __import__("collections").Counter(row["status"] for row in rows)
        failed = [row["case_id"] for row in rows if row["status"] == "FAIL"]
        self.assertEqual(counts, {"PASS": 1, "FAIL": 1})
        self.assertEqual(failed, ["bad"])

    def test_m2_gate_is_blocked_until_external_inputs_are_recorded(self):
        gate, manifest_sha = MODULE.m2_gate_status()
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertFalse(gate["real_service_acceptance"])
        self.assertEqual(len(gate["missing"]), 7)
        self.assertIsNone(manifest_sha)
        self.assertEqual(gate["requirements"]["provider_endpoint_region_models"]["owner"], "ai_cloud_owner")
        self.assertIn("endpoint", gate["requirements"]["provider_endpoint_region_models"]["evidence"])

    def test_reviewed_gate_manifest_can_be_ready_without_claiming_real_acceptance(self):
        requirements = {
            key: {"ready": True, "owner": owner, "evidence": evidence}
            for key, (owner, evidence) in MODULE.GATE_REQUIREMENTS.items()
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "gate.json"
            manifest.write_text(json.dumps({
                "gate_version": MODULE.GATE_VERSION,
                "status": "READY",
                "real_service_acceptance": False,
                "requirements": requirements,
            }))
            gate, manifest_sha = MODULE.m2_gate_status(manifest)
        self.assertEqual(gate["status"], "READY")
        self.assertFalse(gate["real_service_acceptance"])
        self.assertTrue(manifest_sha)

    def test_repository_external_input_gate_is_explicitly_blocked(self):
        manifest = MODULE_PATH.parent / "m2-external-input-gate.json"
        gate, manifest_sha = MODULE.m2_gate_status(manifest)
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertEqual(len(gate["missing"]), 7)
        self.assertTrue(manifest_sha)
        self.assertFalse(gate["real_service_acceptance"])

    def test_gate_manifest_rejects_version_status_and_unknown_fields(self):
        requirements = {
            key: {"ready": False, "owner": owner, "evidence": evidence}
            for key, (owner, evidence) in MODULE.GATE_REQUIREMENTS.items()
        }
        base = {
            "gate_version": MODULE.GATE_VERSION,
            "status": "BLOCKED",
            "real_service_acceptance": False,
            "requirements": requirements,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_version = root / "invalid-version.json"
            invalid_version.write_text(json.dumps({**base, "gate_version": "old"}))
            with self.assertRaisesRegex(ValueError, "version"):
                MODULE.m2_gate_status(invalid_version)

            invalid_status = root / "invalid-status.json"
            invalid_status.write_text(json.dumps({**base, "status": "READY"}))
            with self.assertRaisesRegex(ValueError, "status"):
                MODULE.m2_gate_status(invalid_status)

            malformed_status = root / "malformed-status.json"
            malformed_status.write_text(json.dumps({**base, "status": []}))
            with self.assertRaisesRegex(ValueError, "status"):
                MODULE.m2_gate_status(malformed_status)

            duplicate_key = root / "duplicate-key.json"
            duplicate_key.write_text(
                '{"gate_version":"m2-external-input-gate-v1",'
                '"status":"BLOCKED","status":"READY",'
                '"real_service_acceptance":false,"requirements":{}}'
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                MODULE.m2_gate_status(duplicate_key)

            unknown_requirement = root / "unknown-requirement.json"
            unknown_requirement.write_text(json.dumps({
                **base,
                "requirements": {**requirements, "future_requirement": {
                    "ready": False, "owner": "future", "evidence": "future"
                }},
            }))
            with self.assertRaisesRegex(ValueError, "unknown requirement"):
                MODULE.m2_gate_status(unknown_requirement)

            unknown_root_field = root / "unknown-root-field.json"
            unknown_root_field.write_text(json.dumps({**base, "provider_api_key": "must-not-be-recorded"}))
            with self.assertRaisesRegex(ValueError, "unknown field"):
                MODULE.m2_gate_status(unknown_root_field)

            unknown_requirement_field = root / "unknown-requirement-field.json"
            requirement_rows = {
                **requirements,
                "identity_roles_revocation": {
                    **requirements["identity_roles_revocation"],
                    "evidence_path": "/etc/ecommerce-rag/secret",
                },
            }
            unknown_requirement_field.write_text(json.dumps({**base, "requirements": requirement_rows}))
            with self.assertRaisesRegex(ValueError, "unknown field in identity_roles_revocation"):
                MODULE.m2_gate_status(unknown_requirement_field)

            claimed_acceptance = root / "claimed-acceptance.json"
            claimed_acceptance.write_text(json.dumps({
                **base, "real_service_acceptance": True,
            }))
            with self.assertRaisesRegex(ValueError, "real service acceptance"):
                MODULE.m2_gate_status(claimed_acceptance)

            missing_requirement = root / "missing-requirement.json"
            missing = dict(requirements)
            missing.pop("identity_roles_revocation")
            missing_requirement.write_text(json.dumps({**base, "requirements": missing}))
            with self.assertRaisesRegex(TypeError, "missing requirement"):
                MODULE.m2_gate_status(missing_requirement)

            owner_drift = root / "owner-drift.json"
            owner_rows = {
                **requirements,
                "identity_roles_revocation": {
                    **requirements["identity_roles_revocation"],
                    "owner": "unexpected_owner",
                },
            }
            owner_drift.write_text(json.dumps({**base, "requirements": owner_rows}))
            with self.assertRaisesRegex(ValueError, "owner/evidence mismatch"):
                MODULE.m2_gate_status(owner_drift)

            evidence_drift = root / "evidence-drift.json"
            evidence_rows = {
                **requirements,
                "identity_roles_revocation": {
                    **requirements["identity_roles_revocation"],
                    "evidence": "unreviewed evidence",
                },
            }
            evidence_drift.write_text(json.dumps({**base, "requirements": evidence_rows}))
            with self.assertRaisesRegex(ValueError, "owner/evidence mismatch"):
                MODULE.m2_gate_status(evidence_drift)

            ready_type = root / "ready-type.json"
            ready_rows = {
                **requirements,
                "identity_roles_revocation": {
                    **requirements["identity_roles_revocation"],
                    "ready": "false",
                },
            }
            ready_type.write_text(json.dumps({**base, "requirements": ready_rows}))
            with self.assertRaisesRegex(TypeError, "ready must be boolean"):
                MODULE.m2_gate_status(ready_type)

    def test_report_hash_ignores_timestamp(self):
        first = {"run": {"started_at": "2026-01-01T00:00:00Z"}, "results": {"count": 1}}
        second = {"run": {"started_at": "2027-01-01T00:00:00Z"}, "results": {"count": 1}}
        self.assertEqual(MODULE.report_sha256(first), MODULE.report_sha256(second))


if __name__ == "__main__":
    unittest.main()
