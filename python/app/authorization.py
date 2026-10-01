"""Validate signed scopes with the fixed Go authority; never issue grants."""
from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import grpc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_scope(scope) -> None:
    base = os.environ.get("AUTHORITY_HTTP_BASE", "http://127.0.0.1:8080")
    parsed = urllib.parse.urlsplit(base)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (not loopback or parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ScopeError(grpc.StatusCode.UNAVAILABLE)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(base.rstrip("/") + "/internal/authorization/validate",
                                     data=scope.SerializeToString(deterministic=True),
                                     headers={"Content-Type": "application/x-protobuf"}, method="POST")
    try:
        with opener.open(request, timeout=2) as response:
            if response.status != 200 or json.loads(response.read(4096)) != {"allowed": True}:
                raise ScopeError(grpc.StatusCode.PERMISSION_DENIED)
    except urllib.error.HTTPError as error:
        raise ScopeError(grpc.StatusCode.PERMISSION_DENIED if error.code in {400, 401, 403}
                         else grpc.StatusCode.UNAVAILABLE) from None
    except (OSError, ValueError):
        raise ScopeError(grpc.StatusCode.UNAVAILABLE) from None


class ScopeError(Exception):
    def __init__(self, code):
        self.code = code
