from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-doc-baseline.py"
HEAD = "a" * 40
DOCS = {
    "HANDOFF.md": "# Handoff\n\n代码基线 HEAD：`" + HEAD + "`\n当前验收章节：§1\n当前工程状态矩阵.md\n",
    "CODEX_GOAL持续开发计划.md": "# Goal\n\n代码基线 HEAD：`" + HEAD + "`\n当前工程状态矩阵.md §1 M1_PASS M2_SYNTHETIC_PASS M2_REAL_NOT_READY M3_SYNTHETIC_ADMIN_ONLY CONFIG_BLOCKED NOT_RUN P0：文档和状态同步 P1：阿里百炼 Provider 契约 P2：OSS 适配器 P3：受控 Crawler 来源 P4：统一解析与切片闭环 P5：合成 M2 纵向链路 P6：M3 合成管理回归 P7：真实输入前置门禁\n",
    "docs/README.md": "# Index\n",
    "docs/验证与验收.md": "## 1. 当前基线\n",
    "docs/当前工程状态矩阵.md": "当前 HEAD `" + HEAD + "` DESIGN IMPLEMENTED STATIC_PASS RUNTIME_PASS NOT_RUN BLOCKED CONFIG_BLOCKED M1_PASS M2_SYNTHETIC_PASS M2_REAL_NOT_READY M3_SYNTHETIC_ADMIN_ONLY PENDING_REVIEW\n",
}


class BaselineCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.private = self.repo / ".local/dev-docs"
        self.snapshot = self.private / "snapshots/current"
        self.snapshot.mkdir(parents=True)
        for relative, contents in DOCS.items():
            target = self.private / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        self.registry = {
            "acceptance_section": "1",
            "latest_snapshot": "snapshots/current",
            "documents": [
                {"path": path, "role": path, "manifest": True, "snapshot": True}
                for path in DOCS
            ],
        }
        self.write_registry()
        self.write_manifest()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "baseline@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Baseline Test"], check=True)
        (self.repo / "tracked").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "test"], check=True)
        head = self.current_head()
        for relative in ("HANDOFF.md", "CODEX_GOAL持续开发计划.md", "docs/当前工程状态矩阵.md"):
            target = self.private / relative
            target.write_text(target.read_text(encoding="utf-8").replace(HEAD, head), encoding="utf-8")
        self.write_manifest()
        self.write_snapshot()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_registry(self) -> None:
        (self.private / "ACTIVE-DOCS.json").write_text(json.dumps(self.registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def manifest_entries(self) -> dict[str, str]:
        paths = ["ACTIVE-DOCS.json", *DOCS]
        return {path: hashlib.sha256((self.private / path).read_bytes()).hexdigest() for path in paths}

    def write_manifest(self) -> None:
        entries = self.manifest_entries()
        manifest = "".join(f"{digest}  {path}\n" for path, digest in sorted(entries.items()))
        (self.private / "MANIFEST.sha256").write_text(manifest, encoding="utf-8")

    def write_snapshot(self) -> None:
        for relative in DOCS:
            target = self.snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.private / relative).read_bytes())
        (self.snapshot / "MANIFEST.sha256").write_bytes((self.private / "MANIFEST.sha256").read_bytes())
        (self.snapshot / "ACTIVE-DOCS.json").write_bytes((self.private / "ACTIVE-DOCS.json").read_bytes())
        (self.snapshot / "COMMANDS.txt").write_text("python3 scripts/check-doc-baseline.py: PASS\n", encoding="utf-8")
        (self.snapshot / "RUN.json").write_text(json.dumps({
            "code_head": self.current_head(),
            "acceptance_section": "1",
            "private_manifest_sha256": hashlib.sha256((self.private / "MANIFEST.sha256").read_bytes()).hexdigest(),
        }), encoding="utf-8")

    def current_head(self) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def run_checker(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["python3", str(SCRIPT), "--repo-root", str(self.repo)], check=False, capture_output=True, text=True)

    def test_valid_private_baseline_passes(self) -> None:
        self.write_snapshot()
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS private baseline", result.stdout)

    def test_public_clone_without_private_docs_skips(self) -> None:
        private = self.repo / ".local/dev-docs"
        moved = self.root / "private-backup"
        private.rename(moved)
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIP private baseline", result.stdout)

    def test_missing_active_document_fails(self) -> None:
        (self.private / "docs/README.md").unlink()
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)

    def test_duplicate_roles_fail(self) -> None:
        self.registry["documents"][1]["role"] = self.registry["documents"][0]["role"]
        self.write_registry()
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts", result.stderr)

    def test_manifest_tampering_fails(self) -> None:
        (self.private / "docs/README.md").write_text("changed\n", encoding="utf-8")
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch", result.stderr)

    def test_snapshot_head_mismatch_fails(self) -> None:
        run_path = self.snapshot / "RUN.json"
        record = json.loads(run_path.read_text(encoding="utf-8"))
        record["code_head"] = "b" * 40
        run_path.write_text(json.dumps(record), encoding="utf-8")
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("code_head does not match", result.stderr)

    def test_handoff_stale_head_fails(self) -> None:
        (self.private / "HANDOFF.md").write_text("代码基线 HEAD：`old`\n当前验收章节：§1\n当前工程状态矩阵.md\n", encoding="utf-8")
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current code HEAD does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
