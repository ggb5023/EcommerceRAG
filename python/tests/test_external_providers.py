import asyncio
import io
import json
import stat
import zipfile
from typing import ClassVar

from app.providers import mineru as mineru_module
from app.providers.mineru import (
    HTTPResponse,
    MinerUClient,
    MinerUConfig,
    normalize_content_list,
    safe_extract_zip,
    sha256_bytes,
)
from app.providers.tavily import HTTPResponse as TavilyHTTPResponse
from app.providers.tavily import TavilyConfig, TavilyProvider


class FakeMinerTransport:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, *, headers, body, timeout_s, max_bytes=None):
        self.calls.append((method, url, body))
        if method == "POST":
            return HTTPResponse(200, {}, b'{"data":{"task_id":"task-1"}}')
        return HTTPResponse(200, {}, b'{"data":{"state":"done","full_zip_url":"https://example.invalid/result.zip"}}')


class FakeTavilyTransport:
    async def request(self, method, url, *, headers, body, timeout_s, cancel_event=None):
        payload = json.loads(body)
        assert payload["include_answer"] is False
        assert payload["include_raw_content"] is False
        return TavilyHTTPResponse(200, {"x-request-id": "req-1"}, json.dumps({
            "results": [{"url": "https://example.com/a", "title": "A", "content": "snippet", "score": 0.8}],
            "usage": {"credits": 1},
        }).encode())


def test_mineru_submit_and_wait():
    transport = FakeMinerTransport()
    client = MinerUClient(MinerUConfig("https://mineru.test", "secret", poll_interval_s=0.001), transport)
    result = asyncio.run(client.wait(asyncio.run(client.submit_url("https://example.com/a.pdf", data_id="d1"))))
    assert result["data"]["state"] == "done"
    assert transport.calls[0][0] == "POST"


def test_mineru_result_download_and_safe_extract(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("content_list.json", "[]")
    raw = payload.getvalue()

    class ResultTransport(FakeMinerTransport):
        async def request(self, method, url, *, headers, body, timeout_s, max_bytes=None):
            if method == "GET" and url.endswith("result.zip"):
                return HTTPResponse(200, {}, raw)
            return await super().request(method, url, headers=headers, body=body, timeout_s=timeout_s,
                                         max_bytes=max_bytes)

    client = MinerUClient(MinerUConfig("https://mineru.test", "secret"), ResultTransport())
    downloaded = asyncio.run(client.download_result("https://results.test/result.zip"))
    assert sha256_bytes(downloaded) == sha256_bytes(raw)
    paths = safe_extract_zip(downloaded, tmp_path / "parsed")
    assert paths == [tmp_path / "parsed" / "content_list.json"]


def test_mineru_result_download_rejects_unsafe_urls():
    client = MinerUClient(MinerUConfig("https://mineru.test", "secret"))
    import pytest
    for url in ("http://results.test/result.zip", "https://user:secret@results.test/result.zip",
                "https://results.test/result.zip#fragment", "not-a-url"):
        with pytest.raises(Exception) as raised:
            asyncio.run(client.download_result(url))
        assert getattr(raised.value, "code", None) == "invalid_response"


def test_mineru_urllib_transport_enforces_streaming_limit(monkeypatch):
    class Response:
        status = 200
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/zip"}

        def __init__(self):
            self.parts = iter((b"1234", b"56"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return next(self.parts, b"")

    monkeypatch.setattr(mineru_module.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    import pytest
    with pytest.raises(Exception) as raised:
        asyncio.run(mineru_module.UrllibTransport().request(
            "GET", "https://results.test/result.zip", headers={}, body=None,
            timeout_s=1, max_bytes=5
        ))
    assert getattr(raised.value, "code", None) == "result_too_large"


def test_mineru_urllib_transport_rejects_declared_oversize_before_read(monkeypatch):
    class Response:
        status = 200
        headers: ClassVar[dict[str, str]] = {"Content-Length": "10"}
        read_called = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            self.read_called = True
            return b"too much"

    response = Response()
    monkeypatch.setattr(mineru_module.urllib.request, "urlopen", lambda *args, **kwargs: response)
    import pytest
    with pytest.raises(Exception) as raised:
        asyncio.run(mineru_module.UrllibTransport().request(
            "GET", "https://results.test/result.zip", headers={}, body=None,
            timeout_s=1, max_bytes=5
        ))
    assert getattr(raised.value, "code", None) == "result_too_large"
    assert response.read_called is False


def test_mineru_rejects_zip_traversal(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    import pytest
    with pytest.raises(Exception) as raised:
        safe_extract_zip(payload.getvalue(), tmp_path / "parsed")
    assert getattr(raised.value, "code", None) == "unsafe_result_archive"


def test_mineru_rejects_normalized_dotdot_path(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("nested/../escape.txt", "bad")
    import pytest
    with pytest.raises(Exception) as raised:
        safe_extract_zip(payload.getvalue(), tmp_path / "parsed")
    assert getattr(raised.value, "code", None) == "unsafe_result_archive"


def test_mineru_rejects_symlink_and_cleans_destination(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        member = zipfile.ZipInfo("link")
        member.external_attr = stat.S_IFLNK << 16
        archive.writestr(member, "outside")
    import pytest
    destination = tmp_path / "parsed"
    with pytest.raises(Exception) as raised:
        safe_extract_zip(payload.getvalue(), destination)
    assert getattr(raised.value, "code", None) == "unsafe_result_archive"
    assert not destination.exists()
    assert not list(tmp_path.glob(".parsed.*"))


def test_mineru_rejects_archive_size_and_conflicting_paths(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("directory", "file")
        archive.writestr("directory/child.txt", "child")
    import pytest
    with pytest.raises(Exception) as raised:
        safe_extract_zip(payload.getvalue(), tmp_path / "parsed", max_uncompressed=1024)
    assert getattr(raised.value, "code", None) == "unsafe_result_archive"

    oversized = io.BytesIO()
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("large.txt", "0123456789")
    with pytest.raises(Exception) as raised:
        safe_extract_zip(oversized.getvalue(), tmp_path / "large", max_uncompressed=5)
    assert getattr(raised.value, "code", None) == "result_archive_too_large"
    assert not (tmp_path / "large").exists()


def test_mineru_content_list_normalization_warns_on_unknown_type():
    result = normalize_content_list([{"type": "mystery", "text": "x", "page_idx": 2}],
                                    document_id="doc", version_id="v1")
    assert result[0]["type"] == "text"
    assert result[0]["warning"] == "unknown_type:mystery"


def test_mineru_content_list_normalizes_page_heading_table_and_image_fields():
    result = normalize_content_list([
        {"type": "text", "text_level": 2, "text": "标题", "page_idx": 0},
        {"type": "table", "table": [["A", "B"]], "page_no": 2},
        {"type": "image", "image_path": "images/1.png", "page_no": 0},
    ], document_id="doc", version_id="v1")
    assert result[0]["type"] == "heading"
    assert result[0]["page_no"] == 1
    assert result[1]["table_body"] == [["A", "B"]]
    assert result[2]["image_refs"] == ["images/1.png"]
    assert result[2]["page_no"] is None
    assert result[2]["warning"] == "invalid_page_no"


def test_tavily_result_is_normalized():
    provider = TavilyProvider(TavilyConfig("https://tavily.test/search", "secret", 2, 5), FakeTavilyTransport())
    result = asyncio.run(provider.search("public test"))
    assert result.status == "success_usable"
    assert result.request_id == "req-1"
    assert result.results[0].content == "snippet"
