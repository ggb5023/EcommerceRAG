import http.server
import json
import stat
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.web import crawler
from app.web.crawler import CrawlError, SourcePolicy, fetch_snapshot


class CrawlHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /blocked\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        elif self.path == "/redirect-external":
            self.send_response(302)
            self.send_header("Location", "http://example.com/out")
            self.end_headers()
            return
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", f"http://{self.headers['Host']}/redirect-loop")
            self.end_headers()
            return
        elif self.path.startswith("/chain/"):
            number = int(self.path.rsplit("/", 1)[-1])
            self.send_response(302)
            self.send_header("Location", f"http://{self.headers['Host']}/chain/{number + 1}")
            self.end_headers()
            return
        elif self.path == "/wrong-mime":
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/compressed":
            body = b"compressed"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Encoding", "gzip")
        elif self.path == "/large-header":
            body = b"small"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "1000")
        elif self.path == "/large-stream":
            body = b"x" * 101
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"<html><title>fixture</title><body>official snapshot</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("ETag", '"fixture-v1"')
            self.send_header("Last-Modified", "Thu, 01 Oct 2026 00:00:00 GMT")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class FakeResponse:
    status = 200

    def __init__(self, url: str):
        self.url = url
        self.headers = {"Content-Type": "text/plain"}

    def read(self, _size=-1):
        return b"fake response"

    def geturl(self):
        return self.url

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CrawlHandler)
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def policy(self):
        return SourcePolicy(
            source_id="local",
            allowed_hosts=(self.host,),
            allowed_paths=("/",),
            robots_required=True,
            max_bytes=10000,
            timeout_s=2,
        )

    def test_snapshot_and_metadata(self):
        with TemporaryDirectory() as directory:
            result = fetch_snapshot(f"http://{self.host}/ok", self.policy(), Path(directory), allow_private=True)
            self.assertEqual(result["status_code"], 200)
            snapshot = Path(result["snapshot_path"])
            self.assertTrue(snapshot.exists())
            self.assertEqual(result["canonical_url"], f"http://{self.host}/ok")
            self.assertEqual(result["etag"], '"fixture-v1"')
            self.assertEqual(result["last_modified"], "Thu, 01 Oct 2026 00:00:00 GMT")
            self.assertEqual(result["snapshot_version"], f"sha256-{result['sha256']}")
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
            manifest = snapshot.with_name("manifest.json")
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
            self.assertEqual(json.loads(manifest.read_text()), result)

    def test_robots_denied(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(CrawlError) as error:
                fetch_snapshot(f"http://{self.host}/blocked/path", self.policy(), Path(directory), allow_private=True)
            self.assertEqual(error.exception.code, "robots_denied")

    def test_private_literal_is_rejected_before_request(self):
        policy = SourcePolicy("private", ("127.0.0.1",), robots_required=False)
        with self.assertRaises(CrawlError) as error:
            crawler.validate_url("http://127.0.0.1/", policy)
        self.assertEqual(error.exception.code, "private_address")

    def test_redirect_to_unlisted_host_is_rejected(self):
        policy = self.policy()
        with TemporaryDirectory() as directory, self.assertRaises(CrawlError) as error:
            fetch_snapshot(
                f"http://{self.host}/redirect-external",
                policy,
                Path(directory),
                allow_private=True,
            )
        self.assertEqual(error.exception.code, "host_not_allowed")

    def test_redirect_loop_and_redirect_limit_are_distinct(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(CrawlError) as loop_error:
                fetch_snapshot(
                    f"http://{self.host}/redirect-loop",
                    self.policy(),
                    Path(directory),
                    allow_private=True,
                )
            self.assertEqual(loop_error.exception.code, "redirect_loop")

            limited = SourcePolicy(
                source_id="local-chain",
                allowed_hosts=(self.host,),
                allowed_paths=("/chain",),
                robots_required=False,
                max_redirects=1,
            )
            with self.assertRaises(CrawlError) as limit_error:
                fetch_snapshot(
                    f"http://{self.host}/chain/0",
                    limited,
                    Path(directory),
                    allow_private=True,
                )
            self.assertEqual(limit_error.exception.code, "redirect_limit")

    def test_mime_encoding_and_size_limits(self):
        for path, code in (("/wrong-mime", "content_type_not_allowed"), ("/compressed", "content_encoding_not_allowed")):
            with self.subTest(path=path), TemporaryDirectory() as directory:
                with self.assertRaises(CrawlError) as error:
                    fetch_snapshot(
                        f"http://{self.host}{path}",
                        self.policy(),
                        Path(directory),
                        allow_private=True,
                    )
                self.assertEqual(error.exception.code, code)

        small = SourcePolicy(
            source_id="small",
            allowed_hosts=(self.host,),
            robots_required=False,
            max_bytes=10,
        )
        for path in ("/large-header", "/large-stream"):
            with self.subTest(path=path), TemporaryDirectory() as directory:
                with self.assertRaises(CrawlError) as error:
                    fetch_snapshot(
                        f"http://{self.host}{path}",
                        small,
                        Path(directory),
                        allow_private=True,
                    )
                self.assertEqual(error.exception.code, "response_too_large")

    def test_cancelled_fetch_does_not_create_a_snapshot(self):
        cancel = threading.Event()
        cancel.set()
        with TemporaryDirectory() as directory:
            with self.assertRaises(CrawlError) as error:
                fetch_snapshot(
                    f"http://{self.host}/ok",
                    self.policy(),
                    Path(directory),
                    allow_private=True,
                    cancel_event=cancel,
                )
            self.assertEqual(error.exception.code, "cancelled")
            self.assertEqual(list(Path(directory).rglob("*")), [])

    def test_same_content_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = fetch_snapshot(f"http://{self.host}/ok", self.policy(), root, allow_private=True)
            second = fetch_snapshot(f"http://{self.host}/ok", self.policy(), root, allow_private=True)
            self.assertEqual(first, second)
            self.assertEqual(len(list(root.rglob("raw.html"))), 1)

    def test_peer_address_check_blocks_dns_rebinding(self):
        policy = SourcePolicy(
            source_id="rebind",
            allowed_hosts=("public.example.test",),
            robots_required=False,
        )
        public_dns_address = f"{93}.{184}.{216}.{34}"
        with (
            TemporaryDirectory() as directory,
            patch.object(crawler, "_resolved_addresses", return_value=(public_dns_address,)),
            patch.object(crawler, "_peer_address", return_value="127.0.0.1"),
            patch.object(crawler._OPENER, "open", return_value=FakeResponse("http://public.example.test/ok")),
            self.assertRaises(CrawlError) as error,
        ):
            fetch_snapshot("http://public.example.test/ok", policy, Path(directory))
        self.assertEqual(error.exception.code, "private_address")

    def test_peer_address_unavailable_fails_closed(self):
        policy = SourcePolicy(
            source_id="peer-missing",
            allowed_hosts=("public.example.test",),
            robots_required=False,
        )
        public_dns_address = f"{93}.{184}.{216}.{34}"
        with (
            TemporaryDirectory() as directory,
            patch.object(crawler, "_resolved_addresses", return_value=(public_dns_address,)),
            patch.object(crawler, "_peer_address", return_value=None),
            patch.object(crawler._OPENER, "open", return_value=FakeResponse("http://public.example.test/ok")),
            self.assertRaises(CrawlError) as error,
        ):
            fetch_snapshot("http://public.example.test/ok", policy, Path(directory))
        self.assertEqual(error.exception.code, "peer_address_unavailable")
