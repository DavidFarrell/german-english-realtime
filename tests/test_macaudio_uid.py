"""Slice-1 unit tests: UID-based earbud-group classifier + holder ranking. No hardware.

We never touch real CoreAudio: classify_earbuds / output_channels_for_uid / _device_by_uid all
accept an injected `infos` list of fake DeviceInfo tuples, so the grouping logic is fully headless.
"""
from __future__ import annotations

from gui_bridge import macaudio as m

BT = m.TRANSPORT_BLUETOOTH
BTLE = m.TRANSPORT_BLUETOOTH_LE
BUILTIN = 0x626C746E  # 'bltn'
VIRTUAL = 0x76697274  # 'virt'


def _d(obj, uid, name, in_ch, out_ch, *, tran=BT, maker="Acme", model="EGG", related=()):
    return m.DeviceInfo(obj=obj, uid=uid, name=name, manufacturer=maker, model=model,
                        transport=tran, in_ch=in_ch, out_ch=out_ch, related=related)


def _bud_pair(related=True):
    a2dp = _d(200, "bud-a2dp", "David's Eggies", 0, 2, related=(200, 201) if related else ())
    hfp = _d(201, "bud-hfp", "David's Eggies", 1, 1, related=(201, 200) if related else ())
    builtin = _d(141, "BuiltInMic", "MacBook Pro Microphone", 3, 0, tran=BUILTIN, maker="Apple")
    return [a2dp, hfp, builtin]


def test_classify_related_primary_resolves_hfp_sibling():
    g = m.classify_earbuds("Eggies", _bud_pair(related=True))
    assert g is not None and g.via == "related"
    assert g.output_uid == "bud-a2dp" and g.input_uid == "bud-hfp"


def test_classify_fallback_when_no_related_links():
    g = m.classify_earbuds("Eggies", _bud_pair(related=False))
    assert g is not None and g.via == "fallback"
    assert g.output_uid == "bud-a2dp" and g.input_uid == "bud-hfp"


def test_classify_absent_returns_none():
    builtin = _d(141, "BuiltInMic", "MacBook Pro Microphone", 3, 0, tran=BUILTIN, maker="Apple")
    assert m.classify_earbuds("Eggies", [builtin]) is None


def test_classify_rejects_non_bluetooth_output():
    # A virtual 2ch device named like the buds must NOT be taken as the A2DP node.
    virt = _d(96, "BlackHole", "David's Eggies (virtual)", 2, 2, tran=VIRTUAL)
    assert m.classify_earbuds("Eggies", [virt]) is None


def test_classify_collapsed_buds_do_not_persist_wrong_uid():
    # When already in HFP mono (out_ch=1), there's no >=2ch BT output, so we refuse to classify
    # rather than persist the 1ch node as the A2DP output UID (GPT-5 cp1 must-fix #1).
    a2dp = _d(200, "bud-a2dp", "David's Eggies", 0, 1, related=(200, 201))
    hfp = _d(201, "bud-hfp", "David's Eggies", 1, 1, related=(201, 200))
    assert m.classify_earbuds("Eggies", [a2dp, hfp]) is None


def test_classify_ranks_related_inputs_prefers_1ch_hfp():
    # RelatedDevices has no order: a 2ch sibling listed first must NOT win over the 1ch HFP mic.
    a2dp = _d(200, "bud-a2dp", "David's Eggies", 0, 2, related=(200, 202, 201))
    multi = _d(202, "bud-multi", "David's Eggies", 2, 2)   # combined node, listed before HFP
    hfp = _d(201, "bud-hfp", "David's Eggies", 1, 1)
    g = m.classify_earbuds("Eggies", [a2dp, multi, hfp])
    assert g is not None and g.input_uid == "bud-hfp"      # the 1ch HFP node, deterministically


def test_classify_fallback_ambiguous_containment_is_rejected():
    # Two different BT inputs that only CONTAIN the needle (no exact name) and don't share maker/model
    # must not be guessed between - fall through to output-only (GPT-5 cp1 must-fix #3).
    a2dp = _d(200, "bud-a2dp", "AirPods Max", 0, 2, maker="Apple", model="MAX")
    other1 = _d(210, "x1", "AirPods Pro left", 1, 1, maker="Apple", model="PRO")
    other2 = _d(211, "x2", "AirPods Pro right", 1, 1, maker="Apple", model="PRO")
    g = m.classify_earbuds("AirPods", [a2dp, other1, other2])
    assert g is not None and g.via == "output-only" and g.input_uid is None


def test_output_channels_for_uid():
    infos = _bud_pair()
    assert m.output_channels_for_uid("bud-a2dp", infos) == 2
    assert m.output_channels_for_uid("bud-hfp", infos) == 1
    assert m.output_channels_for_uid("missing", infos) == 0


def test_device_by_uid():
    infos = _bud_pair()
    assert m._device_by_uid("bud-hfp", infos).obj == 201
    assert m._device_by_uid("nope", infos) is None
    assert m._device_by_uid("", infos) is None
