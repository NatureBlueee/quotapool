from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, Optional, Tuple

from .control import (
    FleetControl,
    FleetError,
    LeaseConflict,
    NoCapacity,
    NotFound,
    StaleWriter,
)
from .db import FleetDB


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def quota_rejection(status: int, headers: Dict[str, str], body: bytes) -> bool:
    if status != 429:
        return False
    normalized = {key.lower(): value.lower() for key, value in headers.items()}
    for key, value in normalized.items():
        if key.startswith("anthropic-ratelimit-unified") and "rejected" in value:
            return True
    # Conservative fallback for older upstreams. Ordinary RPM throttles should
    # pass through rather than stampede onto another subscription.
    text = body[:32768].decode("utf-8", "ignore").lower()
    explicit = (
        "usage limit" in text
        or "quota exhausted" in text
        or "weekly limit" in text
        or "5-hour limit" in text
        or "5 hour limit" in text
    )
    ordinary = "rate limit" in text and not explicit
    return explicit and not ordinary


def reset_from_headers(headers: Dict[str, str]) -> Optional[float]:
    now = time.time()
    for key, value in headers.items():
        lower = key.lower()
        if "reset" not in lower:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > now:
            return parsed
        if 0 < parsed < 7 * 24 * 3600:
            return now + parsed
    return None


class FleetProxy:
    def __init__(
        self,
        db_path: str,
        upstream_template: str,
        *,
        upstream_token: Optional[str] = None,
        client_token: Optional[str] = None,
    ):
        if "{account}" not in upstream_template:
            raise ValueError("upstream template must contain {account}")
        self.control = FleetControl(FleetDB(db_path))
        self.upstream_template = upstream_template
        self.upstream_token = upstream_token
        self.client_token = client_token

    def upstream_url(self, account_id: str, path: str) -> str:
        base = self.upstream_template.replace(
            "{account}", urllib.parse.quote(account_id, safe="")
        ).rstrip("/")
        return base + (path if path.startswith("/") else "/" + path)

    def request(
        self,
        account_id: str,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes,
    ) -> Tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
        url = urllib.parse.urlsplit(self.upstream_url(account_id, path))
        if url.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                url.hostname, url.port or 443, timeout=600
            )
        elif url.scheme == "http":
            conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=600)
        else:
            raise ValueError("upstream URL must be http or https")
        target = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
        outbound = {
            key: value
            for key, value in headers.items()
            if key.lower() not in HOP_BY_HOP
            and key.lower() not in {"host", "authorization", "x-api-key", "content-length"}
            and not key.lower().startswith("x-fleet-")
        }
        outbound["Content-Length"] = str(len(body))
        if self.upstream_token:
            outbound["Authorization"] = f"Bearer {self.upstream_token}"
            outbound["X-Api-Key"] = self.upstream_token
        conn.request(method, target, body=body, headers=outbound)
        return conn.getresponse(), conn


def make_handler(proxy: FleetProxy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ClaudeFleet/0.1"

        def log_message(self, fmt: str, *args) -> None:
            # Never log prompt bodies or authorization headers.
            print(f"[fleet-proxy] {self.address_string()} {fmt % args}")

        def _json(self, status: int, value: Dict[str, object]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not proxy.client_token:
                return True
            auth = self.headers.get("Authorization", "")
            key = self.headers.get("X-Api-Key", "")
            return auth == f"Bearer {proxy.client_token}" or key == proxy.client_token

        def do_GET(self) -> None:
            if self.path == "/__fleet/health":
                self._json(200, {"ok": True, "version": "0.1.0"})
                return
            self._proxy()

        def do_POST(self) -> None:
            self._proxy()

        def do_PUT(self) -> None:
            self._proxy()

        def do_DELETE(self) -> None:
            self._proxy()

        def do_PATCH(self) -> None:
            self._proxy()

        def _identity(self) -> Tuple[str, str, str]:
            session_id = (
                self.headers.get("X-Claude-Code-Session-Id")
                or self.headers.get("X-Fleet-Session-Id")
            )
            if not session_id:
                raise FleetError(
                    "missing x-claude-code-session-id; refusing stateless routing"
                )
            holder = self.headers.get("X-Fleet-Process-Id") or (
                f"{self.client_address[0]}:{self.client_address[1]}"
            )
            workload = self.headers.get("X-Fleet-Workload-Class", "interactive")
            return session_id, holder, workload

        def _proxy(self) -> None:
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            try:
                session_id, holder, workload = self._identity()
                try:
                    execution = proxy.control.get_execution_by_session(session_id)
                except NotFound:
                    execution = proxy.control.register_execution(
                        session_id,
                        execution_id=self.headers.get("X-Fleet-Execution-Id"),
                        workload_class=workload,
                        parent_id=self.headers.get("X-Fleet-Parent-Execution-Id"),
                    )
                generation_header = self.headers.get("X-Fleet-Lease-Generation")
                if generation_header:
                    generation = int(generation_header)
                    proxy.control.heartbeat_writer(session_id, holder, generation)
                else:
                    generation = proxy.control.acquire_writer(session_id, holder)
                route = proxy.control.decide_route(session_id, holder, generation)
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024 * 1024:
                    raise FleetError("request body exceeds 64 MiB replay limit")
                body = self.rfile.read(length) if length else b""
                headers = {key: value for key, value in self.headers.items()}
                response, conn = proxy.request(
                    route["account_id"], self.command, self.path, headers, body
                )
                retry_body: Optional[bytes] = None
                if response.status == 429:
                    retry_body = response.read()
                    response_headers = dict(response.getheaders())
                    conn.close()
                    if quota_rejection(response.status, response_headers, retry_body):
                        proxy.control.mark_exhausted(
                            route["account_id"],
                            reset_at=reset_from_headers(response_headers),
                        )
                        route = proxy.control.decide_route(
                            session_id,
                            holder,
                            generation,
                            exclude={route["account_id"]},
                        )
                        response, conn = proxy.request(
                            route["account_id"], self.command, self.path, headers, body
                        )
                        retry_body = None
                self.send_response(response.status, response.reason)
                response_headers = response.getheaders()
                has_length = False
                for key, value in response_headers:
                    if key.lower() in HOP_BY_HOP:
                        continue
                    if key.lower() == "content-length":
                        has_length = True
                    self.send_header(key, value)
                if retry_body is not None and not has_length:
                    self.send_header("Content-Length", str(len(retry_body)))
                    has_length = True
                if not has_length:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.send_header("X-Fleet-Account", route["account_id"])
                self.send_header(
                    "X-Fleet-Route-Generation", str(route["route_generation"])
                )
                self.end_headers()
                if retry_body is not None:
                    self.wfile.write(retry_body)
                else:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                conn.close()
            except (FleetError, ValueError) as exc:
                self.close_connection = True
                status = 409 if isinstance(exc, (LeaseConflict, StaleWriter)) else 503
                self._json(status, {
                    "error": getattr(exc, "code", "invalid_request"),
                    "message": str(exc),
                })
            except (OSError, http.client.HTTPException, socket.error) as exc:
                self.close_connection = True
                self._json(502, {"error": "upstream_unavailable", "message": str(exc)})

    return Handler


def serve(
    db_path: str,
    upstream_template: str,
    host: str = "127.0.0.1",
    port: int = 17890,
    *,
    upstream_token: Optional[str] = None,
    client_token: Optional[str] = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"} and os.environ.get(
        "FLEET_ALLOW_REMOTE"
    ) != "1":
        raise ValueError("remote listen refused; set FLEET_ALLOW_REMOTE=1 explicitly")
    proxy = FleetProxy(
        db_path,
        upstream_template,
        upstream_token=upstream_token,
        client_token=client_token,
    )
    server = ThreadingHTTPServer((host, port), make_handler(proxy))
    return server
