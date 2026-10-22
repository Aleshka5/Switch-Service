# SPDX-License-Identifier: Apache-2.0
import json
import threading

import pytest

from switch_service.errors import SwitchError
from tests.conftest import make_core, service


def test_triangle_edges_do_not_pass_through_off(core):
    previous = "off"
    for target in ("cpu", "gpu", "off", "gpu", "cpu", "off"):
        result = core.set_state("alpha", target, actor="tester")
        transition = result["transition"]
        assert transition["from"] == previous
        assert transition["to"] == target
        assert transition["status"] == "ok"
        view = core.service_view("alpha")
        if target == "off":
            assert view["state"] == "off"
            assert view["mode"] is None
            assert view["claim"] == {"ram_mib": 0, "vram_mib": 0}
            assert view["transitions_allowed"] == ["cpu", "gpu"]
        else:
            assert view["state"] == "ready"
            assert view["mode"] == target
            assert view["desired_state"] == target
            others = {"cpu": ["off", "gpu"], "gpu": ["off", "cpu"]}
            assert view["transitions_allowed"] == others[target]
        previous = target


def test_same_state_is_a_noop_when_healthy(core):
    core.set_state("alpha", "cpu")
    calls = len(core.runtime.calls)
    result = core.set_state("alpha", "cpu")
    assert result == {"noop": True, "transition": None}
    assert len(core.runtime.calls) == calls


def test_insufficient_vram_does_not_touch_containers(core):
    core.set_state("large", "gpu")
    calls = list(core.runtime.calls)
    with pytest.raises(SwitchError) as caught:
        core.set_state("alpha", "gpu")
    assert caught.value.status == 409
    assert caught.value.code == "INSUFFICIENT_VRAM"
    assert "required_mib" in caught.value.details
    assert core.runtime.calls == calls
    assert core.service_view("large")["mode"] == "gpu"
    assert core.service_view("alpha")["state"] == "off"


def test_force_evicts_a_non_preemptible_holder_and_nothing_else(core):
    core.set_state("large", "gpu")
    core.set_state("beta", "cpu")
    with pytest.raises(SwitchError) as caught:
        core.set_state("alpha", "gpu", evict=["large"])
    assert caught.value.code == "CANNOT_EVICT"
    result = core.set_state("alpha", "gpu", evict=["large", "beta"], force=True)
    assert result["transition"]["evict"] == ["large"]
    assert result["transition"]["forced"] is True
    assert core.service_view("large")["state"] == "off"
    assert core.service_view("beta")["mode"] == "cpu"
    assert core.service_view("alpha")["mode"] == "gpu"


def test_cpu_profile_can_be_refused_without_removing_the_edge(core):
    with pytest.raises(SwitchError) as caught:
        core.set_state("large", "cpu")
    assert caught.value.status == 422
    assert caught.value.code == "STATE_NOT_SUPPORTED"
    core.set_state("large", "cpu", force=True)
    assert core.service_view("large")["mode"] == "cpu"


def test_missing_profile_is_not_a_missing_edge(tmp_path):
    core = make_core(tmp_path, [service("image", cpu=False, gpu_vram=20480, gpu_ram=32768)])
    view = core.service_view("image")
    assert view["supported_states"] == ["off", "gpu"]
    assert "cpu" in view["unavailable_states"]
    assert view["transitions_allowed"] == ["cpu", "gpu"]
    with pytest.raises(SwitchError) as caught:
        core.set_state("image", "cpu")
    assert caught.value.status == 422
    core.close()


def test_failed_gpu_start_restores_cpu(core):
    core.set_state("alpha", "cpu")
    core.runtime.fail_modes.add("gpu")
    with pytest.raises(SwitchError) as caught:
        core.set_state("alpha", "gpu")
    assert caught.value.code == "CONTAINER_EXITED"
    view = core.service_view("alpha")
    assert view["state"] == "ready"
    assert view["mode"] == "cpu"
    assert view["desired_state"] == "cpu"


def test_failed_start_from_off_returns_to_off(core):
    core.runtime.fail_modes.add("gpu")
    with pytest.raises(SwitchError):
        core.set_state("alpha", "gpu")
    view = core.service_view("alpha")
    assert view["state"] == "off"
    assert view["desired_state"] == "off"
    assert view["fail_sticky"] is False


def test_sticky_failure_is_not_reconciled_until_a_new_request(core):
    core.set_state("alpha", "cpu")
    core.runtime.fail_modes.update({"cpu", "gpu"})
    with pytest.raises(SwitchError):
        core.set_state("alpha", "gpu")
    view = core.service_view("alpha")
    assert view["state"] == "failed"
    assert view["fail_sticky"] is True
    core.runtime.fail_modes.clear()
    core.reconcile_once()
    info = core.runtime.inspect("alpha")
    assert info is None or info.running is False
    core.set_state("alpha", "cpu")
    assert core.service_view("alpha")["state"] == "ready"


def test_busy_while_a_transition_is_in_progress(core):
    core.runtime.gate = threading.Event()
    core.runtime.entered = threading.Event()
    errors: list[SwitchError] = []

    def other() -> None:
        assert core.runtime.entered.wait(2)
        try:
            core.set_state("alpha", "gpu")
        except SwitchError as exc:
            errors.append(exc)
        finally:
            core.runtime.gate.set()

    thread = threading.Thread(target=other)
    thread.start()
    core.set_state("alpha", "cpu")
    thread.join(3)
    assert not thread.is_alive()
    assert errors and errors[0].status == 423
    assert core.service_view("alpha")["mode"] == "cpu"


def test_reconcile_restarts_a_desired_service_and_stops_a_stray_one(core):
    core.set_state("alpha", "gpu")
    core.runtime.stop("alpha", 1)
    core.reconcile_once()
    assert core.runtime.inspect("alpha").running is True

    core.set_state("alpha", "off")
    alpha = core.catalog.get("alpha")
    spec = core._container_spec(alpha, alpha.profiles["cpu"], recreate=False)
    core.runtime.ensure(spec)
    core.runtime.start(spec)
    assert core.runtime.inspect("alpha").running is True
    core.reconcile_once()
    assert core.runtime.inspect("alpha").running is False


def test_references_increase_the_vram_claim(tmp_path):
    core = make_core(
        tmp_path,
        [service("image", cpu=False, gpu_vram=20000, gpu_ram=1024, vram_per_reference=2048)],
    )
    core.set_state("image", "gpu", options={"references": 4})
    view = core.service_view("image")
    assert view["options"] == {"references": 4}
    assert view["claim"]["vram_mib"] == 20000 + 2048 * 4
    with pytest.raises(SwitchError) as caught:
        core.set_state("image", "gpu", options={"references": 11})
    assert caught.value.code == "BAD_OPTIONS"
    core.close()


def test_drift_is_repaired_by_requesting_the_same_state(core):
    core.set_state("alpha", "cpu")
    core.runtime.containers["alpha"].hash_override = "stale"
    assert core.service_view("alpha")["drift"] is True
    core.set_state("alpha", "cpu")
    assert core.runtime.inspect("alpha").config_hash != "stale"
    assert core.service_view("alpha")["drift"] is False


def test_snapshot_has_no_secret_and_logs_are_redacted(core):
    core.catalog.secret_values = ("llm-test-secret-value",)
    core.runtime.logs_text["alpha"] = (
        "started --api-key llm-test-secret-value\nAuthorization: Bearer abcdefgh"
    )
    core.set_state("alpha", "cpu")
    dumped = json.dumps(core.snapshot())
    assert "llm-test-secret-value" not in dumped
    redacted = core.logs("alpha")
    assert "llm-test-secret-value" not in redacted
    assert "abcdefgh" not in redacted
    assert "…[redacted]…" in redacted


def test_unknown_service_and_bad_state(core):
    with pytest.raises(SwitchError) as missing:
        core.set_state("nope", "cpu")
    assert missing.value.status == 404
    with pytest.raises(SwitchError) as bad:
        core.set_state("alpha", "tpu")
    assert bad.value.code == "BAD_STATE"
