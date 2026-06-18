"""macOS audio helpers (CoreAudio via ctypes) + a one-shot Bluetooth-earbud un-stick recipe.

Why this exists: BT earbuds that advertise a mic (HFP/HSP) get auto-selected by macOS as the
*default input* on connect, and the moment anything reads the default mic (the Sound settings pane,
a screen/audio recorder, a call app) the buds collapse from A2DP stereo (out 2) to HFP mono call
mode (out 1). DebbieDavidApp needs them in stereo to play German->left / English->right. This module
(a) names the process actually holding a mic, so the toast can say WHO, and (b) un-sticks the buds
by parking both default routes off them so the SCO link drops and they renegotiate A2DP, then hands
output back. Pure ctypes - no compiled helper, no extra Python deps. Every call is defensive: any
CoreAudio failure degrades to a neutral result so the bridge never crashes on it.

macOS only (the process-list selectors are 14.4+). Callers guard with `is_supported()`.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import shutil
import subprocess
import time
from typing import NamedTuple

# -- ctypes bindings ------------------------------------------------------------------------------

_IS_MAC = platform.system() == "Darwin"


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


# Selectors / scopes (CoreAudio constants).
kAudioObjectSystemObject = 1
kAudioObjectPropertyElementMain = 0
SCOPE_GLOBAL = _fourcc("glob")
SCOPE_INPUT = _fourcc("inpt")
SCOPE_OUTPUT = _fourcc("outp")
PROP_DEVICES = _fourcc("dev#")
PROP_DEFAULT_INPUT = _fourcc("dIn ")
PROP_DEFAULT_OUTPUT = _fourcc("dOut")
PROP_PROCESS_LIST = _fourcc("prs#")
PROP_PROC_RUNNING_INPUT = _fourcc("piri")
PROP_PROC_PID = _fourcc("ppid")
PROP_PROC_DEVICES = _fourcc("pdv#")     # kAudioProcessPropertyDevices (per-process device set)
PROP_NAME = _fourcc("lnam")
PROP_STREAM_CONFIG = _fourcc("slay")
PROP_DEVICE_UID = _fourcc("uid ")       # kAudioDevicePropertyDeviceUID (CFString)
PROP_TRANSPORT_TYPE = _fourcc("tran")   # kAudioDevicePropertyTransportType (UInt32)
PROP_MANUFACTURER = _fourcc("lmak")     # kAudioObjectPropertyManufacturer (CFString)
PROP_MODEL_UID = _fourcc("muid")        # kAudioDevicePropertyModelUID (CFString)
PROP_RELATED_DEVICES = _fourcc("akin")  # kAudioDevicePropertyRelatedDevices (array of AudioObjectID)
TRANSPORT_BLUETOOTH = _fourcc("blue")   # kAudioDeviceTransportTypeBluetooth
TRANSPORT_BLUETOOTH_LE = _fourcc("blea") # kAudioDeviceTransportTypeBluetoothLE


class _Addr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class _AudioBufferList(ctypes.Structure):
    # mBuffers is 8-byte aligned (it holds a pointer), so ctypes places it at offset 8,
    # matching the C layout - the manual-offset version got this wrong.
    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", _AudioBuffer * 1)]


def _addr(selector: int, scope: int = SCOPE_GLOBAL) -> _Addr:
    return _Addr(selector, scope, kAudioObjectPropertyElementMain)


if _IS_MAC:
    _ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
    _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    _libc = ctypes.CDLL(None)

    _ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    _ca.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    _ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    _ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
    _ca.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]

    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]

    _libc.proc_pidpath.restype = ctypes.c_int
    _libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]


def is_supported() -> bool:
    return _IS_MAC


# -- low-level property reads ---------------------------------------------------------------------

def _get_array(obj: int, selector: int, ctype, scope: int = SCOPE_GLOBAL) -> list:
    """Read a variable-length array property into a list of `ctype`."""
    size = ctypes.c_uint32(0)
    a = _addr(selector, scope)
    if _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(a), 0, None, ctypes.byref(size)) != 0:
        return []
    n = size.value // ctypes.sizeof(ctype)
    if n == 0:
        return []
    buf = (ctype * n)()
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), buf) != 0:
        return []
    return list(buf)


def _get_scalar(obj: int, selector: int, ctype):
    val = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(ctype))
    a = _addr(selector)
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), ctypes.byref(val)) != 0:
        return None
    return val.value


def _cfstring_prop(obj: int, selector: int, scope: int = SCOPE_GLOBAL) -> str:
    """Read a CFString device/object property (name, UID, manufacturer, model) as a Python str."""
    cfstr = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    a = _addr(selector, scope)
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), ctypes.byref(cfstr)) != 0 or not cfstr:
        return ""
    buf = ctypes.create_string_buffer(512)
    ok = _cf.CFStringGetCString(cfstr, buf, 512, 0x08000100)  # kCFStringEncodingUTF8
    _cf.CFRelease(cfstr)
    return buf.value.decode("utf-8", "replace") if ok else ""


def _device_name(dev: int) -> str:
    return _cfstring_prop(dev, PROP_NAME)


def _device_uid(dev: int) -> str:
    return _cfstring_prop(dev, PROP_DEVICE_UID)


def _channels(dev: int, scope: int) -> int:
    """Sum the channels of a device's stream config in `scope` (input or output)."""
    size = ctypes.c_uint32(0)
    a = _addr(PROP_STREAM_CONFIG, scope)
    if _ca.AudioObjectGetPropertyDataSize(dev, ctypes.byref(a), 0, None, ctypes.byref(size)) != 0:
        return 0
    if size.value < ctypes.sizeof(_AudioBufferList):
        return 0
    buf = (ctypes.c_byte * size.value)()
    if _ca.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), buf) != 0:
        return 0
    abl = ctypes.cast(buf, ctypes.POINTER(_AudioBufferList)).contents
    n = abl.mNumberBuffers
    if n == 0:
        return 0
    base = ctypes.addressof(abl) + _AudioBufferList.mBuffers.offset
    buffers = (_AudioBuffer * n).from_address(base)
    return sum(int(b.mNumberChannels) for b in buffers)


# -- public queries -------------------------------------------------------------------------------

def _proc_name(pid: int) -> str:
    buf = ctypes.create_string_buffer(4096)
    if _libc.proc_pidpath(pid, buf, 4096) > 0:
        return buf.value.decode("utf-8", "replace").rsplit("/", 1)[-1]
    return f"pid {pid}"


def running_input_process_names() -> list[str]:
    """Names of processes currently holding an input (mic) stream open. macOS 14.4+; [] otherwise."""
    if not _IS_MAC:
        return []
    try:
        names = []
        for proc in _get_array(kAudioObjectSystemObject, PROP_PROCESS_LIST, ctypes.c_uint32):
            running = _get_scalar(proc, PROP_PROC_RUNNING_INPUT, ctypes.c_uint32)
            if running:
                pid = _get_scalar(proc, PROP_PROC_PID, ctypes.c_int32)
                if pid:
                    names.append(_proc_name(pid))
        return names
    except Exception:
        return []


def _all_devices() -> list[tuple[int, str, int, int]]:
    out = []
    for dev in _get_array(kAudioObjectSystemObject, PROP_DEVICES, ctypes.c_uint32):
        out.append((dev, _device_name(dev),
                    _channels(dev, SCOPE_INPUT), _channels(dev, SCOPE_OUTPUT)))
    return out


class DeviceInfo(NamedTuple):
    """A CoreAudio device's stable identity + capabilities (everything the classifier needs)."""
    obj: int            # AudioObjectID (volatile across reconnects - never persist this)
    uid: str            # kAudioDevicePropertyDeviceUID (stable across reconnects - persist THIS)
    name: str
    manufacturer: str
    model: str
    transport: int      # kAudioDevicePropertyTransportType
    in_ch: int
    out_ch: int
    related: tuple[int, ...]  # RelatedDevices obj-ids (siblings of the same physical endpoint)


def _enumerate() -> list[DeviceInfo]:
    """Full device snapshot with UID/transport/manufacturer/model/related, for UID-based logic."""
    infos: list[DeviceInfo] = []
    for dev in _get_array(kAudioObjectSystemObject, PROP_DEVICES, ctypes.c_uint32):
        infos.append(DeviceInfo(
            obj=dev,
            uid=_device_uid(dev),
            name=_device_name(dev),
            manufacturer=_cfstring_prop(dev, PROP_MANUFACTURER),
            model=_cfstring_prop(dev, PROP_MODEL_UID),
            transport=_get_scalar(dev, PROP_TRANSPORT_TYPE, ctypes.c_uint32) or 0,
            in_ch=_channels(dev, SCOPE_INPUT),
            out_ch=_channels(dev, SCOPE_OUTPUT),
            related=tuple(_get_array(dev, PROP_RELATED_DEVICES, ctypes.c_uint32)),
        ))
    return infos


def output_channels(name_substr: str) -> int:
    """Max output channels of the (highest-channel) device whose name contains `name_substr`."""
    if not _IS_MAC:
        return 2
    best = 0
    for _id, name, _inc, outc in _all_devices():
        if name_substr.lower() in name.lower():
            best = max(best, outc)
    return best


# -- UID-based identity + the earbud-group classifier ---------------------------------------------

class EarbudGroup(NamedTuple):
    """The resolved BT-earbud endpoint: its A2DP (stereo out) node and its HFP (mono in) sibling.

    Both UIDs are persisted once confirmed (PLAN: misidentifying the HFP sibling is the single
    biggest risk). `via` records how the sibling was found, for logging/tests."""
    output_uid: str         # the A2DP node we play stereo into
    input_uid: str | None   # the HFP mic node that, if opened, collapses output to mono
    output_obj: int
    input_obj: int | None
    via: str                # "related" | "fallback" | "output-only"


def _is_bluetooth(info: DeviceInfo) -> bool:
    return info.transport in (TRANSPORT_BLUETOOTH, TRANSPORT_BLUETOOTH_LE)


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def classify_earbuds(output_substr: str,
                     infos: list[DeviceInfo] | None = None) -> EarbudGroup | None:
    """Resolve the BT-earbud A2DP output node and its HFP input sibling, by UID.

    PLAN (the one hard sign-off requirement): RelatedDevices is PRIMARY but not authoritative;
    a validated fallback uses Bluetooth transport + matching name/manufacturer/model + complementary
    in/out capability. UID-stem matching is a last-resort heuristic only. Persist BOTH UIDs.

    Returns None if no matching stereo-output device is present.
    """
    if infos is None:
        infos = _enumerate()
    needle = output_substr.lower()
    # The A2DP node MUST be a Bluetooth, stereo-capable (>=2 out ch) device whose name matches.
    # (GPT-5 cp1 must-fix: accepting out_ch>0 could persist the 1ch HFP output UID as our A2DP node
    # when the buds are already collapsed or only the HFP profile is visible.)
    out_cands = [d for d in infos
                 if needle in d.name.lower() and d.out_ch >= 2 and _is_bluetooth(d)]
    if not out_cands:
        return None
    a2dp = max(out_cands, key=lambda d: d.out_ch)
    by_obj = {d.obj: d for d in infos}

    def _hfp_rank(d: DeviceInfo) -> tuple:
        # Best HFP mic sibling: input-capable, Bluetooth, fewest output channels (1ch HFP preferred),
        # exactly-mono input preferred. RelatedDevices has NO ordering guarantee, so rank explicitly.
        return (0 if d.in_ch == 1 else 1, d.out_ch, -d.in_ch)

    # 1. PRIMARY: rank the RelatedDevices input siblings deterministically (not "first").
    rel_inputs = [by_obj[o] for o in a2dp.related
                  if o in by_obj and by_obj[o].obj != a2dp.obj and by_obj[o].in_ch > 0]
    if rel_inputs:
        sib = min(rel_inputs, key=_hfp_rank)
        return EarbudGroup(a2dp.uid, sib.uid, a2dp.obj, sib.obj, "related")

    # 2. VALIDATED FALLBACK: Bluetooth + identity match + complementary input capability. Strong path
    # is normalised EXACT name equality; loose containment only if it yields exactly ONE candidate.
    if _is_bluetooth(a2dp):
        def _ident_ok(d: DeviceInfo) -> bool:
            same_maker = bool(a2dp.manufacturer) and d.manufacturer == a2dp.manufacturer
            same_model = bool(a2dp.model) and d.model == a2dp.model
            return same_maker or same_model or (not a2dp.manufacturer and not a2dp.model)

        bt_inputs = [d for d in infos
                     if d.obj != a2dp.obj and d.in_ch > 0 and _is_bluetooth(d) and _ident_ok(d)]
        exact = [d for d in bt_inputs if _norm(d.name) == _norm(a2dp.name)]
        if len(exact) >= 1:
            sib = min(exact, key=_hfp_rank)
            return EarbudGroup(a2dp.uid, sib.uid, a2dp.obj, sib.obj, "fallback")
        loose = [d for d in bt_inputs if needle in d.name.lower()]
        if len(loose) == 1:  # only accept containment when it's unambiguous (no max() guessing)
            sib = loose[0]
            return EarbudGroup(a2dp.uid, sib.uid, a2dp.obj, sib.obj, "fallback")

    # 3. No confirmed HFP sibling: persist the output UID only (still better than name substrings).
    return EarbudGroup(a2dp.uid, None, a2dp.obj, None, "output-only")


def _device_by_uid(uid: str, infos: list[DeviceInfo] | None = None) -> DeviceInfo | None:
    if not uid:
        return None
    if infos is None:
        infos = _enumerate()
    return next((d for d in infos if d.uid == uid), None)


def output_channels_for_uid(uid: str, infos: list[DeviceInfo] | None = None) -> int:
    """Output channel count of the device with this UID (2 = A2DP stereo, 1 = HFP mono). 0 if gone."""
    if not _IS_MAC:
        return 2
    d = _device_by_uid(uid, infos)
    return d.out_ch if d is not None else 0


def default_input_uid() -> str | None:
    """UID of the current system default INPUT device (the thing macOS hands new mic readers)."""
    if not _IS_MAC:
        return None
    obj = _get_scalar(kAudioObjectSystemObject, PROP_DEFAULT_INPUT, ctypes.c_uint32)
    return _device_uid(obj) if obj else None


def input_uid_for_name(name_substr: str, infos: list[DeviceInfo] | None = None) -> str | None:
    """UID of the (highest-input-channel) device whose name contains `name_substr` - e.g. the DJI."""
    if not _IS_MAC:
        return None
    if infos is None:
        infos = _enumerate()
    needle = name_substr.lower()
    cands = [d for d in infos if needle in d.name.lower() and d.in_ch > 0]
    if not cands:
        return None
    return max(cands, key=lambda d: d.in_ch).uid


def set_default_uid(kind: str, uid: str, infos: list[DeviceInfo] | None = None) -> bool:
    """Set the system default input/output to the device with this UID (scope-correct). True on ok."""
    if not _IS_MAC or not uid:
        return False
    d = _device_by_uid(uid, infos)
    if d is None:
        return False
    selector = PROP_DEFAULT_OUTPUT if kind == "output" else PROP_DEFAULT_INPUT
    a = _addr(selector)
    val = ctypes.c_uint32(d.obj)
    return _ca.AudioObjectSetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(a), 0, None,
        ctypes.sizeof(val), ctypes.byref(val)) == 0


def earbud_input_holders(input_uid: str, own_pid: int | None = None) -> list[str]:
    """Process names holding the EARBUD HFP INPUT specifically (not 'any mic'), excluding us.

    PLAN: for each process read kAudioProcessPropertyDevices (input scope) and check the earbud HFP
    input UID is in that set, CROSS-CHECKED with IsRunningInput (Devices = attribution; IsRunningInput
    filters stale/non-active clients). macOS 14.4+; [] otherwise or if the UID is unknown.
    """
    if not _IS_MAC or not input_uid:
        return []
    try:
        infos = _enumerate()
        target = _device_by_uid(input_uid, infos)
        if target is None:
            return []
        own_pid = os.getpid() if own_pid is None else own_pid
        holders: list[str] = []
        for proc in _get_array(kAudioObjectSystemObject, PROP_PROCESS_LIST, ctypes.c_uint32):
            if not _get_scalar(proc, PROP_PROC_RUNNING_INPUT, ctypes.c_uint32):
                continue  # IsRunningInput filters stale clients
            pid = _get_scalar(proc, PROP_PROC_PID, ctypes.c_int32)
            if not pid or pid == own_pid:
                continue
            dev_objs = _get_array(proc, PROP_PROC_DEVICES, ctypes.c_uint32, SCOPE_INPUT)
            if target.obj in dev_objs:
                holders.append(_proc_name(pid))
        return holders
    except Exception:
        return []


def _find_device_id(name_substr: str, want_output: bool) -> int | None:
    needle = name_substr.lower()
    match = None
    for dev, name, inc, outc in _all_devices():
        if needle in name.lower() and (outc if want_output else inc) > 0:
            if match is None or (outc if want_output else inc) > match[1]:
                match = (dev, outc if want_output else inc)
    return match[0] if match else None


def set_default(kind: str, name_substr: str) -> bool:
    """Set the system default input/output device by name substring. True on success."""
    if not _IS_MAC:
        return False
    want_output = kind == "output"
    dev = _find_device_id(name_substr, want_output)
    if dev is None:
        return False
    selector = PROP_DEFAULT_OUTPUT if want_output else PROP_DEFAULT_INPUT
    a = _addr(selector)
    val = ctypes.c_uint32(dev)
    return _ca.AudioObjectSetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(a), 0, None,
        ctypes.sizeof(val), ctypes.byref(val)) == 0


# -- the un-stick recipe --------------------------------------------------------------------------

def _find_blueutil() -> str | None:
    """Locate blueutil. shutil.which alone misses it when the app is launched from Finder (launchd
    PATH excludes Homebrew), so also probe the usual install locations."""
    found = shutil.which("blueutil")
    if found:
        return found
    for p in ("/opt/homebrew/bin/blueutil", "/usr/local/bin/blueutil"):
        if os.path.exists(p):
            return p
    return None


def _bt_address(name_substr: str) -> str | None:
    try:
        out = subprocess.run(["system_profiler", "SPBluetoothDataType"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if name_substr.lower() in line.lower() and line.strip().endswith(":"):
            for j in range(i + 1, min(i + 12, len(lines))):
                s = lines[j].strip()
                if s.lower().startswith("address:"):
                    return s.split(":", 1)[1].strip()
    return None


def force_earbuds_stereo(output_substr: str, input_substr: str,
                         park_output_substr: str = "MacBook Pro Speakers") -> dict:
    """Un-stick BT earbuds from HFP mono back to A2DP stereo.

    Recipe (proven 18 Jun 2026): park BOTH default routes off the earbuds (output -> built-in
    speakers, input -> the real mic) so the earbuds go fully idle and the SCO link drops; wait for
    the A2DP renegotiation; if still mono, disconnect/reconnect over Bluetooth; then hand the
    default output back to the earbuds (input stays on the real mic so nothing re-grabs their mic).
    Returns {ok, channels, steps}.
    """
    steps: list[str] = []
    if not _IS_MAC:
        return {"ok": False, "channels": 0, "steps": ["not macOS"]}

    # 1. steer both defaults off the earbuds
    set_default("input", input_substr)
    set_default("output", park_output_substr)
    steps.append(f"parked output->{park_output_substr}, input->{input_substr}")
    time.sleep(4.0)

    # 2. if still mono, do a Bluetooth reconnect (needs blueutil)
    if output_channels(output_substr) < 2:
        addr = _bt_address(output_substr)
        blueutil = _find_blueutil()
        if addr and blueutil:
            try:
                subprocess.run([blueutil, "--disconnect", addr], timeout=10)
                time.sleep(3.0)
                subprocess.run([blueutil, "--connect", addr], timeout=10)
                steps.append("bluetooth reconnect")
                # hold the default input on the real mic through the reconnect window
                for _ in range(8):
                    set_default("input", input_substr)
                    time.sleep(0.6)
            except Exception as exc:
                steps.append(f"blueutil failed: {exc}")
        else:
            steps.append("still mono; blueutil/address unavailable")

    # 3. hand output back to the earbuds (only worth it if they're stereo now)
    chans = output_channels(output_substr)
    if chans >= 2:
        set_default("output", output_substr)
        steps.append(f"output->{output_substr}")
    chans = output_channels(output_substr)
    return {"ok": chans >= 2, "channels": chans, "steps": steps}


# -- UID-aware recipe pieces (Slice 2: the guardian runs park first, reconnect only on tap) --------

def park_recipe(output_uid: str, dji_input_uid: str | None,
                park_output_substr: str = "MacBook Pro Speakers") -> dict:
    """Park BOTH default routes off the earbuds (output -> built-in speakers, input -> the DJI by
    UID) so the SCO link drops and the buds renegotiate A2DP, then hand output back. NO reconnect.
    The disruptive ~4s laptop-speaker blip; user-tapped only (the guardian gates it). {ok,channels}."""
    steps: list[str] = []
    if not _IS_MAC:
        return {"ok": False, "channels": 0, "steps": ["not macOS"]}
    if dji_input_uid:
        set_default_uid("input", dji_input_uid)
    set_default("output", park_output_substr)
    steps.append(f"parked output->{park_output_substr}, input->{dji_input_uid or '(unknown)'}")
    time.sleep(4.0)
    chans = output_channels_for_uid(output_uid)
    if chans >= 2:
        set_default_uid("output", output_uid)
        steps.append(f"output->{output_uid}")
    chans = output_channels_for_uid(output_uid)
    return {"ok": chans >= 2, "channels": chans, "steps": steps}


def reconnect_recipe(output_uid: str, output_substr: str, dji_input_uid: str | None) -> dict:
    """blueutil disconnect/reconnect (disruptive; can drop other apps' audio). User-tapped only -
    the guardian runs this only if park didn't take. Holds the default input on the DJI through the
    reconnect window so nothing re-grabs the earbud mic, then hands output back. {ok,channels}."""
    steps: list[str] = []
    if not _IS_MAC:
        return {"ok": False, "channels": 0, "steps": ["not macOS"]}
    addr = _bt_address(output_substr)
    blueutil = _find_blueutil()
    if not (addr and blueutil):
        return {"ok": output_channels_for_uid(output_uid) >= 2, "channels": 0,
                "steps": ["blueutil/address unavailable"]}
    try:
        subprocess.run([blueutil, "--disconnect", addr], timeout=10)
        time.sleep(3.0)
        subprocess.run([blueutil, "--connect", addr], timeout=10)
        steps.append("bluetooth reconnect")
        for _ in range(8):  # hold default input on the DJI through the reconnect window
            if dji_input_uid:
                set_default_uid("input", dji_input_uid)
            time.sleep(0.6)
    except Exception as exc:
        steps.append(f"blueutil failed: {exc}")
    chans = output_channels_for_uid(output_uid)
    if chans >= 2:
        set_default_uid("output", output_uid)
        steps.append(f"output->{output_uid}")
    chans = output_channels_for_uid(output_uid)
    return {"ok": chans >= 2, "channels": chans, "steps": steps}
