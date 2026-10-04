#!/usr/bin/env python3
"""Read-only consistency checks for the private engineering document baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


REQUIRED_PATHS = {
    "HANDOFF.md",
    "CODEX_GOAL持续开发计划.md",
    "docs/README.md",
    "docs/验证与验收.md",
    "docs/当前工程状态矩阵.md",
}
REQUIRED_STATES = {
    "DESIGN",
    "IMPLEMENTED",
    "STATIC_PASS",
    "RUNTIME_PASS",
    "NOT_RUN",
    "BLOCKED",
    "CONFIG_BLOCKED",
    "M1_PASS",
    "M2_SYNTHETIC_PASS",
    "M2_REAL_NOT_READY",
    "M3_SYNTHETIC_ADMIN_ONLY",
    "PENDING_REVIEW",
}
REQUIRED_GOAL_STAGES = (
    "启动与基线复核",
    "工程闭环稳定",
    "M2 对齐评测收口",
    "摄取和解析闭环",
    "检索和模型接入",
    "来源与 crawler",
    "M3 生产化前置",
    "发布和文档闭环",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        return None
    return candidate.as_posix()


def git_head(repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def check(repo_root: Path, private_root: Path) -> list[str]:
    if not private_root.is_dir():
        print("SKIP private baseline: .local/dev-docs is absent")
        return []

    errors: list[str] = []

    def error(path: str, message: str, line: int | None = None) -> None:
        suffix = f":{line}" if line is not None else ""
        errors.append(f"{path}{suffix}: {message}")

    registry_path = private_root / "ACTIVE-DOCS.json"
    if not registry_path.is_file():
        return ["ACTIVE-DOCS.json: missing private active document registry"]
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"ACTIVE-DOCS.json: invalid registry: {exc}"]

    documents = registry.get("documents") if isinstance(registry, dict) else None
    if not isinstance(documents, list):
        return ["ACTIVE-DOCS.json: documents must be an array"]

    paths: set[str] = set()
    roles: dict[str, str] = {}
    registered: list[dict[str, object]] = []
    for index, entry in enumerate(documents):
        if not isinstance(entry, dict):
            error("ACTIVE-DOCS.json", f"documents[{index}] must be an object")
            continue
        path = relative_path(entry.get("path"))
        role = entry.get("role")
        if path is None:
            error("ACTIVE-DOCS.json", f"documents[{index}].path must be a safe relative path")
            continue
        if path in paths:
            error("ACTIVE-DOCS.json", f"duplicate document path {path}")
        paths.add(path)
        if not isinstance(role, str) or not role:
            error("ACTIVE-DOCS.json", f"{path} has no role")
        elif role in roles:
            error("ACTIVE-DOCS.json", f"role {role} conflicts between {roles[role]} and {path}")
        else:
            roles[role] = path
        for flag in ("manifest", "snapshot"):
            if not isinstance(entry.get(flag), bool):
                error("ACTIVE-DOCS.json", f"{path}.{flag} must be boolean")
        registered.append(entry)

    for required in REQUIRED_PATHS:
        if required not in paths:
            error("ACTIVE-DOCS.json", f"required current document is not registered: {required}")

    docs_root = private_root / "docs"
    current_docs = {
        path.relative_to(private_root).as_posix()
        for path in docs_root.glob("*.md")
    } if docs_root.is_dir() else set()
    registered_docs = {path for path in paths if path.startswith("docs/") and path.endswith(".md")}
    for path in sorted(current_docs - registered_docs):
        error("ACTIVE-DOCS.json", f"active Markdown is not registered: {path}")
    for path in sorted(registered_docs - current_docs):
        error("ACTIVE-DOCS.json", f"registered Markdown is not an active docs file: {path}")

    head = git_head(repo_root)
    if head is None:
        error("git", "cannot resolve repository HEAD")

    acceptance_path = private_root / "docs/验证与验收.md"
    acceptance = acceptance_path.read_text(encoding="utf-8") if acceptance_path.is_file() else ""
    headings = list(re.finditer(r"^##\s+(\d+)\.\s+(.+)$", acceptance, re.MULTILINE))
    if not headings:
        error("docs/验证与验收.md", "no numbered acceptance chapters found")
        latest_section, latest_heading = None, ""
    else:
        latest_section = headings[-1].group(1)
        latest_heading = headings[-1].group(2)
    expected_section = str(registry.get("acceptance_section", ""))
    if latest_section != expected_section or "当前基线" not in latest_heading:
        error("docs/验证与验收.md", f"latest chapter must be current baseline §{expected_section}")

    handoff_path = private_root / "HANDOFF.md"
    handoff = handoff_path.read_text(encoding="utf-8") if handoff_path.is_file() else ""
    if head and f"代码基线 HEAD：`{head}`" not in handoff:
        error("HANDOFF.md", "current code HEAD does not match git HEAD", 1)
    if f"当前验收章节：§{expected_section}" not in handoff:
        error("HANDOFF.md", f"must point to current acceptance chapter §{expected_section}", 1)
    if "当前工程状态矩阵.md" not in handoff:
        error("HANDOFF.md", "must link the current status matrix", 1)

    matrix_path = private_root / "docs/当前工程状态矩阵.md"
    matrix = matrix_path.read_text(encoding="utf-8") if matrix_path.is_file() else ""
    if head and f"当前 HEAD `{head}`" not in matrix:
        error("docs/当前工程状态矩阵.md", "current code HEAD does not match git HEAD")
    for state in REQUIRED_STATES:
        if state not in matrix:
            error("docs/当前工程状态矩阵.md", f"required state or boundary missing: {state}")

    goal_path = private_root / "CODEX_GOAL持续开发计划.md"
    goal = goal_path.read_text(encoding="utf-8") if goal_path.is_file() else ""
    for required in ("当前工程状态矩阵.md", f"§{expected_section}", "syn-005", "启动与基线复核", "发布和文档闭环"):
        if required not in goal:
            error("CODEX_GOAL持续开发计划.md", f"required current protocol reference missing: {required}")
    if head and f"代码基线 HEAD：`{head}`" not in goal:
        error("CODEX_GOAL持续开发计划.md", "current code HEAD does not match git HEAD")
    if re.search(r"^##\s+\d+\..*当前接管任务", goal, re.MULTILINE):
        error("CODEX_GOAL持续开发计划.md", "duplicate historical takeover section remains")

    manifest_path = private_root / "MANIFEST.sha256"
    manifest_entries: dict[str, str] = {}
    if not manifest_path.is_file():
        error("MANIFEST.sha256", "private manifest is missing")
    else:
        for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not match:
                error("MANIFEST.sha256", "expected '<sha256><two spaces><relative path>'", line_number)
                continue
            digest, raw_path = match.groups()
            path = relative_path(raw_path)
            if path is None:
                error("MANIFEST.sha256", f"unsafe path {raw_path}", line_number)
                continue
            if path in manifest_entries:
                error("MANIFEST.sha256", f"duplicate path {path}", line_number)
            manifest_entries[path] = digest
            target = private_root / path
            if not target.is_file():
                error("MANIFEST.sha256", f"listed file is missing: {path}", line_number)
            elif sha256(target) != digest:
                error("MANIFEST.sha256", f"SHA-256 mismatch: {path}", line_number)

    expected_manifest = {entry.get("path") for entry in registered if entry.get("manifest") is True}
    expected_manifest.add("ACTIVE-DOCS.json")
    if set(manifest_entries) != expected_manifest:
        missing = sorted(expected_manifest - set(manifest_entries))
        extra = sorted(set(manifest_entries) - expected_manifest)
        error("MANIFEST.sha256", f"coverage differs from registry; missing={missing}, extra={extra}")

    snapshot_rel = relative_path(registry.get("latest_snapshot"))
    if snapshot_rel is None:
        error("ACTIVE-DOCS.json", "latest_snapshot must be a safe relative path")
        return errors
    if not snapshot_rel.startswith("snapshots/"):
        error("ACTIVE-DOCS.json", "latest_snapshot must point inside snapshots/")
    snapshot_root = private_root / snapshot_rel
    run_path = snapshot_root / "RUN.json"
    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"{snapshot_rel}/RUN.json", f"snapshot run record is invalid: {exc}")
        return errors
    if head and run.get("code_head") != head:
        error(f"{snapshot_rel}/RUN.json", "snapshot code_head does not match git HEAD")
    if str(run.get("acceptance_section")) != expected_section:
        error(f"{snapshot_rel}/RUN.json", f"snapshot must reference acceptance §{expected_section}")
    manifest_hash = sha256(manifest_path) if manifest_path.is_file() else None
    if run.get("private_manifest_sha256") != manifest_hash:
        error(f"{snapshot_rel}/RUN.json", "snapshot private manifest hash does not match current MANIFEST.sha256")
    snapshot_manifest = snapshot_root / "MANIFEST.sha256"
    if not snapshot_manifest.is_file() or snapshot_manifest.read_bytes() != manifest_path.read_bytes():
        error(f"{snapshot_rel}/MANIFEST.sha256", "snapshot manifest differs from current private manifest")

    snapshot_registry = snapshot_root / "ACTIVE-DOCS.json"
    if not snapshot_registry.is_file() or snapshot_registry.read_bytes() != registry_path.read_bytes():
        error(f"{snapshot_rel}/ACTIVE-DOCS.json", "snapshot active document registry is missing or stale")

    for entry in registered:
        if entry.get("snapshot") is not True:
            continue
        path = relative_path(entry.get("path"))
        if path is None:
            continue
        source = private_root / path
        copy = snapshot_root / path
        if not source.is_file() or not copy.is_file():
            error(f"{snapshot_rel}/{path}", "registered current document copy is missing")
        elif sha256(source) != sha256(copy):
            error(f"{snapshot_rel}/{path}", "snapshot current document copy is stale")

    if not (snapshot_root / "COMMANDS.txt").is_file():
        error(f"{snapshot_rel}/COMMANDS.txt", "snapshot command results are missing")

    for stage in REQUIRED_GOAL_STAGES:
        if stage not in goal:
            error("CODEX_GOAL持续开发计划.md", f"required end-to-end stage is missing: {stage}")
    if "§78" in goal or "§74" in goal:
        error("CODEX_GOAL持续开发计划.md", "goal refers to a stale current acceptance section")
    if "syn-005" not in goal or "PENDING_REVIEW" not in goal or "NOT_RUN" not in goal:
        error("CODEX_GOAL持续开发计划.md", "goal must keep the unresolved syn-005 gate explicit")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--private-root", type=Path)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    private_root = (args.private_root or repo_root / ".local/dev-docs").resolve()
    errors = check(repo_root, private_root)
    if errors:
        for message in errors:
            print(f"FAIL private baseline: {message}", file=sys.stderr)
        return 1
    if private_root.is_dir():
        print("PASS private baseline: active docs, HEAD, acceptance, manifest and snapshot agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
