from __future__ import annotations

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/test-migrations.sh"


class MigrationRunnerStaticTests(unittest.TestCase):
    def test_versions_are_contiguous_and_ordered(self):
        versions = sorted(path.name[:4] for path in (ROOT / "sql/migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
        self.assertEqual(versions, [f"{number:04d}" for number in range(1, len(versions) + 1)])

    def test_new_migration_records_valid_raw_file_checksum(self):
        migration = ROOT / "sql/migrations/0004_migration_checksum.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("checksum_sha256", sql)
        self.assertIn(":'migration_checksum'", sql)
        self.assertRegex(sql, r"\^\[0-9a-f\]\{64\}\$")
        self.assertIn("sha256sum", RUNNER.read_text(encoding="utf-8"))

    def test_every_post_checksum_migration_receives_the_raw_file_hash(self):
        for migration in sorted((ROOT / "sql/migrations").glob("000[5-9]_*.sql")):
            self.assertIn(":'migration_checksum'", migration.read_text(encoding="utf-8"), migration.name)

    def test_runner_reports_legacy_and_rejects_mismatch(self):
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("LEGACY_UNVERIFIED", runner)
        self.assertIn("FAIL checksum mismatch", runner)
        self.assertIn("M1_APP_DATABASE_URL", runner)

    def test_synthetic_authorization_constraints_are_versioned(self):
        migration = ROOT / "sql/migrations/0008_synthetic_auth_constraints.sql"
        sql = migration.read_text(encoding="utf-8")
        for fragment in (
            "app_user_role_check",
            "acl_resource_type_check",
            "acl_subject_type_check",
            "acl_permission_check",
            "acl_tenant_resource_subject_key",
            "ecr_validate_acl_scope",
            ":'migration_checksum'",
        ):
            self.assertIn(fragment, sql)
        self.assertIn("m1-auth-constraints.sql", RUNNER.read_text(encoding="utf-8"))

    def test_admin_role_catalog_is_versioned_and_read_only_for_app(self):
        migration = ROOT / "sql/migrations/0014_admin_role_catalog.sql"
        sql = migration.read_text(encoding="utf-8")
        for fragment in (
            "CREATE TABLE admin_role_catalog",
            "platform_access_admin",
            "platform_role_approver",
            "support_permission_reviewer",
            "merchant_member",
            "GRANT SELECT ON admin_role_catalog TO rag_app",
            ":'migration_checksum'",
        ):
            self.assertIn(fragment, sql)

if __name__ == "__main__":
    unittest.main()
