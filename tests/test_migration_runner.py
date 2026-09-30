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

    def test_runner_reports_legacy_and_rejects_mismatch(self):
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("LEGACY_UNVERIFIED", runner)
        self.assertIn("FAIL checksum mismatch", runner)
        self.assertIn("M1_APP_DATABASE_URL", runner)

if __name__ == "__main__":
    unittest.main()
