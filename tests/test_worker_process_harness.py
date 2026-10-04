from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "scripts" / "ingest-worker-process-smoke.sh"


def run_harness(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
        **env,
    }
    return subprocess.run(
        [str(HARNESS)],
        cwd=REPO,
        env=clean,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


class WorkerProcessHarnessTests(unittest.TestCase):
    def test_missing_pid_configuration_is_not_run(self) -> None:
        result = run_harness({})
        self.assertEqual(result.returncode, 3)
        self.assertIn("NOT_RUN/CONFIG_BLOCKED", result.stdout)
        self.assertIn("INGEST_WORKER_PID_FILE", result.stdout)

    def test_pid_one_is_rejected_before_http_or_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "worker.pid"
            restart = root / "restart.sh"
            pid_file.write_text("1\n")
            restart.write_text("#!/usr/bin/env bash\nexit 99\n")
            restart.chmod(0o700)
            result = run_harness(
                {
                    "ADMIN_HTTP_BASE": "http://127.0.0.1:8082",
                    "INGEST_WORKER_PID_FILE": str(pid_file),
                    "INGEST_WORKER_RESTART_SCRIPT": str(restart),
                    "INGEST_WORKER_JOB_ID": "fixture-job",
                    "INGEST_WORKER_PROCESS_MARKER": "gateway",
                }
            )
        self.assertEqual(result.returncode, 3)
        self.assertIn("refusing to kill PID 1", result.stdout)

    def test_process_marker_mismatch_does_not_kill_target(self) -> None:
        target = subprocess.Popen(["sleep", "30"])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pid_file = root / "worker.pid"
                restart = root / "restart.sh"
                pid_file.write_text(f"{target.pid}\n")
                restart.write_text("#!/usr/bin/env bash\nexit 99\n")
                restart.chmod(0o700)
                result = run_harness(
                    {
                        "ADMIN_HTTP_BASE": "http://127.0.0.1:8082",
                        "INGEST_WORKER_PID_FILE": str(pid_file),
                        "INGEST_WORKER_RESTART_SCRIPT": str(restart),
                        "INGEST_WORKER_JOB_ID": "fixture-job",
                        "INGEST_WORKER_PROCESS_MARKER": "gateway-that-is-not-present",
                    }
                )
            self.assertEqual(result.returncode, 3)
            self.assertIn("does not match INGEST_WORKER_PROCESS_MARKER", result.stdout)
            self.assertIsNone(target.poll(), "marker rejection must not kill the target")
        finally:
            target.terminate()
            target.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
