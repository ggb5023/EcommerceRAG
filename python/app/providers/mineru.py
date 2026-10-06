"""Minimal MinerU v4 REST client for isolated parsing experiments."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import posixpath
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from .config import ProviderConfigError, parse_env_file
from .contracts import ProviderError


@dataclass(frozen=True)
class MinerUConfig:
    endpoint: str
    token: str
    timeout_s: float = 30.0
    poll_interval_s: float = 3.0
    poll_timeout_s: float = 300.0


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HTTPTransport(Protocol):
    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      body: bytes | None, timeout_s: float,
                      max_bytes: int | None = None) -> HTTPResponse: ...


class UrllibTransport:
    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      body: bytes | None, timeout_s: float,
                      max_bytes: int | None = None) -> HTTPResponse:
        if max_bytes is not None and max_bytes <= 0:
            raise ProviderError("invalid_response", "MinerU response size limit is invalid", provider="mineru")

        def do_request() -> HTTPResponse:
            request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    response_headers = dict(response.headers.items())
                    if max_bytes is None:
                        response_body = response.read()
                    else:
                        content_length = response_headers.get("Content-Length")
                        try:
                            declared_length = int(content_length) if content_length is not None else None
                        except ValueError:
                            declared_length = None
                        if declared_length is not None and declared_length > max_bytes:
                            raise ProviderError("result_too_large", "MinerU result exceeds the size limit",
                                                provider="mineru")
                        parts: list[bytes] = []
                        total = 0
                        while True:
                            part = response.read(min(64 * 1024, max_bytes - total + 1))
                            if not part:
                                break
                            total += len(part)
                            if total > max_bytes:
                                raise ProviderError("result_too_large", "MinerU result exceeds the size limit",
                                                    provider="mineru")
                            parts.append(part)
                        response_body = b"".join(parts)
                    return HTTPResponse(response.status, response_headers, response_body)
            except urllib.error.HTTPError as error:
                return HTTPResponse(error.code, dict(error.headers.items()), error.read())
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise ProviderError("timeout", "MinerU unavailable", provider="mineru", retryable=True) from error
        return await asyncio.to_thread(do_request)


def load_mineru_config(path: str = "/etc/ecommerce-rag/providers.env") -> MinerUConfig:
    values = parse_env_file(path)
    token = values.get("MINERU_TOKEN", "").strip()
    if not token:
        raise ProviderConfigError("missing provider configuration: MINERU_TOKEN")
    endpoint = values.get("MINERU_ENDPOINT", "https://mineru.net/api/v4").strip().rstrip("/")
    try:
        timeout = float(values.get("MINERU_TIMEOUT_S", "30"))
        poll_interval = float(values.get("MINERU_POLL_INTERVAL_S", "3"))
        poll_timeout = float(values.get("MINERU_POLL_TIMEOUT_S", "300"))
    except ValueError as error:
        raise ProviderConfigError("MinerU timing configuration is invalid", code="CONFIG_FAIL") from error
    if timeout <= 0 or poll_interval <= 0 or poll_timeout <= 0:
        raise ProviderConfigError("MinerU timing configuration must be positive", code="CONFIG_FAIL")
    return MinerUConfig(endpoint, token, timeout, poll_interval, poll_timeout)


def _payload(response: HTTPResponse) -> Mapping[str, Any]:
    if response.status == 429:
        raise ProviderError("rate_limited", "MinerU quota exceeded", provider="mineru", status=429, retryable=True)
    if response.status >= 500:
        raise ProviderError("upstream_error", "MinerU service failed", provider="mineru", status=response.status, retryable=True)
    if response.status in {401, 403}:
        raise ProviderError("authentication", "MinerU authentication failed", provider="mineru", status=response.status)
    if response.status < 200 or response.status >= 300:
        raise ProviderError("http_error", "MinerU rejected the request", provider="mineru", status=response.status)
    try:
        value = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("invalid_response", "MinerU returned invalid JSON", provider="mineru") from error
    if not isinstance(value, Mapping):
        raise ProviderError("invalid_response", "MinerU response is not an object", provider="mineru")
    return value


class MinerUClient:
    def __init__(self, config: MinerUConfig, transport: HTTPTransport | None = None):
        self.config = config
        self.transport = transport or UrllibTransport()

    async def submit_url(self, url: str, *, data_id: str, model_version: str = "vlm") -> str:
        body = json.dumps({"url": url, "data_id": data_id, "model_version": model_version,
                           "is_ocr": True, "enable_formula": True, "enable_table": True},
                          separators=(",", ":")).encode()
        response = await self.transport.request("POST", self.config.endpoint + "/extract/task",
            headers={"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json"},
            body=body, timeout_s=self.config.timeout_s)
        payload = _payload(response)
        data = payload.get("data", payload)
        task_id = data.get("task_id") if isinstance(data, Mapping) else None
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError("invalid_response", "MinerU task id is missing", provider="mineru")
        return task_id

    async def status(self, task_id: str) -> Mapping[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ProviderError("invalid_request", "MinerU task id is invalid", provider="mineru")
        response = await self.transport.request("GET", self.config.endpoint + "/extract/task/" + quote(task_id, safe=""),
            headers={"Authorization": f"Bearer {self.config.token}"}, body=None, timeout_s=self.config.timeout_s)
        return _payload(response)

    async def download_result(self, url: str, *, max_bytes: int = 256 * 1024 * 1024) -> bytes:
        """Download a completed result without exposing credentials to the result host."""
        try:
            parsed = urlsplit(url)
            valid_url = (parsed.scheme == "https" and bool(parsed.hostname) and
                         parsed.username is None and parsed.password is None and not parsed.fragment)
        except (TypeError, ValueError):
            valid_url = False
        if not valid_url or max_bytes <= 0:
            raise ProviderError("invalid_response", "MinerU result URL is invalid", provider="mineru")
        response = await self.transport.request(
            "GET", url, headers={}, body=None, timeout_s=self.config.timeout_s, max_bytes=max_bytes
        )
        if response.status < 200 or response.status >= 300:
            raise ProviderError("result_download_failed", "MinerU result download failed",
                                provider="mineru", status=response.status, retryable=response.status >= 500)
        if len(response.body) > max_bytes:
            raise ProviderError("result_too_large", "MinerU result exceeds the size limit", provider="mineru")
        return response.body

    async def wait(self, task_id: str) -> Mapping[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.config.poll_timeout_s
        while True:
            payload = await self.status(task_id)
            data = payload.get("data", payload)
            state = str(data.get("state", data.get("status", ""))).lower() if isinstance(data, Mapping) else ""
            if state in {"done", "success", "completed", "succeeded"}:
                return payload
            if state in {"failed", "error", "cancelled", "canceled"}:
                raise ProviderError("parse_failed", "MinerU task failed", provider="mineru")
            if asyncio.get_running_loop().time() >= deadline:
                raise ProviderError("timeout", "MinerU task polling timed out", provider="mineru", retryable=True)
            await asyncio.sleep(self.config.poll_interval_s)


def result_url(payload: Mapping[str, Any]) -> str | None:
    """Extract a provider result URL without serializing the provider payload."""
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        return None
    for key in ("full_zip_url", "zip_url", "result_url"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                parsed = urlsplit(value)
                if parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.fragment:
                    return value
            except (TypeError, ValueError):
                pass
    return None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_extract_zip(raw: bytes, destination: Path, *, max_members: int = 10000,
                     max_uncompressed: int = 512 * 1024 * 1024) -> list[Path]:
    """Atomically extract a result archive after validating member paths."""
    if max_members <= 0 or max_uncompressed <= 0:
        raise ValueError("archive limits must be positive")
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination_parent = destination.parent.resolve()
    destination = destination_parent / destination.name
    if destination.exists():
        raise ProviderError("artifact_destination_exists", "MinerU artifact destination already exists",
                            provider="mineru")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as error:
        raise ProviderError("invalid_result_archive", "MinerU result archive is invalid", provider="mineru") from error
    temporary: Path | None = None
    try:
        with archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise ProviderError("result_archive_too_large", "MinerU result has too many files", provider="mineru")
            validated: list[tuple[zipfile.ZipInfo, str]] = []
            seen: set[str] = set()
            file_names: set[str] = set()
            total = 0
            for member in members:
                name = member.filename.replace("\\", "/")
                normalized = posixpath.normpath(name).rstrip("/")
                if not name or normalized in {".", ""}:
                    continue
                raw_parts = name.split("/")
                if ("\x00" in name or any(part in {".", ".."} for part in raw_parts)
                        or normalized.startswith("../") or normalized == ".." or name.startswith("/")):
                    raise ProviderError("unsafe_result_archive", "MinerU result contains an unsafe path", provider="mineru")
                mode = (member.external_attr >> 16) & 0o170000
                is_dir = member.is_dir() or mode == stat.S_IFDIR
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ProviderError("unsafe_result_archive", "MinerU result contains a special file", provider="mineru")
                if normalized in seen:
                    raise ProviderError("unsafe_result_archive", "MinerU result contains duplicate paths", provider="mineru")
                seen.add(normalized)
                if not is_dir:
                    file_names.add(normalized)
                if member.file_size < 0 or member.compress_size < 0:
                    raise ProviderError("invalid_result_archive", "MinerU archive metadata is invalid", provider="mineru")
                total += member.file_size
                if total > max_uncompressed:
                    raise ProviderError("result_archive_too_large", "MinerU result expands beyond the size limit", provider="mineru")
                validated.append((member, normalized))
            for _, name in validated:
                parts = name.split("/")
                if any("/".join(parts[:index]) in file_names for index in range(1, len(parts))):
                    raise ProviderError("unsafe_result_archive", "MinerU result has conflicting paths", provider="mineru")
            temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination_parent))
            temporary.chmod(0o700)
            extracted_relative: list[Path] = []
            written_total = 0
            for member, name in validated:
                target = temporary / name
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                written = 0
                with archive.open(member) as source, target.open("xb") as output:
                    while block := source.read(1024 * 1024):
                        written += len(block)
                        written_total += len(block)
                        if written > member.file_size or written_total > max_uncompressed:
                            raise ProviderError("result_archive_too_large", "MinerU result expands beyond the size limit", provider="mineru")
                        output.write(block)
                if written != member.file_size:
                    raise ProviderError("invalid_result_archive", "MinerU archive is truncated", provider="mineru")
                target.chmod(0o600)
                extracted_relative.append(Path(name))
        if destination.exists():
            raise ProviderError("artifact_destination_exists", "MinerU artifact destination already exists", provider="mineru")
        temporary.rename(destination)
        return [destination / relative for relative in extracted_relative]
    except ProviderError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as error:
        raise ProviderError("invalid_result_archive", "MinerU result archive could not be extracted", provider="mineru") from error
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def normalize_content_list(content: Any, *, document_id: str, version_id: str) -> list[dict[str, Any]]:
    """Map common MinerU content-list fields to the internal element contract."""
    if not isinstance(content, list):
        raise TypeError("content_list must be an array")
    allowed = {"text", "heading", "table", "image", "list", "code"}
    result: list[dict[str, Any]] = []
    for index, item in enumerate(content):
        if not isinstance(item, Mapping):
            raise TypeError("content_list element must be an object")
        raw_type = str(item.get("type", item.get("category", "text"))).lower()
        text_level = item.get("text_level")
        if raw_type == "text" and isinstance(text_level, int) and text_level > 0:
            raw_type = "heading"
        element_type = raw_type if raw_type in allowed else "text"
        warnings: list[str] = []
        if raw_type not in allowed:
            warnings.append("unknown_type:" + raw_type)
        raw_page = item.get("page_no")
        if raw_page is None:
            raw_page = item.get("page_idx")
            if isinstance(raw_page, int):
                raw_page += 1
        if raw_page is not None and (not isinstance(raw_page, int) or raw_page < 1):
            warnings.append("invalid_page_no")
            raw_page = None
        heading_path = item.get("heading_path", [])
        if not isinstance(heading_path, list):
            heading_path = [str(heading_path)]
        image_refs = item.get("image_refs")
        if image_refs is None:
            image_ref = item.get("img_path", item.get("image_path"))
            image_refs = [image_ref] if isinstance(image_ref, str) and image_ref else []
        elif not isinstance(image_refs, list):
            image_refs = [image_refs]
        text = item.get("text", item.get("content", "")) or ""
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False, sort_keys=True)
        result.append({
            "document_id": document_id, "document_version_id": version_id,
            "type": element_type, "text": text,
            "text_level": text_level, "page_no": raw_page,
            "heading_path": heading_path, "table_body": item.get("table_body", item.get("table")),
            "table_caption": item.get("table_caption"), "image_refs": image_refs,
            "bbox": item.get("bbox", item.get("coordinates")),
            "source_position": {"element_index": index},
            "warning": ";".join(warnings) if warnings else None,
        })
    return result
