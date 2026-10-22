# SPDX-License-Identifier: Apache-2.0
"""Container runtimes.

``FakeRuntime`` executes nothing. It is the default for local use and tests.
``PodmanRuntime`` talks to the rootless Podman API over its unix socket.
GPU devices are CDI names (``nvidia.com/gpu=all``) in ``devices[].path``.
The Docker ``HostConfig.DeviceRequests`` field is intentionally not used.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from switch_service.errors import SwitchError
from switch_service.util import canonical_hash


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    service_id: str
    mode: str
    image: str
    command: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    networks: tuple[str, ...]
    devices: tuple[str, ...]
    binds: tuple[tuple[str, str, bool], ...]
    volumes: tuple[tuple[str, str, bool], ...]
    ports: tuple[tuple[str | None, int, int], ...]
    memory_limit_mib: int | None
    shm_size_mib: int | None
    health_url: str | None
    health_timeout_s: float
    stop_grace_s: float
    recreate: bool = False

    @property
    def config_hash(self) -> str:
        payload = {
            "name": self.name,
            "image": self.image,
            "command": list(self.command),
            "env": list(self.env),
            "networks": list(self.networks),
            "devices": list(self.devices),
            "binds": [list(item) for item in self.binds],
            "volumes": [list(item) for item in self.volumes],
            "ports": [list(item) for item in self.ports],
            "memory_limit_mib": self.memory_limit_mib,
            "shm_size_mib": self.shm_size_mib,
        }
        return canonical_hash(payload)


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    id: str | None
    running: bool
    status: str
    mode: str | None
    config_hash: str | None
    image: str | None
    exit_code: int | None = None
    oom_killed: bool = False


@dataclass(frozen=True)
class Stats:
    ram_mib: int | None


class _FakeContainer:
    def __init__(self, spec: ContainerSpec, *, running: bool, status: str) -> None:
        self.spec = spec
        self.running = running
        self.status = status
        self.hash_override: str | None = None
        self.measured_ram_mib: int | None = None


class FakeRuntime:
    """In-memory containers. ``ram_factor`` scales the cgroup limit into a fake RSS."""

    def __init__(self) -> None:
        self.containers: dict[str, _FakeContainer] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.logs_text: dict[str, str] = {}
        self.networks: set[str] = set()
        self.fail_modes: set[str] = set()
        self.unhealthy: set[str] = set()
        self.ram_factor = 0.25
        self.down = False
        self.gate: Any = None
        self.entered: Any = None

    def ready(self) -> bool:
        return not self.down

    def ensure_network(self, name: str, subnet: str | None = None) -> None:
        self.calls.append(("ensure_network", name, subnet))
        self.networks.add(name)

    def ensure(self, spec: ContainerSpec) -> None:
        self.calls.append(("ensure", spec.name, spec.mode, spec.config_hash))
        current = self.containers.get(spec.name)
        if (
            current is not None
            and not spec.recreate
            and (current.hash_override or current.spec.config_hash) == spec.config_hash
        ):
            current.spec = spec
            return
        if current is not None and current.running:
            self.stop(spec.name, spec.stop_grace_s)
        if current is not None:
            self.remove(spec.name)
        self.containers[spec.name] = _FakeContainer(spec, running=False, status="created")

    def start(self, spec: ContainerSpec) -> None:
        self.calls.append(("start", spec.name, spec.mode))
        if self.gate is not None and not self.gate.is_set():
            if self.entered is not None:
                self.entered.set()
            if not self.gate.wait(5):
                raise SwitchError(502, "RUNTIME_FAILED", "timed out waiting to start")
        current = self.containers.get(spec.name)
        if current is None:
            self.containers[spec.name] = _FakeContainer(spec, running=False, status="created")
            current = self.containers[spec.name]
        current.spec = spec
        if spec.mode in self.fail_modes:
            current.running = False
            current.status = "exited"
            return
        current.running = True
        current.status = "running"
        if current.measured_ram_mib is None and spec.memory_limit_mib:
            current.measured_ram_mib = max(1, int(spec.memory_limit_mib * self.ram_factor))

    def stop(self, name: str, grace_s: float) -> None:
        self.calls.append(("stop", name, grace_s))
        current = self.containers.get(name)
        if current is not None:
            current.running = False
            current.status = "exited"

    def kill(self, name: str) -> None:
        self.calls.append(("kill", name))
        self.stop(name, 0)

    def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        self.containers.pop(name, None)

    def inspect(self, name: str) -> ContainerInfo | None:
        current = self.containers.get(name)
        if current is None:
            return None
        return ContainerInfo(
            name=name,
            id=f"fake-{name}",
            running=current.running,
            status=current.status,
            mode=current.spec.mode,
            config_hash=current.hash_override or current.spec.config_hash,
            image=current.spec.image,
            exit_code=None if current.running else 1,
        )

    def probe(self, spec: ContainerSpec) -> bool:
        info = self.inspect(spec.name)
        if info is None or not info.running:
            return False
        if spec.name in self.unhealthy:
            return False
        return True

    def logs(self, name: str, tail: int) -> str:
        text = self.logs_text.get(name, "")
        if tail <= 0:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-tail:])

    def stats(self, name: str) -> Stats | None:
        current = self.containers.get(name)
        if current is None or not current.running:
            return None
        return Stats(ram_mib=current.measured_ram_mib)


def podman_payload(spec: ContainerSpec) -> dict[str, Any]:
    """Libpod create body. CDI devices go in ``devices[].path``, never DeviceRequests."""
    body: dict[str, Any] = {
        "name": spec.name,
        "image": spec.image,
        "env": dict(spec.env),
        "labels": {
            "switch.service": spec.service_id,
            "switch.mode": spec.mode,
            "switch.config-hash": spec.config_hash,
        },
        "restart_policy": "no",
        "netns": {"nsmode": "bridge"},
    }
    if spec.command:
        body["command"] = list(spec.command)
    if spec.networks:
        body["networks"] = {network: {} for network in spec.networks}
    if spec.devices:
        body["devices"] = [{"path": device} for device in spec.devices]
    if spec.binds:
        body["mounts"] = [
            {
                "type": "bind",
                "source": source,
                "destination": dest,
                "options": ["rbind", "ro"] if read_only else ["rbind"],
            }
            for source, dest, read_only in spec.binds
        ]
    if spec.volumes:
        body["volumes"] = [
            {"name": name, "dest": dest, **({"options": ["ro"]} if read_only else {})}
            for name, dest, read_only in spec.volumes
        ]
    if spec.ports:
        body["portmappings"] = [
            {
                "host_port": host,
                "container_port": container,
                "protocol": "tcp",
                **({"host_ip": host_ip} if host_ip else {}),
            }
            for host_ip, host, container in spec.ports
        ]
    if spec.memory_limit_mib:
        body["resource_limits"] = {"memory": {"limit": spec.memory_limit_mib * 1024 * 1024}}
    if spec.shm_size_mib:
        body["shm_size"] = spec.shm_size_mib * 1024 * 1024
        body["ipcns"] = {"nsmode": "private"}
    return body


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix)
        self.sock = sock


class PodmanRuntime:
    def __init__(self, socket_path: str | None = None, api_version: str = "v5.0.0") -> None:
        self.socket_path = socket_path or default_podman_socket()
        self.api_version = api_version

    def ready(self) -> bool:
        if not self.socket_path or not os.path.exists(self.socket_path):
            return False
        try:
            status, _ = self._request("GET", "/_ping", timeout=2)
        except OSError:
            return False
        return status == 200

    def ensure_network(self, name: str, subnet: str | None = None) -> None:
        """Create the shared bridge network once. 409 means somebody else won the race."""
        status, raw = self._request("GET", f"/networks/{quote(name)}/json", timeout=10)
        if status == 200:
            return
        if status != 404:
            raise SwitchError(
                502, "RUNTIME_FAILED", _podman_message("network inspect", status, raw)
            )
        body: dict[str, Any] = {"name": name, "driver": "bridge"}
        if subnet:
            body["subnets"] = [subnet]
        status, raw = self._request("POST", "/networks/create", body=body, timeout=30)
        if status in {200, 201, 409}:
            return
        raise SwitchError(502, "RUNTIME_FAILED", _podman_message("network create", status, raw))

    def ensure(self, spec: ContainerSpec) -> None:
        info = self.inspect(spec.name)
        if info is not None and not spec.recreate and info.config_hash == spec.config_hash:
            return
        if info is not None:
            if info.running:
                self.stop(spec.name, spec.stop_grace_s)
            self.remove(spec.name)
        self._create(spec)

    def start(self, spec: ContainerSpec) -> None:
        status, raw = self._request("POST", f"/containers/{quote(spec.name)}/start", timeout=60)
        if status not in {200, 204, 304}:
            raise SwitchError(502, "RUNTIME_FAILED", _podman_message("start", status, raw))

    def stop(self, name: str, grace_s: float) -> None:
        grace = max(1, int(grace_s))
        status, raw = self._request(
            "POST",
            f"/containers/{quote(name)}/stop?t={grace}",
            timeout=grace + 15,
        )
        if status in {200, 204, 304, 404}:
            return
        raise SwitchError(502, "RUNTIME_FAILED", _podman_message("stop", status, raw))

    def kill(self, name: str) -> None:
        status, raw = self._request("POST", f"/containers/{quote(name)}/kill", timeout=20)
        if status in {200, 204, 404}:
            return
        raise SwitchError(502, "RUNTIME_FAILED", _podman_message("kill", status, raw))

    def remove(self, name: str) -> None:
        status, raw = self._request("DELETE", f"/containers/{quote(name)}?force=true", timeout=30)
        if status in {200, 204, 404}:
            return
        raise SwitchError(502, "RUNTIME_FAILED", _podman_message("remove", status, raw))

    def inspect(self, name: str) -> ContainerInfo | None:
        status, raw = self._request("GET", f"/containers/{quote(name)}/json", timeout=10)
        if status == 404:
            return None
        if status != 200:
            raise SwitchError(502, "RUNTIME_FAILED", _podman_message("inspect", status, raw))
        data = json.loads(raw.decode("utf-8") or "{}")
        state = data.get("State") or {}
        config = data.get("Config") or {}
        labels = config.get("Labels") or data.get("Labels") or {}
        running = bool(state.get("Running")) or state.get("Status") == "running"
        return ContainerInfo(
            name=name,
            id=data.get("Id"),
            running=running,
            status=str(state.get("Status") or ("running" if running else "exited")),
            mode=labels.get("switch.mode"),
            config_hash=labels.get("switch.config-hash"),
            image=data.get("ImageName") or config.get("Image"),
            exit_code=state.get("ExitCode"),
            oom_killed=bool(state.get("OOMKilled")),
        )

    def probe(self, spec: ContainerSpec) -> bool:
        info = self.inspect(spec.name)
        if info is None or not info.running:
            return False
        if not spec.health_url:
            return True
        request = urllib.request.Request(spec.health_url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=spec.health_timeout_s) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def logs(self, name: str, tail: int) -> str:
        tail = max(1, min(tail, 2000))
        status, raw = self._request(
            "GET",
            f"/containers/{quote(name)}/logs?stdout=true&stderr=true&tail={tail}",
            timeout=15,
        )
        if status == 404:
            return ""
        if status != 200:
            raise SwitchError(502, "RUNTIME_FAILED", _podman_message("logs", status, raw))
        return _demux(raw)

    def stats(self, name: str) -> Stats | None:
        status, raw = self._request(
            "GET", f"/containers/{quote(name)}/stats?stream=false", timeout=15
        )
        if status != 200:
            return None
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return None
        ram = _find_memory_bytes(payload)
        if ram is None:
            return Stats(ram_mib=None)
        return Stats(ram_mib=max(0, ram // (1024 * 1024)))

    def _create(self, spec: ContainerSpec) -> None:
        status, raw = self._request(
            "POST",
            f"/containers/create?name={quote(spec.name)}",
            body=podman_payload(spec),
            timeout=60,
        )
        if status in {200, 201}:
            return
        message = _podman_message("create", status, raw)
        code = "IMAGE_MISSING" if status == 404 else "RUNTIME_FAILED"
        raise SwitchError(502, code, message)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> tuple[int, bytes]:
        target = f"/{self.api_version}/libpod{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = _UnixConnection(self.socket_path, timeout)
        try:
            connection.request(method, target, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()


def default_podman_socket() -> str:
    configured = os.environ.get("CONTAINER_HOST", "")
    if configured.startswith("unix://"):
        return configured[len("unix://") :]
    user_socket = f"/run/user/{os.getuid()}/podman/podman.sock"
    if os.path.exists(user_socket):
        return user_socket
    return "/run/podman/podman.sock"


def _podman_message(action: str, status: int, raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip().replace("\n", " ")
    if len(text) > 400:
        text = text[:400] + "…"
    return f"podman {action} failed ({status}): {text or 'empty response'}"


def _demux(raw: bytes) -> str:
    if len(raw) >= 8 and raw[0] in {0, 1, 2}:
        chunks: list[bytes] = []
        index = 0
        while index + 8 <= len(raw):
            length = int.from_bytes(raw[index + 4 : index + 8], "big")
            end = index + 8 + length
            if length < 0 or end > len(raw):
                break
            chunks.append(raw[index + 8 : end])
            index = end
        if chunks and index == len(raw):
            return b"".join(chunks).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def _find_memory_bytes(node: Any, parent: str = "") -> int | None:
    if isinstance(node, dict):
        for key in ("MemUsage", "mem_usage", "memory_usage"):
            if isinstance(node.get(key), int):
                return node[key]
        if parent == "memory" and isinstance(node.get("usage"), int):
            return node["usage"]
        for key, value in node.items():
            found = _find_memory_bytes(value, key.lower())
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_memory_bytes(value, parent)
            if found is not None:
                return found
    return None
