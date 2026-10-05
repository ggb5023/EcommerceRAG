from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "scripts/crawler-smoke.py"
SPEC = importlib.util.spec_from_file_location("crawler_smoke", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class CrawlerSmokeTests(unittest.TestCase):
    def test_default_mode_does_not_network(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = MODULE._local_gate()
        self.assertEqual(result, 3)
        payload = json.loads(output.getvalue()[output.getvalue().find("{") :])
        self.assertEqual(payload["network_requests"], 0)
        self.assertEqual(payload["active_count"], 0)

    def test_registered_sources_are_fixed_and_pending(self):
        sources = MODULE._load_sources()
        self.assertEqual(len(sources), 3)
        self.assertTrue(all(source["status"] == "pending_review" for source in sources))
        self.assertTrue(all(len(source["fixed_urls"]) == 1 for source in sources))


if __name__ == "__main__":
    unittest.main()
