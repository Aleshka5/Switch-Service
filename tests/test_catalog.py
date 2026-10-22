# SPDX-License-Identifier: Apache-2.0
import pytest

from switch_service.catalog import Catalog, CatalogError
from switch_service.core import SwitchCore
from switch_service.runtime import ContainerSpec, podman_payload
from switch_service.sample import parse_meminfo, parse_nvidia_csv
from switch_service.util import redact


def test_demo_catalog_runs_the_triangle(tmp_path):
    core = SwitchCore.open(
        "config/services.yaml",
        db_path=str(tmp_path / "state.sqlite"),
        runtime="fake",
        synchronous=True,
        reconcile=False,
    )
    core.set_state("alpha", "gpu")
    core.set_state("beta", "cpu")
    with pytest.raises(Exception) as caught:
        core.set_state("large", "gpu")
    assert getattr(caught.value, "code", "") == "INSUFFICIENT_VRAM"
    snapshot = core.snapshot()
    assert {item["id"] for item in snapshot["services"]} == {"alpha", "beta", "large"}
    core.close()


def test_homelab_catalog_keeps_runtime_flags_in_the_environment(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf-test-secret-value")
    monkeypatch.setenv("LLM_API_KEY", "llm-test-secret-value")
    catalog = Catalog.load("config/services.homelab.example.yaml")
    ocr = catalog.get("ocr")
    assert ocr.profiles["cpu"].command == ()
    assert dict(ocr.profiles["cpu"].env)["OCR_LLAMA_NGL"] == "0"
    assert dict(ocr.profiles["gpu"].env)["OCR_LLAMA_NGL"] == "999"
    assert "cpu" not in catalog.get("image").profiles
    assert catalog.get("image").profiles["gpu"].memory_limit_mib == 36864
    assert catalog.get("image").profiles["gpu"].calibrate_vram is False
    assert catalog.get("llm").profiles["cpu"].available is False
    assert "llm-test-secret-value" in catalog.secret_values
    assert "llm-test-secret-value" in catalog.get("llm").profiles["gpu"].command


def test_missing_secret_and_duplicate_id(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    path = tmp_path / "bad.yaml"
    path.write_text(
        "host:\n"
        "  ram_reserve_mib: 1\n"
        "  vram_reserve_mib: 1\n"
        "  sample: live\n"
        "services:\n"
        "  - id: ocr\n"
        "    profiles:\n"
        "      cpu:\n"
        "        image: demo\n"
        "        env:\n"
        '          HF_TOKEN: "${HF_TOKEN}"\n'
        "        claim:\n"
        "          ram_mib: 1\n"
        "          vram_mib: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        Catalog.load(str(path))
    with pytest.raises(CatalogError):
        Catalog.from_dict(
            {
                "host": {"ram_reserve_mib": 1, "vram_reserve_mib": 1, "sample": "live"},
                "services": [
                    {
                        "id": "a",
                        "profiles": {"cpu": {"image": "demo", "claim": {"ram_mib": 1, "vram_mib": 0}}},
                    },
                    {
                        "id": "a",
                        "profiles": {"cpu": {"image": "demo", "claim": {"ram_mib": 1, "vram_mib": 0}}},
                    },
                ],
            }
        )


def test_podman_payload_uses_cdi_and_does_not_restart():
    spec = ContainerSpec(
        name="qwen38",
        service_id="llm",
        mode="gpu",
        image="ghcr.io/ggml-org/llama.cpp:server-cuda12",
        command=("--model", "/models/model.gguf"),
        env=(("A", "B"),),
        networks=("llm_network",),
        devices=("nvidia.com/gpu=all",),
        binds=(("/models", "/models", True),),
        volumes=(),
        ports=((None, 4001, 4001),),
        memory_limit_mib=4096,
        shm_size_mib=None,
        health_url=None,
        health_timeout_s=2,
        stop_grace_s=10,
    )
    body = podman_payload(spec)
    assert body["restart_policy"] == "no"
    assert body["devices"] == [{"path": "nvidia.com/gpu=all"}]
    assert "HostConfig" not in body
    assert "DeviceRequests" not in body
    assert body["resource_limits"]["memory"]["limit"] == 4096 * 1024 * 1024
    assert body["networks"] == {"llm_network": {}}
    assert body["labels"]["switch.config-hash"] == spec.config_hash


def test_loopback_port_mapping_and_shared_network(tmp_path):
    """A health port may be published on loopback only, and the network is created once."""
    catalog = Catalog.from_dict(
        {
            "host": {
                "ram_reserve_mib": 1,
                "vram_reserve_mib": 0,
                "sample": "live",
                "network": "models_network",
            },
            "services": [
                {
                    "id": "ocr",
                    "health": {"url": "http://127.0.0.1:18090/health", "timeout_s": 1},
                    "profiles": {
                        "cpu": {
                            "image": "demo",
                            "ports": ["127.0.0.1:18090:8090", "9000:9000"],
                            "claim": {"ram_mib": 1, "vram_mib": 0},
                        }
                    },
                }
            ],
        }
    )
    assert catalog.host.network == "models_network"
    profile = catalog.get("ocr").profiles["cpu"]
    assert profile.ports == (("127.0.0.1", 18090, 8090), (None, 9000, 9000))

    spec = ContainerSpec(
        name="ocr",
        service_id="ocr",
        mode="cpu",
        image="demo",
        command=(),
        env=(),
        networks=("models_network",),
        devices=(),
        binds=(),
        volumes=(),
        ports=profile.ports,
        memory_limit_mib=None,
        shm_size_mib=None,
        health_url=None,
        health_timeout_s=1,
        stop_grace_s=1,
    )
    body = podman_payload(spec)
    assert body["portmappings"] == [
        {"host_port": 18090, "container_port": 8090, "protocol": "tcp", "host_ip": "127.0.0.1"},
        {"host_port": 9000, "container_port": 9000, "protocol": "tcp"},
    ]

    with pytest.raises(CatalogError):
        Catalog.from_dict(
            {
                "host": {"ram_reserve_mib": 1, "vram_reserve_mib": 0, "sample": "live"},
                "services": [
                    {
                        "id": "ocr",
                        "profiles": {
                            "cpu": {
                                "image": "demo",
                                "ports": ["localhost:18090:8090"],
                                "claim": {"ram_mib": 1, "vram_mib": 0},
                            }
                        },
                    }
                ],
            }
        )


def test_network_is_ensured_before_the_first_container(tmp_path):
    from switch_service.runtime import FakeRuntime
    from tests.conftest import make_core, service

    core = make_core(tmp_path, [service("alpha")], network="models_network")
    runtime = core.runtime
    assert isinstance(runtime, FakeRuntime)
    try:
        core.set_state("alpha", "cpu")
        kinds = [call[0] for call in runtime.calls]
        assert kinds[0] == "ensure_network"
        assert runtime.calls[0][1] == "models_network"
        assert "ensure" in kinds[1:]

        core.set_state("alpha", "off")
        core.set_state("alpha", "gpu")
        created = [call for call in runtime.calls if call[0] == "ensure_network"]
        assert len(created) == 1, "the network must be created only once per process"
    finally:
        core.close()


def test_parsers_and_redaction():
    total, available = parse_meminfo("MemTotal: 90112 kB\nMemAvailable: 48128 kB\n")
    assert (total, available) == (90112 // 1024, 48128 // 1024)
    parsed = parse_nvidia_csv("32768, 440, Tesla PG503-216, 7.0\n")
    assert parsed == (32768, 440, "Tesla PG503-216", "7.0")
    masked = redact("api-key: llm-test-secret-value", ["llm-test-secret-value"])
    assert "llm-test-secret-value" not in masked


def test_check_command(capsys):
    from switch_service.cli import main

    assert main(["check", "--config", "config/services.yaml"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "large" in out
