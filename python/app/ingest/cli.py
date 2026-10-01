from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ALLOWED_FORMATS = {"csv", "markdown"}
ALLOWED_DISCLOSURE = {"external_allowed", "internal_only", "unclassified"}
ALLOWED_ROLES = {"owner", "admin", "operator", "viewer"}
STATE_ROOT = Path(".local/ingest-state")


@dataclass(frozen=True)
class ValidationResult:
    manifest: dict[str, Any]
    root: Path
    files: dict[str, dict[str, Any]]
    errors: list[str]
    manifest_sha256: str
    dataset_sha256: str

    @property
    def ok(self) -> bool:
        return not self.errors


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_safe_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _date(value: Any, field: str, errors: list[str]) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        errors.append(f"{field}: must be YYYY-MM-DD or null")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field}: invalid date {value!r}")
        return None


def _nonempty(value: Any, field: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: required non-empty string")
        return ""
    return value.strip()


def _load_yaml(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        errors.append(f"manifest: cannot read YAML: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append("manifest: top-level value must be a mapping")
        return {}
    return value


def validate_manifest(manifest_path: Path) -> ValidationResult:
    manifest_path = manifest_path.resolve()
    errors: list[str] = []
    if not manifest_path.is_file():
        return ValidationResult({}, manifest_path.parent, {}, [f"manifest not found: {manifest_path}"], "", "")
    raw_manifest = manifest_path.read_bytes()
    manifest = _load_yaml(manifest_path, errors)
    root = manifest_path.parent
    if manifest.get("schema_version") != 1:
        errors.append("schema_version: must be 1")
    for field in ("dataset_id", "pipeline_version", "license"):
        _nonempty(manifest.get(field), field, errors)
    if manifest.get("synthetic") is not True or manifest.get("provisional") is not True:
        errors.append("manifest: synthetic and provisional must both be true")
    if manifest.get("license") != "internal-generated":
        errors.append("manifest: license must be internal-generated")

    tenants = manifest.get("tenants")
    documents = manifest.get("documents")
    if not isinstance(tenants, list) or not tenants:
        errors.append("tenants: must be a non-empty list")
        tenants = []
    if not isinstance(documents, list) or not documents:
        errors.append("documents: must be a non-empty list")
        documents = []

    tenant_shops: dict[str, set[str]] = {}
    principal_keys: set[tuple[str, str]] = set()
    for index, tenant in enumerate(tenants):
        prefix = f"tenants[{index}]"
        if not isinstance(tenant, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        tenant_id = _nonempty(tenant.get("tenant_id"), f"{prefix}.tenant_id", errors)
        shops = tenant.get("shops")
        if not isinstance(shops, list) or not shops or not all(isinstance(s, str) and s for s in shops):
            errors.append(f"{prefix}.shops: must be a non-empty list of strings")
            shops = []
        if tenant_id in tenant_shops:
            errors.append(f"duplicate tenant_id: {tenant_id}")
        tenant_shops[tenant_id] = set(shops)
        principals = tenant.get("principals")
        if not isinstance(principals, list):
            errors.append(f"{prefix}.principals: must be a list")
            principals = []
        for pindex, principal in enumerate(principals):
            pfx = f"{prefix}.principals[{pindex}]"
            if not isinstance(principal, dict):
                errors.append(f"{pfx}: must be a mapping")
                continue
            user_id = _nonempty(principal.get("user_id"), f"{pfx}.user_id", errors)
            role = _nonempty(principal.get("role"), f"{pfx}.role", errors)
            if role not in ALLOWED_ROLES:
                errors.append(f"{pfx}.role: unsupported role {role!r}")
            pshops = principal.get("shop_ids")
            if not isinstance(pshops, list) or not all(isinstance(s, str) and s for s in pshops):
                errors.append(f"{pfx}.shop_ids: must be a list of strings")
                pshops = []
            if not set(pshops).issubset(set(shops)):
                errors.append(f"{pfx}.shop_ids: contains shop outside tenant")
            key = (tenant_id, user_id)
            if key in principal_keys:
                errors.append(f"duplicate principal: {tenant_id}/{user_id}")
            principal_keys.add(key)

    document_ids: set[str] = set()
    files: dict[str, dict[str, Any]] = {}
    for index, document in enumerate(documents):
        prefix = f"documents[{index}]"
        if not isinstance(document, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        document_id = _nonempty(document.get("document_id"), f"{prefix}.document_id", errors)
        if document_id in document_ids:
            errors.append(f"duplicate document_id: {document_id}")
        document_ids.add(document_id)
        relative = document.get("path")
        if not _is_safe_relative(relative):
            errors.append(f"{prefix}.path: must be a safe relative POSIX path")
            continue
        fmt = _nonempty(document.get("format"), f"{prefix}.format", errors)
        if fmt not in ALLOWED_FORMATS:
            errors.append(f"{prefix}.format: unsupported format {fmt!r}")
        if Path(relative).suffix.lower() not in ({".csv"} if fmt == "csv" else {".md", ".markdown"}):
            errors.append(f"{prefix}.path: extension does not match format")
        tenant_id = _nonempty(document.get("tenant_id"), f"{prefix}.tenant_id", errors)
        shop_id = _nonempty(document.get("shop_id"), f"{prefix}.shop_id", errors)
        if shop_id not in tenant_shops.get(tenant_id, set()):
            errors.append(f"{prefix}: shop is not registered under tenant")
        disclosure = _nonempty(document.get("disclosure_class"), f"{prefix}.disclosure_class", errors)
        if disclosure not in ALLOWED_DISCLOSURE:
            errors.append(f"{prefix}.disclosure_class: unsupported value {disclosure!r}")
        source_type = _nonempty(document.get("source_type"), f"{prefix}.source_type", errors)
        if source_type != "synthetic":
            errors.append(f"{prefix}.source_type: must be synthetic")
        start = _date(document.get("effective_from"), f"{prefix}.effective_from", errors)
        end = _date(document.get("effective_to"), f"{prefix}.effective_to", errors)
        if start and end and end <= start:
            errors.append(f"{prefix}: effective_to must be after effective_from")
        target = root / relative
        try:
            target.relative_to(root)
        except ValueError:
            errors.append(f"{prefix}.path: escapes manifest directory")
            continue
        if not target.is_file():
            errors.append(f"{prefix}.path: file not found: {relative}")
            continue
        content = target.read_bytes()
        file_info = {
            "document_id": document_id,
            "path": relative,
            "format": fmt,
            "tenant_id": tenant_id,
            "shop_id": shop_id,
            "disclosure_class": disclosure,
            "effective_from": document.get("effective_from").isoformat() if isinstance(document.get("effective_from"), date) else document.get("effective_from"),
            "effective_to": document.get("effective_to").isoformat() if isinstance(document.get("effective_to"), date) else document.get("effective_to"),
            "sha256": sha256_bytes(content),
            "size_bytes": len(content),
        }
        files[document_id] = file_info
        for marker in ("synthetic", "provisional", "license"):
            if marker == "license":
                continue
            if marker not in manifest or manifest[marker] is not True:
                errors.append(f"{prefix}: inherits missing {marker}=true marker")
    if not document_ids.intersection({"syn-policy-a", "syn-faq-a"}):
        errors.append("documents: expected policy or FAQ document is missing")
    document_material = json.dumps({"manifest_sha256": sha256_bytes(raw_manifest), "files": files}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return ValidationResult(manifest, root, files, errors, sha256_bytes(raw_manifest), sha256_bytes(document_material))


def _state_root() -> Path:
    return Path(__file__).resolve().parents[3] / STATE_ROOT


def _state_path(dataset_id: str) -> Path:
    return _state_root() / f"{dataset_id}.json"


def import_dataset(result: ValidationResult) -> tuple[bool, str]:
    if not result.ok:
        return False, "validation failed; import refused"
    dataset_id = result.manifest["dataset_id"]
    path = _state_path(dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = None
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("manifest_sha256") == result.manifest_sha256 and existing.get("dataset_sha256") == result.dataset_sha256:
            return True, f"IDEMPOTENT dataset={dataset_id} manifest_sha256={result.manifest_sha256}"
    records: list[dict[str, Any]] = []
    for document_id, info in sorted(result.files.items()):
        content = (result.root / info["path"]).read_text(encoding="utf-8")
        if info["format"] == "csv":
            rows = list(csv.DictReader(content.splitlines()))
            for row_no, row in enumerate(rows, 2):
                text = " ".join(f"{key}: {value}" for key, value in row.items() if value)
                records.append({"document_id": document_id, "record_id": f"{document_id}:{row_no}", "text": text, **info})
        else:
            for number, block in enumerate(re.split(r"\n\s*\n", content), 1):
                block = block.strip()
                if block and not block.startswith("---"):
                    records.append({"document_id": document_id, "record_id": f"{document_id}:{number}", "text": block, **info})
    payload = {
        "dataset_id": dataset_id,
        "pipeline_version": result.manifest["pipeline_version"],
        "manifest_sha256": result.manifest_sha256,
        "dataset_sha256": result.dataset_sha256,
        "synthetic": True,
        "provisional": True,
        "license": "internal-generated",
        "record_count": len(records),
        "documents": result.files,
        "records": records,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    action = "UPDATED" if existing else "IMPORTED"
    return True, f"{action} dataset={dataset_id} records={len(records)} manifest_sha256={result.manifest_sha256}"


def status_dataset(dataset_id: str) -> tuple[bool, str]:
    path = _state_path(dataset_id)
    if not path.exists():
        return False, f"NOT_FOUND dataset={dataset_id} state={path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return True, json.dumps({
        "dataset_id": payload.get("dataset_id"),
        "pipeline_version": payload.get("pipeline_version"),
        "manifest_sha256": payload.get("manifest_sha256"),
        "dataset_sha256": payload.get("dataset_sha256"),
        "record_count": payload.get("record_count"),
        "synthetic": payload.get("synthetic"),
        "provisional": payload.get("provisional"),
        "license": payload.get("license"),
    }, ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingest")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "import"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--manifest", type=Path, required=True)
    status = sub.add_parser("status")
    status.add_argument("--dataset", required=True)
    args = parser.parse_args(argv)
    if args.command in {"validate", "import"}:
        result = validate_manifest(args.manifest)
        if not result.ok:
            print("FAIL")
            for error in result.errors:
                print(f"- {error}")
            return 1
        print(f"PASS dataset={result.manifest['dataset_id']} files={len(result.files)} manifest_sha256={result.manifest_sha256} dataset_sha256={result.dataset_sha256}")
        if args.command == "import":
            ok, message = import_dataset(result)
            print(message)
            return 0 if ok else 1
        return 0
    ok, message = status_dataset(args.dataset)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
