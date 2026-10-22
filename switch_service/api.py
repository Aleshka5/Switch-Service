# SPDX-License-Identifier: Apache-2.0
"""Small HTTP API. Authentication is a static token; put a gateway in front for users."""

from __future__ import annotations

import hmac
import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from switch_service.core import SwitchCore
from switch_service.errors import SwitchError

log = logging.getLogger("switch_service.api")


def serve(core: SwitchCore, host: str, port: int, token: str | None = None) -> None:
    handler = _handler_class(core, token)
    httpd = ThreadingHTTPServer((host, port), handler)
    core.recover()
    core.start_worker()
    log.info("listening on http://%s:%s", host, port)

    def _stop(signum: int, _frame: Any) -> None:
        log.info("signal %s, shutting down", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        httpd.serve_forever()
    finally:
        core.close()
        httpd.server_close()


def _handler_class(core: SwitchCore, token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - " + fmt, self.address_string(), *args)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            if path not in {"/healthz", "/readyz"} and not _authorized(self, token):
                _send(self, 401, {"error": {"code": "UNAUTHORIZED", "message": "missing token"}})
                return
            try:
                status, body, content_type = _route(core, method, path, query, self, token)
            except SwitchError as exc:
                _send(self, exc.status, exc.to_body())
                return
            except Exception:
                log.exception("request failed")
                _send(
                    self,
                    500,
                    {"error": {"code": "INTERNAL", "message": "internal error"}},
                )
                return
            if content_type == "application/json":
                _send(self, status, body)
            else:
                _send_raw(self, status, body, content_type)

    return Handler


def _route(
    core: SwitchCore,
    method: str,
    path: str,
    query: dict[str, list[str]],
    handler: BaseHTTPRequestHandler,
    token: str | None,
) -> tuple[int, Any, str]:
    if method == "GET" and path == "/healthz":
        return 200, {"ok": True}, "application/json"
    if method == "GET" and path == "/readyz":
        ready = core.runtime.ready()
        return (200 if ready else 503), {"ok": ready}, "application/json"
    if method == "GET" and path == "/metrics":
        return 200, core.render_metrics(), "text/plain; version=0.0.4"
    if method == "GET" and path == "/api/services":
        return 200, core.snapshot(), "application/json"
    if method == "GET" and path == "/api/services/transitions":
        limit = int(query.get("limit", ["50"])[0])
        return 200, {"transitions": core.transition_history(limit)}, "application/json"
    parts = [part for part in path.split("/") if part]
    # /api/services/{id}[/...]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "services":
        service_id = parts[2]
        if method == "GET" and len(parts) == 3:
            return 200, core.service_view(service_id), "application/json"
        if method == "GET" and parts[3:] == ["logs"]:
            tail = int(query.get("tail", ["200"])[0])
            return 200, core.logs(service_id, tail), "text/plain; charset=utf-8"
        if method == "POST" and parts[3:] == ["state"]:
            payload = _json_body(handler)
            result = core.set_state(
                service_id,
                payload.get("state"),
                evict=payload.get("evict"),
                force=bool(payload.get("force", False)),
                options=payload.get("options"),
                actor=_actor(handler, token),
            )
            return 202, result, "application/json"
        if method == "POST" and parts[3:] == ["actions", "restart"]:
            result = core.restart(service_id, actor=_actor(handler, token))
            return 202, result, "application/json"
        if method == "POST" and parts[3:] == ["actions", "recalibrate"]:
            return 200, core.recalibrate(service_id, actor=_actor(handler, token)), "application/json"
    raise SwitchError(404, "NOT_FOUND", f"no route for {method} {path}")


def _json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length > 65536:
        raise SwitchError(400, "BAD_OPTIONS", "body is too large")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwitchError(400, "BAD_OPTIONS", "body must be JSON") from exc
    if not isinstance(payload, dict):
        raise SwitchError(400, "BAD_OPTIONS", "body must be a JSON object")
    allowed = {"state", "evict", "force", "options"}
    unknown = set(payload) - allowed
    if unknown:
        raise SwitchError(400, "BAD_OPTIONS", f"unknown fields: {sorted(unknown)}")
    if "force" in payload and not isinstance(payload["force"], bool):
        raise SwitchError(400, "BAD_OPTIONS", "force must be a boolean")
    return payload


def _authorized(handler: BaseHTTPRequestHandler, token: str | None) -> bool:
    if not token:
        return True
    supplied = ""
    header = handler.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    elif handler.headers.get("X-Service-Token"):
        supplied = handler.headers.get("X-Service-Token", "").strip()
    if len(supplied) != len(token):
        return False
    return hmac.compare_digest(supplied, token)


def _actor(handler: BaseHTTPRequestHandler, token: str | None) -> str:
    email = handler.headers.get("X-Auth-Email", "").strip()
    if email:
        return email[:200]
    if token:
        return "service"
    return "local"


def _send(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _send_raw(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    payload = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)
