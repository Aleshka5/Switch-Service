#!/usr/bin/env bash
# One place for the model topology: create the shared bridge network, attach
# the consumers that must resolve the model names, and preflight the host.
#
#   scripts/models-network.sh up        # network + attach consumers (idempotent)
#   scripts/models-network.sh status    # who is on the network, are the ports alive
#   scripts/models-network.sh doctor    # images, ports, GPU, socket, catalogue
#
# It never starts a model: the switch service owns those containers. This file
# only prepares the network the containers are created in.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
NETWORK=${NETWORK:-models_network}
CONFIG=${CONFIG:-$ROOT/config/services.models-network.yaml}
CONSUMERS=${CONSUMERS:-"gateway rag-chat_processor_1 rag-chat_chat_1"}
PYTHON=${PYTHON:-$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)}

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have_podman() { command -v podman >/dev/null 2>&1 || die "podman is not installed"; }

network_exists() { podman network exists "$NETWORK" >/dev/null 2>&1; }

attached() {
    # Names of running containers attached to $NETWORK.
    podman ps --format '{{.Names}}' | while read -r name; do
        if podman inspect "$name" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
            2>/dev/null | tr ' ' '\n' | grep -qx "$NETWORK"; then
            printf '%s\n' "$name"
        fi
    done
}

cmd_up() {
    have_podman
    if network_exists; then
        say "network $NETWORK: already exists"
    else
        podman network create "$NETWORK" >/dev/null
        say "network $NETWORK: created"
    fi

    local connected=0 skipped=0
    for name in $CONSUMERS; do
        if ! podman container exists "$name" >/dev/null 2>&1; then
            say "consumer $name: not found, skipped"
            skipped=$((skipped + 1))
            continue
        fi
        if ! podman inspect "$name" --format '{{.State.Running}}' | grep -qx true; then
            say "consumer $name: not running, skipped"
            skipped=$((skipped + 1))
            continue
        fi
        if podman inspect "$name" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
            | tr ' ' '\n' | grep -qx "$NETWORK"; then
            say "consumer $name: already attached"
            continue
        fi
        podman network connect "$NETWORK" "$name" >/dev/null
        say "consumer $name: attached"
        connected=$((connected + 1))
    done

    say ""
    say "network members:"
    attached | sed 's/^/  /' || true
    if [ "$connected" -eq 0 ] && [ "$skipped" -eq 0 ]; then
        say "  (nothing changed)"
    fi
    say ""
    say "next:"
    say "  make run RUNTIME=podman CONFIG=config/services.models-network.yaml"
    say "  .venv/bin/switch-service state ocr gpu"
}

cmd_status() {
    have_podman
    network_exists || die "network $NETWORK does not exist; run: $0 up"
    podman network inspect "$NETWORK" \
        --format 'network {{.Name}} driver={{.Driver}} dns={{.DNSEnabled}} subnet={{range .Subnets}}{{.Subnet}}{{end}}'
    say ""
    say "members:"
    attached | sed 's/^/  /' || true
    say ""
    [ -f "$CONFIG" ] || { say "no catalogue at $CONFIG"; return 0; }
    "$PYTHON" - "$CONFIG" <<'PY'
import json
import sys
import urllib.error
import urllib.request

import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    catalog = yaml.safe_load(handle)
services = catalog.get("services") or []
print("health probes:")
for item in services:
    url = (item.get("health") or {}).get("url")
    if not url:
        print(f"  {item['id']:<10} no health url")
        continue
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            print(f"  {item['id']:<10} {url} -> {response.status}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  {item['id']:<10} {url} -> down ({exc.__class__.__name__})")
PY
}

cmd_doctor() {
    have_podman
    local failures=0
    ok() { say "  ok      $*"; }
    bad() { say "  FAIL    $*"; failures=$((failures + 1)); }

    say "podman"
    ok "$(podman --version)"
    podman info --format '{{.Host.Security.Rootless}}' | grep -qx true \
        && ok "rootless" || bad "podman is rootful; the catalogue assumes a rootless user socket"

    say ""
    say "control plane socket"
    local socket=${CONTAINER_HOST:-unix:///run/user/$(id -u)/podman/podman.sock}
    socket=${socket#unix://}
    if [ -S "$socket" ]; then
        ok "$socket"
    else
        bad "$socket is missing: systemctl --user enable --now podman.socket"
    fi

    say ""
    say "GPU"
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader \
            | sed 's/^/  ok      /'
    else
        bad "nvidia-smi not found; VRAM claims cannot be sampled"
    fi
    local available
    available=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
    ok "MemAvailable ${available} MiB"

    [ -f "$CONFIG" ] || die "no catalogue at $CONFIG"
    say ""
    say "images (from $CONFIG)"
    local image status
    while IFS=$'\t' read -r image status; do
        if podman image exists "$image"; then
            ok "$image ($status)"
        else
            bad "$image is missing ($status)"
        fi
    done < <("$PYTHON" - "$CONFIG" <<'PY'
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    catalog = yaml.safe_load(handle)
seen = {}
for item in catalog.get("services") or []:
    for mode, body in (item.get("profiles") or {}).items():
        if body.get("available", True) is False:
            continue
        seen.setdefault(body["image"], []).append(f"{item['id']}.{mode}")
for image, users in sorted(seen.items()):
    print(f"{image}\t{','.join(users)}")
PY
    )

    say ""
    say "health ports (loopback, published by the containers)"
    local port holder
    for port in $(sed -n 's/.*"127\.0\.0\.1:\([0-9]\{2,5\}\):.*/\1/p' "$CONFIG" | sort -u); do
        if holder=$(ss -ltnp 2>/dev/null | grep ":${port} " | grep -o 'users:(([^)]*))' | head -1); then
            ok "127.0.0.1:$port taken ${holder:-(owner unknown)}"
        else
            ok "127.0.0.1:$port free"
        fi
    done

    say ""
    say "catalogue"
    if [ -f "$ROOT/config/secrets.env" ]; then
        if (cd "$ROOT" && SWITCH_SECRETS="$ROOT/config/secrets.env" \
            .venv/bin/switch-service check --config "$CONFIG" >/dev/null 2>&1); then
            ok "loads with config/secrets.env"
        else
            bad "does not load with config/secrets.env (run: switch-service check --config $CONFIG)"
        fi
    else
        say "  skip    no config/secrets.env; variables like \${HF_TOKEN} are unresolved"
    fi

    say ""
    say "network"
    network_exists && ok "$NETWORK exists" || ok "$NETWORK not created yet (run: $0 up)"

    say ""
    if [ "$failures" -eq 0 ]; then
        say "doctor: all checks passed"
    else
        say "doctor: $failures problem(s)"
        return 1
    fi
}

case "${1:-up}" in
    up) cmd_up ;;
    status) cmd_status ;;
    doctor) cmd_doctor ;;
    *) die "usage: $0 [up|status|doctor]" ;;
esac
