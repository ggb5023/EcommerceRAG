import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_mock_retrieval.py")
SPEC = importlib.util.spec_from_file_location("run_mock_retrieval", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class MockRetrievalEvaluationTests(unittest.TestCase):
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
        gate = MODULE.m2_gate_status()
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertFalse(gate["real_service_acceptance"])
        self.assertEqual(len(gate["missing"]), 7)
        self.assertEqual(gate["requirements"]["provider_endpoint_region_models"]["owner"], "ai_cloud_owner")
        self.assertIn("endpoint", gate["requirements"]["provider_endpoint_region_models"]["evidence"])

    def test_report_hash_ignores_timestamp(self):
        first = {"run": {"started_at": "2026-01-01T00:00:00Z"}, "results": {"count": 1}}
        second = {"run": {"started_at": "2027-01-01T00:00:00Z"}, "results": {"count": 1}}
        self.assertEqual(MODULE.report_sha256(first), MODULE.report_sha256(second))


if __name__ == "__main__":
    unittest.main()
