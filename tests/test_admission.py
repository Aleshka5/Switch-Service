# SPDX-License-Identifier: Apache-2.0
import pytest

from switch_service.admission import Holder, Sample, admit, live_free_vram
from switch_service.errors import SwitchError

RESERVE_RAM = 8192
RESERVE_VRAM = 1536
TOTAL_RAM = 90112
TOTAL_VRAM = 32768


def sample(ram_available: int = 70000, vram_used: int = 440) -> Sample:
    return Sample(
        ram_total_mib=TOTAL_RAM,
        ram_available_mib=ram_available,
        vram_total_mib=TOTAL_VRAM,
        vram_used_mib=vram_used,
    )


def decide(holders, *, claim_vram=0, claim_ram=0, evict=None, force=False, target="new", **sample_kwargs):
    preemptible = {holder.id: holder.id != "large" for holder in holders}
    preemptible.setdefault(target, True)
    return admit(
        subject=target,
        target_id=target,
        holders=holders,
        known_ids={holder.id for holder in holders} | {target, *(evict or [])},
        preemptible=preemptible,
        sample=sample(**sample_kwargs),
        ram_reserve_mib=RESERVE_RAM,
        vram_reserve_mib=RESERVE_VRAM,
        claim_ram_mib=claim_ram,
        claim_vram_mib=claim_vram,
        evict=evict or [],
        force=force,
    )


def test_lazy_gpu_claim_blocks_a_second_model_even_when_smi_is_idle():
    # Image holds a 20 GiB claim but nvidia-smi still shows the idle 440 MiB.
    # Scheduling from `used` alone would let a 30 GiB LLM in beside it.
    image = Holder("image", reserve_ram_mib=32768, reserve_vram_mib=20480, measured_vram_mib=0)
    decision = decide([image], claim_vram=30720, claim_ram=2048, target="llm")
    assert decision.ok is False
    assert decision.error is not None
    assert decision.error.code == "INSUFFICIENT_VRAM"
    suggestion = decision.error.details["suggestions"][0]
    assert suggestion["evict"] == ["image"]
    assert suggestion["enough"] is True


def test_evicting_the_lazy_model_frees_the_claim_not_the_idle_reading():
    image = Holder("image", reserve_ram_mib=32768, reserve_vram_mib=20480, measured_vram_mib=0)
    decision = decide([image], claim_vram=30720, claim_ram=2048, evict=["image"], target="llm")
    assert decision.ok is True
    assert decision.evict == ("image",)


def test_live_vram_rejects_unmanaged_usage_the_ledger_cannot_see():
    decision = decide([], claim_vram=20000, claim_ram=512, vram_used=20000, target="llm")
    assert decision.ok is False
    assert decision.error is not None
    assert decision.error.code == "INSUFFICIENT_VRAM"
    assert decision.error.details["resource"] == "vram"


def test_live_free_vram_counts_unmanaged_usage_once():
    # The claim is the floor for a managed service; whatever nvidia-smi shows
    # above the sum of weights belongs to nobody and is subtracted as is.
    image = Holder("image", reserve_ram_mib=1024, reserve_vram_mib=20480)
    assert live_free_vram([image], sample(vram_used=20000), RESERVE_VRAM) == 32768 - 1536 - 20480
    assert live_free_vram([image], sample(vram_used=28000), RESERVE_VRAM) == 32768 - 1536 - 28000
    # Freeing the holder releases its claim, not the unmanaged part.
    freed = live_free_vram([image], sample(vram_used=28000), RESERVE_VRAM, {"image"})
    assert freed == 32768 - 1536 - (28000 - 20480)


def test_ram_claims_are_not_subtracted_from_memavailable():
    # Image already accounts for its RSS inside MemAvailable. Subtracting the
    # claim again would refuse a 1 GiB neighbour that easily fits.
    image = Holder("image", reserve_ram_mib=32768, reserve_vram_mib=0, measured_ram_mib=30000)
    decision = decide([image], claim_ram=1024, claim_vram=0, ram_available=40000, target="ocr")
    assert decision.ok is True


def test_ledger_still_rejects_two_large_claims_that_exceed_capacity():
    image = Holder("image", reserve_ram_mib=1024, reserve_vram_mib=20480, measured_vram_mib=20000)
    decision = decide([image], claim_vram=3072, claim_ram=512, vram_used=20000, target="ocr")
    assert decision.ok is True
    too_big = decide([image], claim_vram=30720, claim_ram=2048, vram_used=20000, target="llm")
    assert too_big.ok is False


def test_force_does_not_skip_the_budget():
    decision = decide([], claim_vram=40000, claim_ram=512, force=True, target="llm")
    assert decision.ok is False
    assert decision.error is not None
    assert decision.error.code == "INSUFFICIENT_VRAM"


def test_non_preemptible_needs_force_and_is_not_stopped_when_it_does_not_help():
    large = Holder("large", reserve_ram_mib=2048, reserve_vram_mib=30720)
    with pytest.raises(SwitchError) as caught:
        decide([large], claim_vram=4096, claim_ram=512, evict=["large"], target="alpha")
    assert caught.value.code == "CANNOT_EVICT"
    allowed = decide(
        [large], claim_vram=4096, claim_ram=512, evict=["large"], force=True, target="alpha"
    )
    assert allowed.ok is True
    assert allowed.evict == ("large",)
