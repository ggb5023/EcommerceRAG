"""Configuration loading and validation for model providers.

Provider configuration is intentionally file-only.  In particular, this
module never merges ``os.environ`` into the provider configuration; that
prevents a shell environment or a process manager argument from silently
overriding the reviewed configuration in ``/etc/ecommerce-rag``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_PATH = Path("/etc/ecommerce-rag/providers.env")
PROFILES = frozenset({"mock", "aliyun-bailian"})


class ProviderConfigError(ValueError):
    """A non-sensitive configuration error suitable for operator output."""

    def __init__(self, message: str, *, code: str = "CONFIG_BLOCKED") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderConfig:
    profile: str
    endpoint: str
    region: str
    control_model: str
    embedding_model: str
    rerank_model: str
    generation_model: str
    embedding_dimensions: int
    timeout_s: float
    quota_rpm: int
    api_key: str | None = None
    workspace_id: str | None = None

    @property
    def models(self) -> dict[str, str]:
        return {
            "control": self.control_model,
            "embedding": self.embedding_model,
            "rerank": self.rerank_model,
            "generation": self.generation_model,
        }


_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def parse_env_file(path: str | Path = CONFIG_PATH) -> dict[str, str]:
    """Parse a small dotenv subset without expanding variables or secrets."""

    config_path = Path(path)
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProviderConfigError("provider configuration file is unavailable") from exc
    values: dict[str, str] = {}
    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProviderConfigError(
                f"invalid provider configuration line {line_no}", code="CONFIG_FAIL"
            )
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not _KEY_RE.fullmatch(key):
            raise ProviderConfigError(
                f"invalid provider configuration key at line {line_no}",
                code="CONFIG_FAIL",
            )
        if key in values:
            raise ProviderConfigError(
                f"duplicate provider configuration key {key}", code="CONFIG_FAIL"
            )
        # Quotes are accepted for normal dotenv ergonomics, but no expansion
        # or command substitution is performed.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _value(
    values: dict[str, str],
    *keys: str,
    required: bool = True,
    default: str | None = None,
) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and value.strip():
            return value.strip()
    if required:
        raise ProviderConfigError(f"missing provider configuration: {keys[0]}")
    return default


def _positive_int(
    values: dict[str, str], field: str, *keys: str, default: int | None = None
) -> int:
    raw = _value(
        values,
        *keys,
        required=default is None,
        default=None if default is None else str(default),
    )
    try:
        result = int(raw or "")
    except ValueError as exc:
        raise ProviderConfigError(f"{field} must be a positive integer") from exc
    if result <= 0:
        raise ProviderConfigError(f"{field} must be a positive integer")
    return result


def _positive_float(
    values: dict[str, str], field: str, *keys: str, default: float | None = None
) -> float:
    raw = _value(
        values,
        *keys,
        required=default is None,
        default=None if default is None else str(default),
    )
    try:
        result = float(raw or "")
    except ValueError as exc:
        raise ProviderConfigError(f"{field} must be a positive number") from exc
    if result <= 0:
        raise ProviderConfigError(f"{field} must be a positive number")
    return result


def validate_values(values: dict[str, str]) -> ProviderConfig:
    """Validate a parsed file and return a typed, secret-bearing config.

    The returned API key is only held in memory by the adapter.  Callers must
    never serialize or print this object.
    """

    profile = (_value(values, "PROVIDER_PROFILE", "PROFILE") or "").lower()
    if profile not in PROFILES:
        raise ProviderConfigError("PROVIDER_PROFILE must be mock or aliyun-bailian")

    # A mock profile still carries explicit slot IDs so that tests catch an
    # accidental model/profile mismatch.  Its endpoint and key are unused.
    endpoint = (
        _value(
            values,
            "BAILIAN_ENDPOINT",
            "DASHSCOPE_BASE_URL",
            required=profile == "aliyun-bailian",
            default="http://mock.invalid",
        )
        or "http://mock.invalid"
    )
    if profile == "aliyun-bailian":
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderConfigError("endpoint must be an http(s) URL")
    region = (
        _value(
            values,
            "BAILIAN_REGION",
            "REGION",
            required=profile == "aliyun-bailian",
            default="mock",
        )
        or "mock"
    )
    models = {
        "control": _value(values, "CONTROL_MODEL"),
        "embedding": _value(values, "EMBEDDING_MODEL"),
        "rerank": _value(values, "RERANK_MODEL"),
        "generation": _value(values, "GENERATION_MODEL"),
    }
    dimensions = _positive_int(
        values, "EMBEDDING_DIMENSIONS", "EMBEDDING_DIMENSIONS", default=1024
    )
    if dimensions != 1024:
        raise ProviderConfigError("EMBEDDING_DIMENSIONS must be 1024")
    timeout_s = _positive_float(
        values, "PROVIDER_TIMEOUT_S", "PROVIDER_TIMEOUT_S", "TIMEOUT_S", default=60.0
    )
    quota_rpm = _positive_int(
        values, "PROVIDER_QUOTA_RPM", "PROVIDER_QUOTA_RPM", "QUOTA_RPM", default=60
    )
    api_key = _value(
        values,
        "DASHSCOPE_API_KEY",
        "BAILIAN_API_KEY",
        required=profile == "aliyun-bailian",
    )
    workspace_id = _value(
        values, "BAILIAN_WORKSPACE_ID", "WORKSPACE_ID", required=False
    )
    return ProviderConfig(
        profile=profile,
        endpoint=endpoint.rstrip("/"),
        region=region,
        control_model=models["control"] or "",
        embedding_model=models["embedding"] or "",
        rerank_model=models["rerank"] or "",
        generation_model=models["generation"] or "",
        embedding_dimensions=dimensions,
        timeout_s=timeout_s,
        quota_rpm=quota_rpm,
        api_key=api_key,
        workspace_id=workspace_id,
    )


def load_provider_config(path: str | Path = CONFIG_PATH) -> ProviderConfig:
    return validate_values(parse_env_file(path))
