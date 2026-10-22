# SPDX-License-Identifier: Apache-2.0
"""Command line. ``serve`` runs the API; the other commands talk to it or check a file."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from switch_service.catalog import Catalog, CatalogError
from switch_service.core import SwitchCore
from switch_service.errors import SwitchError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="switch-service")
    parser.add_argument("--url", default=os.environ.get("SWITCH_URL", "http://127.0.0.1:8095"))
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--config", default="config/services.yaml")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8095)
    serve.add_argument("--db", default=os.environ.get("SWITCH_DB"))
    serve.add_argument("--runtime", default=os.environ.get("SWITCH_RUNTIME"))

    check = sub.add_parser("check", help="validate a catalog and print its triangle")
    check.add_argument("--config", default="config/services.yaml")

    status = sub.add_parser("status", help="print the current snapshot")
    status.add_argument("--json", action="store_true")

    state = sub.add_parser("state", help="request a triangle transition")
    state.add_argument("service")
    state.add_argument("target", choices=["off", "cpu", "gpu"])
    state.add_argument("--evict", action="append", default=[])
    state.add_argument("--force", action="store_true")
    state.add_argument("--references", type=int)
    state.add_argument("--no-wait", action="store_true")
    state.add_argument("--timeout", type=float, default=300)

    logs = sub.add_parser("logs", help="print a container log tail")
    logs.add_argument("service")
    logs.add_argument("--tail", type=int, default=200)

    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args)
        if args.command == "check":
            return _check(args)
        if args.command == "status":
            return _status(args)
        if args.command == "state":
            return _state(args)
        if args.command == "logs":
            return _logs(args)
    except CatalogError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    except SwitchError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    return 2


def _serve(args: argparse.Namespace) -> int:
    from switch_service.api import serve

    core = SwitchCore.open(
        args.config,
        db_path=args.db,
        runtime=args.runtime,
        synchronous=False,
        reconcile=True,
    )
    if core.catalog.host.sample == "fixture":
        print("sample source is fixture, not the live host", file=sys.stderr)
    token = os.environ.get("SWITCH_TOKEN") or None
    serve(core, args.host, args.port, token)
    return 0


def _check(args: argparse.Namespace) -> int:
    catalog = Catalog.load(args.config)
    print(f"sample: {catalog.host.sample}")
    print(
        f"reserves: ram {catalog.host.ram_reserve_mib} MiB, "
        f"vram {catalog.host.vram_reserve_mib} MiB"
    )
    for service in catalog.services:
        modes = ", ".join(service.supported_states)
        disabled = ", ".join(
            f"{name} ({info['reason']})" for name, info in service.unavailable().items()
        )
        line = f"{service.id:12} states {modes}"
        if disabled:
            line += f"  unavailable: {disabled}"
        print(line)
    return 0


def _status(args: argparse.Namespace) -> int:
    body = _request("GET", args.url, "/api/services")
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    host = body["host"]
    ram = host["ram"]
    print(
        f"RAM free {ram['free_mib']} MiB / budget {ram['budget_mib']} "
        f"(claimed {ram['claimed_mib']}, reserve {ram['reserve_mib']}, "
        f"ledger {ram['ledger_free_mib']}, source {ram['free_source']})"
    )
    if host["vram"]:
        vram = host["vram"]
        print(
            f"VRAM free {vram['free_mib']} MiB / budget {vram['budget_mib']} "
            f"(claimed {vram['claimed_mib']}, used {vram['used_mib']}, "
            f"unmanaged {vram['unmanaged_mib']}, ledger {vram['ledger_free_mib']}, "
            f"source {vram['free_source']})"
        )
    else:
        print("VRAM: no sample")
    for service in body["services"]:
        allowed = ",".join(service["transitions_allowed"]) or "-"
        claim = service["claim"]
        print(
            f"{service['id']:12} {service['state']:10} mode={service['mode'] or '-':3} "
            f"desired={service['desired_state']:3} -> {allowed:8} "
            f"ram={claim['ram_mib']} vram={claim['vram_mib']}"
        )
    return 0


def _state(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"state": args.target, "force": args.force}
    if args.evict:
        payload["evict"] = args.evict
    if args.references is not None:
        payload["options"] = {"references": args.references}
    try:
        body = _request("POST", args.url, f"/api/services/{args.service}/state", payload)
    except urllib.error.HTTPError as exc:
        _print_http_error(exc)
        return 1
    if body.get("noop"):
        print(f"{args.service} is already {args.target}")
        return 0
    transition = body.get("transition") or {}
    print(f"accepted {transition.get('id')} {transition.get('from')} -> {transition.get('to')}")
    if args.no_wait:
        return 0
    return _wait(args.url, args.service, transition.get("id"), args.target, args.timeout)


def _wait(base: str, service_id: str, transition_id: str | None, target: str, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _request("GET", base, f"/api/services/{service_id}")
        transition = body.get("transition")
        if transition is None or transition.get("id") != transition_id:
            if body.get("state") == "ready" and body.get("mode") == target:
                print(f"{service_id} ready {target}")
                return 0
            if target == "off" and body.get("state") == "off" and body.get("desired_state") == "off":
                print(f"{service_id} off")
                return 0
            if body.get("state") == "failed":
                print(body.get("last_error") or "failed", file=sys.stderr)
                return 1
        elif transition.get("status") == "failed":
            error = transition.get("error") or {}
            print(f"{error.get('code')}: {error.get('message')}", file=sys.stderr)
            return 1
        time.sleep(0.5)
    print("timed out waiting for the transition", file=sys.stderr)
    return 1


def _logs(args: argparse.Namespace) -> int:
    token = os.environ.get("SWITCH_TOKEN")
    url = args.url.rstrip("/") + f"/api/services/{args.service}/logs?tail={args.tail}"
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        sys.stdout.write(response.read().decode("utf-8"))
    return 0


def _request(method: str, base: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = os.environ.get("SWITCH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": {"code": "HTTP", "message": raw}}
        error = body.get("error") or {}
        raise SwitchError(
            exc.code,
            error.get("code", "HTTP"),
            error.get("message", raw),
            error.get("details"),
        ) from exc
    if not raw:
        return None
    return json.loads(raw)


def _print_http_error(exc: urllib.error.HTTPError) -> None:
    raw = exc.read().decode("utf-8", "replace")
    print(raw or str(exc), file=sys.stderr)
