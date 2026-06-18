"""Slice-2 engine integration: the guardian is created, leased by wizard step, toggled, and the
manual Fix routes through it (no separate racing recipe path). Headless via fakes + monkeypatched
macaudio so it runs on any platform.
"""
from __future__ import annotations

import asyncio

import pytest

import fakes
from gui_bridge import engine as engine_mod
from gui_bridge import guardian as guardian_mod
from gui_bridge.engine import GuiEngine


class FakeMac:
    """Minimal macaudio stand-in: supported, buds present + stereo, no holders."""

    EarbudGroup = guardian_mod  # unused; real EarbudGroup comes from the engine's classify

    def __init__(self):
        self.set_default_calls = []
        self.parks = 0
        self.reconnects = 0
        from gui_bridge.macaudio import EarbudGroup
        self._group = EarbudGroup("bud-a2dp", "bud-hfp", 200, 201, "related")

    def is_supported(self):
        return True

    def classify_earbuds(self, _s):
        return self._group

    def output_channels_for_uid(self, uid):
        return 2 if uid == "bud-a2dp" else 1

    def default_input_uid(self):
        return "dji"

    def input_uid_for_name(self, _s):
        return "dji"

    def earbud_input_holders(self, _uid):
        return []

    def set_default_uid(self, kind, uid):
        self.set_default_calls.append((kind, uid))
        return True

    def park_recipe(self, *a, **k):
        self.parks += 1
        return {"ok": True, "channels": 2}

    def reconnect_recipe(self, *a, **k):
        self.reconnects += 1
        return {"ok": True, "channels": 2}

    def running_input_process_names(self):
        return []


@pytest.fixture
def fake_mac(monkeypatch):
    fm = FakeMac()
    monkeypatch.setattr(engine_mod, "macaudio", fm)
    monkeypatch.setattr(guardian_mod, "DEBOUNCE_MS", 0, raising=False)
    # DefaultAudioOps imports macaudio lazily inside __init__; patch that module ref too.
    import gui_bridge.macaudio as real_mac
    for name in ("output_channels_for_uid", "default_input_uid", "input_uid_for_name",
                 "earbud_input_holders", "set_default_uid", "park_recipe", "reconnect_recipe"):
        monkeypatch.setattr(real_mac, name, getattr(fm, name))
    monkeypatch.setattr(real_mac, "is_supported", fm.is_supported)
    return fm


class _Runtime(fakes.FakeAudioRuntime):
    output_available = True

    def start(self):
        pass

    def stop(self):
        pass


def _engine(monkeypatch, enabled=True):
    # avoid touching the user's real settings file (only patch the getter; tests patch the setter)
    monkeypatch.setattr(engine_mod.settings, "get_guard_enabled", lambda default=True: enabled)
    return GuiEngine(live_events_fn=fakes.fake_live_events,
                     runtime_factory=lambda *a: _Runtime())


@pytest.mark.asyncio
async def test_guardian_created_and_leased_by_step(fake_mac, monkeypatch):
    e = _engine(monkeypatch)
    e.start_guardian()
    assert e.guardian is not None and e.guardian.state.value == "passive"
    await e.goto_step("outputs")
    await asyncio.sleep(0.05)
    assert e.guardian.state.value == "armed"
    await e.goto_step("inputs")
    await asyncio.sleep(0.05)
    assert e.guardian.state.value == "passive"
    await e.shutdown()


@pytest.mark.asyncio
async def test_toggle_disables_and_persists(fake_mac, monkeypatch):
    saved = {}
    monkeypatch.setattr(engine_mod.settings, "set_guard_enabled", lambda v: saved.update(v=v))
    e = _engine(monkeypatch)
    e.start_guardian()
    await e.set_guard_enabled(False)
    await asyncio.sleep(0.05)
    assert e.guardian.state.value == "disabled"
    assert saved == {"v": False}
    assert e.state.guardian["enabled"] is False
    await e.shutdown()


@pytest.mark.asyncio
async def test_fix_earbuds_routes_through_guardian(fake_mac, monkeypatch):
    e = _engine(monkeypatch)
    e.start_guardian()
    await e.goto_step("outputs")     # arm so the lease allows recovery
    await asyncio.sleep(0.05)
    await e.fix_earbuds()            # should post MANUAL_FIX, not run the recipe inline
    await asyncio.sleep(0.1)
    assert fake_mac.parks == 1        # the guardian ran park exactly once
    await e.shutdown()


@pytest.mark.asyncio
async def test_live_start_stop_posts_lease(fake_mac, monkeypatch):
    e = _engine(monkeypatch)
    e.start_guardian()
    await e.start_live()
    await asyncio.sleep(0.05)
    assert e.guardian.state.value == "live"
    await e.stop_live()
    await asyncio.sleep(0.05)
    assert e.guardian.state.value in ("passive", "armed")
    await e.shutdown()
