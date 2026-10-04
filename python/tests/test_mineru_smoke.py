from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.providers.mineru import MinerUConfig

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "mineru-smoke.py"
SPEC = importlib.util.spec_from_file_location("mineru_smoke_script", MODULE_PATH)
assert SPEC and SPEC.loader
mineru_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mineru_smoke)


def _args(root: Path, **kwargs):
    values = {
        "env": "/does/not/exist",
        "root": root,
        "limit": 0,
        "first_success": False,
        "poll_timeout": 1.0,
        "skip_document_id": [],
        "only_document_id": [],
        "output": root / "report.json",
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_selection_can_skip_completed_document_without_loading_credentials(tmp_path: Path, monkeypatch) -> None:
    manifest = {"sources": [
        {"document_id": "done", "file_name": "done.pdf", "source_url": "https://example.test/done.pdf"},
        {"document_id": "todo", "file_name": "todo.pdf", "source_url": "https://example.test/todo.pdf"},
    ]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "raw").mkdir()
    for name in ("done.pdf", "todo.pdf"):
        (tmp_path / "raw" / name).write_bytes(b"%PDF-test")

    class Client:
        def __init__(self, config):
            self.config = config

        async def submit_url(self, url, *, data_id):
            assert data_id == "todo"
            return "todo-task"

        async def wait(self, task_id):
            return {"data": {"result_url": "https://result.example/todo.zip"}}

    monkeypatch.setattr(mineru_smoke, "load_mineru_config", lambda path: MinerUConfig("https://mineru.test", "token"))
    monkeypatch.setattr(mineru_smoke, "MinerUClient", Client)
    exit_code = asyncio.run(mineru_smoke.run(_args(tmp_path, skip_document_id=["done"])))
    assert exit_code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert [row["document_id"] for row in report["documents"]] == ["todo"]


def test_only_document_id_can_select_one_source(tmp_path: Path, monkeypatch) -> None:
    manifest = {"sources": [
        {"document_id": "one", "file_name": "one.pdf", "source_url": "https://example.test/one.pdf"},
        {"document_id": "two", "file_name": "two.pdf", "source_url": "https://example.test/two.pdf"},
    ]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "raw").mkdir()
    for name in ("one.pdf", "two.pdf"):
        (tmp_path / "raw" / name).write_bytes(b"%PDF-test")

    class Client:
        def __init__(self, config):
            self.config = config

        async def submit_url(self, url, *, data_id):
            assert data_id == "two"
            return "two-task"

        async def wait(self, task_id):
            return {"data": {"result_url": "https://result.example/two.zip"}}

    monkeypatch.setattr(mineru_smoke, "load_mineru_config", lambda path: MinerUConfig("https://mineru.test", "token"))
    monkeypatch.setattr(mineru_smoke, "MinerUClient", Client)
    exit_code = asyncio.run(mineru_smoke.run(_args(tmp_path, only_document_id=["two"])))
    assert exit_code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert [row["document_id"] for row in report["documents"]] == ["two"]
