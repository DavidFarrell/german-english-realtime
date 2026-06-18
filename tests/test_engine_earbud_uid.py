"""Slice-1 engine tests: the mono check + holder naming prefer UID-resolved earbud identity.

We monkeypatch gui_bridge.engine.macaudio so no CoreAudio is touched, and use the headless engine
(fake live_events + fake runtime) so nothing opens hardware.
"""
from __future__ import annotations

import pytest

import fakes
from gui_bridge import engine as engine_mod
from gui_bridge.engine import GuiEngine
from gui_bridge import macaudio


class FakeMac:
    """Stand-in for the macaudio module with the small surface the engine uses in Slice 1."""

    def __init__(self, *, group=None, out_ch_by_uid=None, holders=None, any_mic=None):
        self.group = group
        self.out_ch_by_uid = out_ch_by_uid or {}
        self.holders = holders or []
        self.any_mic = any_mic or []
        self.holder_calls: list[str] = []

    def is_supported(self):
        return True

    def classify_earbuds(self, _substr):
        return self.group

    def output_channels_for_uid(self, uid):
        return self.out_ch_by_uid.get(uid, 0)

    def earbud_input_holders(self, input_uid):
        self.holder_calls.append(input_uid)
        return list(self.holders)

    def running_input_process_names(self):
        return list(self.any_mic)


def _engine():
    return GuiEngine(live_events_fn=fakes.fake_live_events,
                     runtime_factory=lambda *_: fakes.FakeAudioRuntime())


def _group(output_uid="bud-a2dp", input_uid="bud-hfp"):
    return macaudio.EarbudGroup(output_uid=output_uid, input_uid=input_uid,
                                output_obj=200, input_obj=201, via="related")


def test_resolve_earbud_group_caches(monkeypatch):
    fake = FakeMac(group=_group())
    monkeypatch.setattr(engine_mod, "macaudio", fake)
    e = _engine()
    g1 = e.resolve_earbud_group()
    assert g1 is not None and g1.output_uid == "bud-a2dp"
    # second call uses the cache: swap the fake's group and confirm we keep the old one
    fake.group = _group(output_uid="other")
    assert e.resolve_earbud_group().output_uid == "bud-a2dp"
    assert e.resolve_earbud_group(force=True).output_uid == "other"


def test_resolve_keeps_known_hfp_when_rescan_sees_output_only(monkeypatch):
    fake = FakeMac(group=_group(input_uid="bud-hfp"))
    monkeypatch.setattr(engine_mod, "macaudio", fake)
    e = _engine()
    assert e.resolve_earbud_group().input_uid == "bud-hfp"
    # a later scan can only see the output node (buds mid-collapse) -> keep the known HFP UID
    fake.group = macaudio.EarbudGroup("bud-a2dp", None, 200, None, "output-only")
    g = e.resolve_earbud_group(force=True)
    assert g.input_uid == "bud-hfp" and g.output_uid == "bud-a2dp"


def test_mono_check_by_uid_true(monkeypatch):
    # A2DP node reads 1ch by UID -> mono, regardless of the index-based device list.
    fake = FakeMac(group=_group(), out_ch_by_uid={"bud-a2dp": 1})
    monkeypatch.setattr(engine_mod, "macaudio", fake)
    e = _engine()
    e.state.output.index = 7
    e.state.output_devices = [{"index": 7, "name": "David's Eggies", "outCh": 2}]  # index says stereo
    assert e._output_mono_device() is not None  # UID view wins: it's actually mono


def test_mono_check_by_uid_false(monkeypatch):
    fake = FakeMac(group=_group(), out_ch_by_uid={"bud-a2dp": 2})
    monkeypatch.setattr(engine_mod, "macaudio", fake)
    e = _engine()
    e.state.output.index = 7
    e.state.output_devices = [{"index": 7, "name": "David's Eggies", "outCh": 1}]  # index says mono
    assert e._output_mono_device() is None  # UID view wins: it's actually stereo


def test_mono_check_falls_back_to_index_when_uid_unavailable(monkeypatch):
    # buds absent (output_channels_for_uid -> 0) => UID view is None => use the index device list.
    fake = FakeMac(group=_group(), out_ch_by_uid={})
    monkeypatch.setattr(engine_mod, "macaudio", fake)
    e = _engine()
    e.state.output.index = 7
    e.state.output_devices = [{"index": 7, "name": "David's Eggies", "outCh": 1}]
    assert e._output_mono_device() is not None  # index says mono, UID couldn't read


def test_holder_uses_hfp_uid_not_any_mic(monkeypatch):
    fake = FakeMac(group=_group(input_uid="bud-hfp"),
                   holders=["Sound"], any_mic=["replayd"])
    monkeypatch.setattr(engine_mod, "macaudio", fake)
    e = _engine()
    holder = e._earbud_holder()
    assert fake.holder_calls == ["bud-hfp"]                  # queried the EARBUD input specifically
    assert holder == "the Sound settings window"            # named from the HFP-input holders


def test_holder_falls_back_to_any_mic_without_hfp_uid(monkeypatch):
    group = macaudio.EarbudGroup("bud-a2dp", None, 200, None, "output-only")
    fake = FakeMac(group=group, holders=["Sound"], any_mic=["replayd"])
    monkeypatch.setattr(engine_mod, "macaudio", fake)
    e = _engine()
    holder = e._earbud_holder()
    assert fake.holder_calls == []                          # no HFP UID -> didn't query by-UID
    assert holder == "a screen / audio recording"          # fell back to any-mic names (replayd)
