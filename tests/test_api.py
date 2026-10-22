# SPDX-License-Identifier: Apache-2.0
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from switch_service.api import _handler_class


def _start(core, token=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(core, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(port, method, path, body=None, token=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, raw


def test_state_and_snapshot_round_trip(core):
    server = _start(core)
    port = server.server_address[1]
    try:
        status, raw = _request(port, "POST", "/api/services/alpha/state", {"state": "gpu"})
        assert status == 202
        body = json.loads(raw)
        assert body["noop"] is False
        assert body["transition"]["to"] == "gpu"
        status, raw = _request(port, "GET", "/api/services")
        assert status == 200
        snapshot = json.loads(raw)
        alpha = next(item for item in snapshot["services"] if item["id"] == "alpha")
        assert alpha["mode"] == "gpu"
        assert snapshot["host"]["ram"]["budget_mib"] == 90112 - 8192
        assert "active_transitions" in snapshot
        status, raw = _request(port, "GET", "/api/services/alpha/logs?tail=10")
        assert status == 200
        status, raw = _request(port, "GET", "/healthz")
        assert status == 200
        status, raw = _request(port, "GET", "/readyz")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_conflict_is_synchronous_and_token_is_required(core):
    core.set_state("large", "gpu")
    server = _start(core, token="sekret-token-value")
    port = server.server_address[1]
    try:
        status, raw = _request(port, "GET", "/api/services")
        assert status == 401
        status, raw = _request(
            port, "POST", "/api/services/alpha/state", {"state": "gpu"}, token="sekret-token-value"
        )
        assert status == 409
        body = json.loads(raw)
        assert body["error"]["code"] == "INSUFFICIENT_VRAM"
        assert body["error"]["details"]["suggestions"]
    finally:
        server.shutdown()
        server.server_close()
