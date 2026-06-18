"""Slice-2 unit tests: the AudioGuardian state machine. Fully headless - a FakeAudioOps scripts
snapshots and records mutations; no CoreAudio, no hardware, no threads-with-real-IO.

The guardian owner + poll are real asyncio tasks; we drive events via post() and pump the loop with
short sleeps. asyncio.to_thread of the (fake, instant) recipe callables works under pytest-asyncio.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import gui_bridge.guardian as gm
from gui_bridge.guardian import (
    AudioGuardian, AudioSnapshot, Event, GuardianStatus, State,
)


@pytest.fixture(autouse=True)
def _no_debounce(monkeypatch):
    # Tests pump the loop with asyncio.sleep(0) (no real time passes), so disable the cross-tick
    # debounce window; flicker-debounce timing is exercised in test_debounce_* explicitly.
    monkeypatch.setattr(gm, "DEBOUNCE_MS", 0)


class FakeAudioOps:
    def __init__(self) -> None:
        self.snap = AudioSnapshot(
            present=True, output_uid="bud-a2dp", input_uid="bud-hfp", out_ch=2,
            default_input_uid="dji", dji_input_uid="dji", holders=frozenset(), group_generation=1)
        self.corrections = 0
        self.parks = 0
        self.reconnects = 0
        self.park_result = {"ok": True, "channels": 2}
        self.reconnect_result = {"ok": True, "channels": 2}

    def snapshot(self) -> AudioSnapshot:
        return self.snap

    def set(self, **kw) -> None:
        self.snap = replace(self.snap, **kw)

    def correct_default_input(self) -> bool:
        self.corrections += 1
        # prevention re-points the default input back to the DJI (does NOT un-collapse a mono bud)
        self.set(default_input_uid="dji")
        return True

    def run_park_recipe(self) -> dict:
        self.parks += 1
        if self.park_result.get("ok"):
            self.set(out_ch=2)
        return self.park_result

    def run_reconnect_recipe(self) -> dict:
        self.reconnects += 1
        if self.reconnect_result.get("ok"):
            self.set(out_ch=2)
        return self.reconnect_result


async def _pump(n: int = 6) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _guard(ops, enabled=True):
    changes = []
    g = AudioGuardian(ops, GuardianStatus(), on_change=lambda: changes.append(1), enabled=enabled)
    return g, changes


@pytest.mark.asyncio
async def test_passive_never_mutates():
    ops = FakeAudioOps()
    ops.set(default_input_uid="bud-hfp")  # macOS grabbed the buds as default input
    g, _ = _guard(ops)
    g.run()
    g.post(Event.TICK)          # passive: poll wouldn't emit, but even a stray tick must no-op
    await _pump()
    assert ops.corrections == 0 and g.state == State.PASSIVE
    await g.stop()


@pytest.mark.asyncio
async def test_armed_prevention_corrects_default_input():
    ops = FakeAudioOps()
    ops.set(default_input_uid="bud-hfp")  # default input grabbed by the buds
    g, _ = _guard(ops)
    g.run()
    g.post(Event.ARM)
    await _pump()
    assert g.state == State.ARMED
    assert ops.corrections == 1                # re-pointed default input -> DJI (prevention)
    assert g.status.phase == "preventing"
    await g.stop()


@pytest.mark.asyncio
async def test_disabled_toggle_blocks_all_mutation():
    ops = FakeAudioOps()
    ops.set(default_input_uid="bud-hfp")
    g, _ = _guard(ops, enabled=False)
    g.run()
    g.post(Event.ARM)
    g.post(Event.TICK)
    await _pump()
    assert g.state == State.DISABLED and ops.corrections == 0
    # re-enabling resumes; with the lease intent tracked, it goes armed and prevents
    g.post(Event.SET_ENABLED, True)
    await _pump()
    g.post(Event.TICK)
    await _pump()
    assert g.state == State.ARMED and ops.corrections == 1
    await g.stop()


@pytest.mark.asyncio
async def test_already_mono_surfaces_needs_user_recovery():
    # An already-collapsed bud can't be un-collapsed by prevention -> surface immediately (no
    # autonomous park). Prevention (correct_default_input) is NOT run for a mono bud.
    ops = FakeAudioOps()
    ops.set(out_ch=1, default_input_uid="bud-hfp")  # already collapsed AND grabbed
    g, _ = _guard(ops)
    g.run()
    g.post(Event.LIVE_START)
    await _pump()
    assert g.state == State.NEEDS_USER_RECOVERY
    assert ops.corrections == 0
    assert g.status.persistent is True and g.status.actionable is True and g.status.action == "fix"
    await g.stop()


@pytest.mark.asyncio
async def test_external_holder_goes_blocked():
    ops = FakeAudioOps()
    ops.set(holders=frozenset({"zoom.us"}))
    g, _ = _guard(ops)
    g.run()
    g.post(Event.LIVE_START)
    await _pump()
    assert g.state == State.BLOCKED
    assert g.status.persistent is True and "zoom.us" in g.status.message
    assert ops.corrections == 0   # don't thrash when an external holder will win
    await g.stop()


@pytest.mark.asyncio
async def test_manual_fix_runs_park_only_when_park_succeeds():
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    g, _ = _guard(ops)
    g.run()
    g.post(Event.ARM)
    g.post(Event.MANUAL_FIX)
    await _pump(10)
    assert ops.parks == 1 and ops.reconnects == 0   # park took -> no reconnect
    await g.stop()


@pytest.mark.asyncio
async def test_manual_fix_escalates_to_reconnect_when_park_fails():
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    ops.park_result = {"ok": False, "channels": 1}   # park doesn't restore stereo
    g, _ = _guard(ops)
    g.run()
    g.post(Event.ARM)
    g.post(Event.MANUAL_FIX)
    await _pump(12)
    assert ops.parks == 1 and ops.reconnects == 1     # escalated to reconnect (user-tapped chain)
    await g.stop()


@pytest.mark.asyncio
async def test_manual_fix_never_auto():
    # The disruptive recipe must require an explicit MANUAL_FIX; armed ticks alone never park.
    ops = FakeAudioOps()
    ops.set(out_ch=1, default_input_uid="bud-hfp")
    g, _ = _guard(ops)
    g.run()
    g.post(Event.ARM)
    for _ in range(5):
        g.post(Event.TICK)
        await _pump()
    assert ops.parks == 0 and ops.reconnects == 0     # prevention only; never autonomous park
    await g.stop()


@pytest.mark.asyncio
async def test_recovery_success_exits_to_resting_not_stuck():
    # GPT-5 cp3 #1: a successful fix must leave recovering_* and return to the lease state.
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    g, _ = _guard(ops)
    g.run()
    g.post(Event.LIVE_START)
    g.post(Event.MANUAL_FIX)
    await _pump(12)
    assert ops.parks == 1 and ops.reconnects == 0
    assert g.state == State.LIVE          # back to the lease state, NOT recovering_park
    assert g.status.phase == "idle"
    await g.stop()


@pytest.mark.asyncio
async def test_manual_fix_without_lease_never_mutates():
    # GPT-5 cp3 #3: passive (no lease) -> a stale/stray Fix tap must not run the recipe.
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    g, _ = _guard(ops)
    g.run()
    g.post(Event.MANUAL_FIX)              # still passive, no arm/live
    await _pump(8)
    assert ops.parks == 0 and ops.reconnects == 0 and g.state == State.PASSIVE
    await g.stop()


@pytest.mark.asyncio
async def test_blocked_clears_when_holder_goes_away():
    # GPT-5 cp3 #2: a sticky surface must actually clear when the episode signature changes.
    ops = FakeAudioOps()
    ops.set(holders=frozenset({"zoom.us"}))
    g, _ = _guard(ops)
    g.run()
    g.post(Event.LIVE_START)
    await _pump()
    assert g.state == State.BLOCKED
    ops.set(holders=frozenset())          # holder quit -> new signature
    g.post(Event.TICK)
    await _pump()
    assert g.state == State.LIVE and g.status.phase == "idle"
    await g.stop()


@pytest.mark.asyncio
async def test_oscillation_blocks_after_threshold():
    # GPT-5 cp3 #7: >OSC_MAX corrections in the window -> blocked_oscillation (off-by-one correct).
    ops = FakeAudioOps()
    g, _ = _guard(ops)
    g.run()
    g.post(Event.ARM)
    await _pump()
    # repeatedly re-grab the default input so prevention corrects each time
    blocked = False
    for _ in range(gm.OSC_MAX_CORRECTIONS + 3):
        ops.set(default_input_uid="bud-hfp")   # macOS re-grabs the buds
        g.post(Event.TICK)
        await _pump()
        if g.state == State.BLOCKED:
            blocked = True
            break
    assert blocked
    assert ops.corrections == gm.OSC_MAX_CORRECTIONS   # exactly the budget, then block (not budget+1)
    assert g.status.action == "reconnect"
    await g.stop()


@pytest.mark.asyncio
async def test_prevention_repeats_across_distinct_grabs():
    # GPT-5 cp3 #8: a later genuine re-grab gets a fresh correction (prevention is NOT one-per-sig).
    ops = FakeAudioOps()
    g, _ = _guard(ops)
    g.run()
    g.post(Event.ARM)
    ops.set(default_input_uid="bud-hfp")
    g.post(Event.TICK)
    await _pump()
    assert ops.corrections == 1
    # prevention corrected default back to DJI; later macOS grabs again -> correct again
    ops.set(default_input_uid="bud-hfp")
    g.post(Event.TICK)
    await _pump()
    assert ops.corrections == 2
    await g.stop()


@pytest.mark.asyncio
async def test_surface_clear_emits_on_signature_change():
    # GPT-5 cp4 #2: leaving a sticky surface must emit so the renderer mirror updates.
    ops = FakeAudioOps()
    ops.set(holders=frozenset({"zoom.us"}))
    g, changes = _guard(ops)
    g.run()
    g.post(Event.LIVE_START)
    await _pump()
    assert g.state == State.BLOCKED
    n_before = len(changes)
    ops.set(holders=frozenset())   # holder gone -> new signature -> clear
    g.post(Event.TICK)
    await _pump()
    assert g.state == State.LIVE
    assert len(changes) > n_before   # emitted on clear


@pytest.mark.asyncio
async def test_buds_gone_clears_stale_surface_and_emits():
    # GPT-5 cp4 #2: when buds vanish, a stale surface must clear + emit (comment said "idle surface").
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    g, changes = _guard(ops)
    g.run()
    g.post(Event.ARM)
    await _pump()
    assert g.state == State.NEEDS_USER_RECOVERY
    ops.set(present=False, out_ch=0)
    n_before = len(changes)
    g.post(Event.TICK)
    await _pump()
    assert g.status.phase == "idle" and len(changes) > n_before


@pytest.mark.asyncio
async def test_fix_during_live_runs_full_escalation():
    # GPT-5 cp4 #1: a Fix tapped during live (lease=live) must still run park->reconnect even though
    # freeing the streams posts an internal LIVE_STOP - that internal stop must NOT veto reconnect.
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    ops.park_result = {"ok": False, "channels": 1}   # park fails -> needs reconnect
    g, _ = _guard(ops)
    live_was = {"v": False}

    async def before():
        # mimic the engine: freeing streams during recovery drops the live lease WITHOUT notify.
        # Here we simulate the WRONG behaviour (posting LIVE_STOP) NOT happening: lease stays.
        live_was["v"] = True

    g._before_recovery = before
    g.run()
    g.post(Event.LIVE_START)
    g.post(Event.MANUAL_FIX)
    await _pump(14)
    assert live_was["v"] is True
    assert ops.parks == 1 and ops.reconnects == 1   # full escalation ran (reconnect not vetoed)
    await g.stop()


@pytest.mark.asyncio
async def test_post_recovery_mono_not_surfaced_when_disabled_midway():
    # GPT-5 cp4 #3: if the user disables protection during the recipe, a still-mono bud afterwards
    # must NOT leave an actionable needs_user_recovery surface (its button would no-op).
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    ops.park_result = {"ok": False, "channels": 1}
    ops.reconnect_result = {"ok": False, "channels": 1}  # stays mono after recovery
    g, _ = _guard(ops)

    async def before():
        g.post(Event.SET_ENABLED, False)   # user disables mid-recipe

    g._before_recovery = before
    g.run()
    g.post(Event.ARM)
    g.post(Event.MANUAL_FIX)
    await _pump(16)
    assert g.state != State.NEEDS_USER_RECOVERY
    assert g.status.actionable is False
    await g.stop()


@pytest.mark.asyncio
async def test_disarm_during_reconnect_clears_surface():
    # GPT-5 cp4b: a DISARM that queues WHILE reconnect is in flight must be applied before the final
    # decision, so a still-mono bud afterwards does NOT leave a dead actionable surface.
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    ops.park_result = {"ok": False, "channels": 1}      # force reconnect
    ops.reconnect_result = {"ok": False, "channels": 1}  # stays mono after recovery
    g, _ = _guard(ops)

    orig_reconnect = ops.run_reconnect_recipe

    def reconnect_then_disarm():
        g.post(Event.DISARM)   # user leaves the armed step DURING reconnect
        return orig_reconnect()

    ops.run_reconnect_recipe = reconnect_then_disarm
    g.run()
    g.post(Event.ARM)
    g.post(Event.MANUAL_FIX)
    await _pump(18)
    assert g.state != State.NEEDS_USER_RECOVERY   # lease gone -> no dead actionable surface
    assert g.status.actionable is False
    await g.stop()


@pytest.mark.asyncio
async def test_duplicate_manual_fix_does_not_double_run():
    # GPT-5 cp3 #4: a double-tap queued during the recipe must not launch a second recipe.
    ops = FakeAudioOps()
    ops.set(out_ch=1)
    g, _ = _guard(ops)
    g.run()
    g.post(Event.ARM)
    g.post(Event.MANUAL_FIX)
    g.post(Event.MANUAL_FIX)             # duplicate tap
    await _pump(14)
    assert ops.parks == 1                 # exactly one recipe ran
    await g.stop()
