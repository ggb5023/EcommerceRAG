"""Bounded, allow-listed web snapshot fetcher.

The crawler is an isolated source-ingestion utility. It is never used by the
M1 customer response path and stores only restricted snapshots plus metadata.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDIRECT_CODES = {301, 302, 303, 307, 308}
DEFAULT_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class CrawlError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourcePolicy:
    source_id: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...] = ("/",)
    robots_required: bool = True
    max_bytes: int = 2_000_000
    timeout_s: float = 5.0
    max_redirects: int = 3
    retries: int = 0
    purpose: str = "controlled_reference_snapshot"
    authorization: str = "pending"
    owner: str = "engineering"
    retention_days: int | None = None
    policy_version: str = "source-policy-v1"
    allowed_content_types: tuple[str, ...] = DEFAULT_CONTENT_TYPES

    def __post_init__(self) -> None:
        if not _COMPONENT.fullmatch(self.source_id):
            raise ValueError("source_id must be a safe path component")
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        if not self.allowed_paths:
            raise ValueError("allowed_paths must not be empty")
        if self.max_bytes <= 0 or self.timeout_s <= 0:
            raise ValueError("max_bytes and timeout_s must be positive")
        if self.max_redirects < 0 or self.retries < 0:
            raise ValueError("max_redirects and retries must not be negative")
        if self.retention_days is not None and self.retention_days < 0:
            raise ValueError("retention_days must not be negative")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose redirects to the caller so every target is revalidated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def normalize_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise CrawlError("invalid_url")
    path = urllib.parse.urljoin("/", parsed.path or "/")
    query = urllib.parse.urlencode(
        [
            (key, val)
            for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith(("utm_", "fbclid"))
        ],
        doseq=True,
    )
    host = parsed.hostname.lower().rstrip(".")
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, path, query, ""))


def _blocked_address(value: str) -> bool:
    try:
        # IPv6 zone identifiers are not part of the address literal.
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    # is_global is deliberately stricter than is_private: it also rejects
    # loopback, link-local, multicast, documentation, reserved and metadata
    # ranges such as 169.254.169.254.
    return not address.is_global


def _resolved_addresses(host: str, port: int | None) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise CrawlError("dns_failed") from error
    addresses = tuple(sorted({str(info[4][0]) for info in infos if info[4]}))
    if not addresses:
        raise CrawlError("dns_failed")
    return addresses


def _private(host: str) -> bool:
    """Return whether a literal or DNS-resolved host is non-public."""
    try:
        return _blocked_address(host)
    except (CrawlError, ValueError):
        return any(_blocked_address(address) for address in _resolved_addresses(host, None))


def _validate_dns(host: str, port: int | None, *, allow_private: bool) -> tuple[str, ...]:
    addresses = _resolved_addresses(host, port)
    if not allow_private and any(_blocked_address(address) for address in addresses):
        raise CrawlError("private_address")
    return addresses


def validate_url(url: str, policy: SourcePolicy, *, allow_private: bool = False) -> str:
    normalized = normalize_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {value.lower().rstrip(".") for value in policy.allowed_hosts}
    netloc = parsed.netloc.lower()
    if host not in allowed and netloc not in allowed:
        raise CrawlError("host_not_allowed")
    if parsed.port not in (None, 80, 443) and not allow_private:
        raise CrawlError("port_not_allowed")
    if not any(
        parsed.path == allowed_path
        or parsed.path.startswith(allowed_path.rstrip("/") + "/")
        for allowed_path in policy.allowed_paths
    ):
        raise CrawlError("path_not_allowed")
    if not allow_private:
        _validate_dns(host, parsed.port or (443 if parsed.scheme == "https" else 80), allow_private=False)
    return normalized


def _cancelled(event: Any | None) -> bool:
    if event is None:
        return False
    checker = getattr(event, "is_set", None)
    if callable(checker):
        return bool(checker())
    if callable(event):
        return bool(event())
    return False


def _check_cancelled(event: Any | None) -> None:
    if _cancelled(event):
        raise CrawlError("cancelled")


def _robots_allows(
    url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    allow_private: bool = False,
    cancel_event: Any | None = None,
) -> bool:
    parsed = urllib.parse.urlsplit(url)
    robots = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    if not allow_private:
        _validate_dns(parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80), allow_private=False)
    _check_cancelled(cancel_event)
    request = urllib.request.Request(robots, headers={"User-Agent": "EcommerceRAG/controlled-snapshot-v1"}, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            _validate_peer(response, allow_private=allow_private)
            if response.status == 404:
                return True
            if response.status < 200 or response.status >= 300:
                return False
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        return False
                except ValueError:
                    return False
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                return False
            text = body.decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code == 404
    except (OSError, urllib.error.URLError):
        return False
    _check_cancelled(cancel_event)
    applies = False
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            applies = value in {"*", "ecommerce-rag"}
        elif key == "disallow" and applies and value and urllib.parse.urlsplit(url).path.startswith(value):
            return False
    return True


def _read_limited(response: Any, max_bytes: int, cancel_event: Any | None) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise CrawlError("response_too_large")
        except ValueError as error:
            raise CrawlError("invalid_content_length") from error
    content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise CrawlError("content_encoding_not_allowed")
    parts: list[bytes] = []
    total = 0
    while True:
        _check_cancelled(cancel_event)
        block = response.read(min(64 * 1024, max_bytes - total + 1))
        if not block:
            break
        total += len(block)
        if total > max_bytes:
            raise CrawlError("response_too_large")
        parts.append(block)
    return b"".join(parts)


def _peer_address(response: Any) -> str | None:
    try:
        raw = response.fp.raw
        sock = raw._sock
        return str(sock.getpeername()[0])
    except (AttributeError, OSError, TypeError, IndexError):
        return None


def _validate_peer(response: Any, *, allow_private: bool) -> str | None:
    """Fail closed when the connected peer cannot be inspected."""
    peer = _peer_address(response)
    if allow_private:
        return peer
    if peer is None:
        raise CrawlError("peer_address_unavailable")
    if _blocked_address(peer):
        raise CrawlError("private_address")
    return peer


def _atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if isinstance(data, bytes):
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_once(
    url: str,
    policy: SourcePolicy,
    root: Path,
    *,
    allow_private: bool,
    cancel_event: Any | None,
) -> dict[str, object]:
    current = validate_url(url, policy, allow_private=allow_private)
    visited = {current}
    body: bytes | None = None
    content_type = ""
    response_headers: Any = {}
    final_url = current
    for redirect_index in range(policy.max_redirects + 1):
        _check_cancelled(cancel_event)
        if policy.robots_required and not _robots_allows(
            current,
            timeout_s=policy.timeout_s,
            max_bytes=min(policy.max_bytes, 128_000),
            allow_private=allow_private,
            cancel_event=cancel_event,
        ):
            raise CrawlError("robots_denied")
        parsed = urllib.parse.urlsplit(current)
        _validate_dns(
            parsed.hostname or "",
            parsed.port or (443 if parsed.scheme == "https" else 80),
            allow_private=allow_private,
        )
        request = urllib.request.Request(
            current,
            headers={
                "User-Agent": "EcommerceRAG/controlled-snapshot-v1",
                "Accept-Encoding": "identity",
            },
            method="GET",
        )
        try:
            response = _OPENER.open(request, timeout=policy.timeout_s)
        except urllib.error.HTTPError as error:
            if error.code not in REDIRECT_CODES:
                raise CrawlError(f"http_{error.code}") from error
            location = error.headers.get("Location")
            error.close()
            if not location:
                raise CrawlError("redirect_location_missing")
            if redirect_index >= policy.max_redirects:
                raise CrawlError("redirect_limit")
            next_url = validate_url(urllib.parse.urljoin(current, location), policy, allow_private=allow_private)
            if next_url in visited:
                raise CrawlError("redirect_loop")
            visited.add(next_url)
            current = next_url
            continue
        except TimeoutError as error:
            raise CrawlError("timeout") from error
        except urllib.error.URLError as error:
            raise CrawlError("fetch_failed") from error
        try:
            _validate_peer(response, allow_private=allow_private)
            if response.status < 200 or response.status >= 300:
                raise CrawlError(f"http_{response.status}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in set(policy.allowed_content_types):
                raise CrawlError("content_type_not_allowed")
            response_headers = response.headers
            body = _read_limited(response, policy.max_bytes, cancel_event)
            final_url = validate_url(response.geturl(), policy, allow_private=allow_private)
            parsed_final = urllib.parse.urlsplit(final_url)
            _validate_dns(
                parsed_final.hostname or "",
                parsed_final.port or (443 if parsed_final.scheme == "https" else 80),
                allow_private=allow_private,
            )
        finally:
            response.close()
        break
    if body is None:
        raise CrawlError("empty_response")
    text = re.sub(
        r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>",
        " ",
        body.decode("utf-8", "replace"),
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise CrawlError("empty_content")
    content_hash = hashlib.sha256(body).hexdigest()
    object_dir = root / policy.source_id / content_hash
    object_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    object_dir.chmod(0o700)
    raw_path = object_dir / "raw.html"
    manifest_path = object_dir / "manifest.json"
    if raw_path.exists() and manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CrawlError("snapshot_conflict") from error
        if existing.get("sha256") == content_hash and existing.get("size_bytes") == len(body):
            return existing
        raise CrawlError("snapshot_conflict")
    _atomic_write(raw_path, body)
    manifest = {
        "source_id": policy.source_id,
        "url": normalize_url(url),
        "final_url": final_url,
        "canonical_url": final_url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "status_code": 200,
        "content_type": content_type,
        "size_bytes": len(body),
        "sha256": content_hash,
        "snapshot_version": f"sha256-{content_hash}",
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "etag": response_headers.get("ETag"),
        "last_modified": response_headers.get("Last-Modified"),
        "snapshot_path": str(raw_path),
        "purpose": policy.purpose,
        "authorization": policy.authorization,
        "owner": policy.owner,
        "retention_days": policy.retention_days,
        "policy_version": policy.policy_version,
        "robots_required": policy.robots_required,
        "real_service_acceptance": False,
    }
    _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def fetch_snapshot(
    url: str,
    policy: SourcePolicy,
    root: Path,
    *,
    allow_private: bool = False,
    cancel_event: Any | None = None,
) -> dict[str, object]:
    """Fetch one allow-listed snapshot with bounded retries and atomic output."""
    last_error: CrawlError | None = None
    for attempt in range(policy.retries + 1):
        _check_cancelled(cancel_event)
        try:
            return _fetch_once(
                url,
                policy,
                root,
                allow_private=allow_private,
                cancel_event=cancel_event,
            )
        except CrawlError as error:
            last_error = error
            if error.code not in {"fetch_failed", "timeout", "http_408", "http_429"} or attempt >= policy.retries:
                raise
            time.sleep(min(0.25 * (2**attempt), 2.0))
    assert last_error is not None
    raise last_error
